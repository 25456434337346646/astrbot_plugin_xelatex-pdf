"""答疑归档回写 · QA 独立复核补充测试（严过关）。

针对工程师测试（test_plugin.py）未覆盖的攻击面做独立验证：
- 并发/线程安全：多生产者快速入队不丢不重、队列满丢弃语义、stop 后入队安全、start/stop 幂等与重启
- 令牌竞态：多线程并发 _get_token 只刷新一次（令牌锁 + 双重检查）
- 解析边界：超长正文、嵌套 [ARCHIVE]、CRLF 换行、空输入、仅有标记无正文
- external_id 边界：空题干、None 事件、falsy 消息 id
- payload 组装：pdf_url 缺失 / qq 为空 / 图片为空时字段契约完整
- 失败隔离铁律对抗性验证：submit_fn 抛各种异常，worker 存活且主链路不受影响
- 配置启用判定：开关关闭/凭据缺失时调度器空转、零网络调用；全配置时真实装配且令牌懒加载
- 重试矩阵补全：403/404 不重试、令牌端点瞬时失败重试/永久失败、deduplicated 成功语义
- 解耦验收：调度层不感知 HTTP，enqueue 签名与设计契约一致
- 日志卫生：全链路日志不得出现 client_secret / 完整令牌 / Basic 凭据

全部网络均 mock（MockTransport/SpyTransport），零真实网络调用。
"""

from __future__ import annotations

import base64
import inspect
import os
import threading
import time
import unittest
from unittest.mock import patch

from main import (
    CommandResponse,
    HttpResponse,
    ParsedMessage,
    Plugin,
    QaWritebackClient,
    WritebackError,
    WritebackScheduler,
)
try:  # discover -s tests 模式下 tests 目录在 sys.path
    from test_plugin import (
        DummyEvent,
        MockTransport,
        WritebackIntegrationTests,
        bare_plugin,
        make_client,
        token_response,
    )
except ImportError:  # 直接以包路径执行时回退
    from tests.test_plugin import (
        DummyEvent,
        MockTransport,
        WritebackIntegrationTests,
        bare_plugin,
        make_client,
        token_response,
    )


class SchedulerConcurrencyReviewTests(unittest.TestCase):
    """并发/线程安全：多生产者入队不丢不重、满队列丢弃语义、生命周期边界。"""

    def test_100_jobs_from_multiple_producers_all_consumed_exactly_once(self) -> None:
        received: list[str] = []
        lock = threading.Lock()
        all_consumed = threading.Event()
        total = 100

        def submit(payload: dict) -> dict:
            with lock:
                received.append(payload["external_id"])
                if len(received) == total:
                    all_consumed.set()
            return {"qa_request_id": 1, "deduplicated": False}

        # 放大容量，确保本测试只考察消费正确性而非背压
        scheduler = WritebackScheduler(submit_fn=submit, maxsize=256)
        scheduler.start()
        try:
            def produce(offset: int) -> None:
                for index in range(25):
                    self.assertTrue(scheduler.enqueue({"external_id": f"qq:{offset + index}"}))

            producers = [threading.Thread(target=produce, args=(batch * 25,)) for batch in range(4)]
            for thread in producers:
                thread.start()
            for thread in producers:
                thread.join(5)
            self.assertTrue(all_consumed.wait(5), "worker 未在限时内消费完 100 条任务")
        finally:
            scheduler.stop()

        self.assertEqual(len(received), total)
        self.assertEqual(len(set(received)), total, "出现重复消费")
        self.assertEqual(set(received), {f"qq:{i}" for i in range(total)}, "出现任务丢失")

    def test_full_queue_drops_newest_and_keeps_oldest(self) -> None:
        processed: list[str] = []

        def submit(payload: dict) -> dict:
            processed.append(payload["external_id"])
            return {}

        scheduler = WritebackScheduler(submit_fn=submit, maxsize=3)
        # 不启动 worker，确定性地制造满队列
        results = [scheduler.enqueue({"external_id": name}) for name in ("a", "b", "c", "d", "e")]

        self.assertEqual(results, [True, True, True, False, False])
        scheduler.start()
        scheduler.stop()
        # 丢弃的是最新的 d/e，最早的 a/b/c 全部被消费（drop-newest 语义）
        self.assertEqual(processed, ["a", "b", "c"])
        self.assertEqual(scheduler.pending_count(), 0)

    def test_enqueue_after_stop_does_not_raise(self) -> None:
        scheduler = WritebackScheduler(submit_fn=lambda payload: {})
        scheduler.start()
        scheduler.stop()

        try:
            scheduler.enqueue({"external_id": "qq:late"})
        except Exception as exc:
            self.fail(f"stop() 后 enqueue 抛错，违反『永不抛错』契约: {exc}")

    def test_repeated_start_is_idempotent_and_restart_works(self) -> None:
        processed: list[str] = []

        def submit(payload: dict) -> dict:
            processed.append(payload["external_id"])
            return {}

        scheduler = WritebackScheduler(submit_fn=submit)
        scheduler.start()
        scheduler.start()
        scheduler.start()
        self.assertEqual(len(scheduler._workers), 1, "重复 start 应幂等，只创建一个 worker")

        scheduler.enqueue({"external_id": "first"})
        scheduler.stop()
        self.assertEqual(processed, ["first"])

        # stop 之后重新启动仍能消费
        scheduler.start()
        scheduler.enqueue({"external_id": "second"})
        scheduler.stop()
        self.assertEqual(processed, ["first", "second"])


class TokenRefreshRaceReviewTests(unittest.TestCase):
    """令牌锁：多线程并发取令牌时只真实刷新一次（双重检查生效）。"""

    def test_concurrent_get_token_refreshes_only_once(self) -> None:
        token_calls = {"count": 0}
        count_lock = threading.Lock()

        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                with count_lock:
                    token_calls["count"] += 1
                time.sleep(0.05)  # 放大竞态窗口
                return token_response()
            return HttpResponse(status=200, body={})

        client = make_client(MockTransport(responder))
        results: list[str] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(8)

        def fetch() -> None:
            barrier.wait(5)
            token = client._get_token()
            with results_lock:
                results.append(token)

        threads = [threading.Thread(target=fetch) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(len(results), 8)
        self.assertEqual(set(results), {"token-1"})
        self.assertEqual(token_calls["count"], 1, "令牌锁失效：并发下发生了多次刷新")


class ParseBoundaryReviewTests(unittest.TestCase):
    """_parse_structured_answer 边界：工程师未覆盖的输入形态。"""

    def test_very_long_body_with_archive_parses(self) -> None:
        plugin = bare_plugin()
        long_body = "\n".join(f"line {i} " + "x" * 100 for i in range(1000))
        answer = f"[TITLE] 长正文\n{long_body}\n[ARCHIVE]\n[STEM] 题干\n[ANSWER] A\n[/ARCHIVE]"

        result = plugin._parse_structured_answer(answer)

        self.assertEqual(result["title"], "长正文")
        self.assertTrue(result["archive"]["present"])
        self.assertEqual(result["archive"]["stem"], "题干")
        self.assertEqual(result["archive"]["answer"], "A")
        self.assertIn("line 999", result["body"])
        self.assertNotIn("[ARCHIVE]", result["body"])

    def test_nested_archive_marker_inside_fence_is_inert(self) -> None:
        plugin = bare_plugin()
        answer = "[ARCHIVE]\n[STEM] 外层题干\n[ARCHIVE]\n[ANSWER] 外层答案\n[/ARCHIVE]\n尾部正文"

        result = plugin._parse_structured_answer(answer)

        # 围栏内的 [ARCHIVE] 只是普通内容行：不开启新层、不导致异常
        self.assertTrue(result["archive"]["present"])
        self.assertEqual(result["archive"]["stem"], "外层题干")
        self.assertEqual(result["archive"]["answer"], "外层答案")
        self.assertEqual(result["body"], "尾部正文")

    def test_crlf_line_endings_parse(self) -> None:
        plugin = bare_plugin()
        answer = "[TITLE] 标题\r\n[SUMMARY] 提要\r\n正文行\r\n[ARCHIVE]\r\n[STEM] 题干\r\n[TYPE] calc\r\n[/ARCHIVE]\r\n"

        result = plugin._parse_structured_answer(answer)

        self.assertEqual(result["title"], "标题")
        self.assertEqual(result["summary"], "提要")
        self.assertEqual(result["body"], "正文行")
        self.assertTrue(result["archive"]["present"])
        self.assertEqual(result["archive"]["stem"], "题干")
        self.assertEqual(result["archive"]["type"], "calc")

    def test_empty_input_returns_defaults(self) -> None:
        plugin = bare_plugin()

        result = plugin._parse_structured_answer("")

        self.assertEqual(result["title"], "")
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["body"], "")
        self.assertFalse(result["archive"]["present"])

    def test_only_markers_no_body_falls_back_to_raw_text(self) -> None:
        plugin = bare_plugin()
        answer = "[TITLE] 标题\n[SUMMARY] 提要"

        result = plugin._parse_structured_answer(answer)

        self.assertEqual(result["title"], "标题")
        self.assertEqual(result["summary"], "提要")
        self.assertFalse(result["archive"]["present"])
        # 既有降级语义（承自 v0.5.0 基线，非本分支引入）：无正文时 body 回落原文
        self.assertEqual(result["body"], answer)


class ExternalIdBoundaryReviewTests(unittest.TestCase):
    """_build_external_id 边界：空题干、None 事件、falsy 消息 id。"""

    def test_empty_question_and_none_event_still_builds_fallback_id(self) -> None:
        plugin = bare_plugin()

        self.assertRegex(plugin._build_external_id(None, ""), r"^ts:\d{14}:[0-9a-f]{16}$")

    def test_message_id_wins_over_empty_question(self) -> None:
        plugin = bare_plugin()

        self.assertEqual(plugin._build_external_id(DummyEvent(message_id="7"), ""), "qq:7")

    def test_falsy_message_id_falls_back_to_timestamp_digest(self) -> None:
        plugin = bare_plugin()

        class ZeroIdEvent:
            message_id = 0

        self.assertRegex(plugin._build_external_id(ZeroIdEvent(), "题目"), r"^ts:\d{14}:[0-9a-f]{16}$")


class PayloadAssemblyReviewTests(unittest.TestCase):
    """_build_writeback_payload：缺失字段不抛错且字段契约完整（入队时刻冻结）。"""

    def _structured(self) -> dict:
        return {
            "title": "标题",
            "summary": "提要",
            "body": "正文",
            "archive": {"present": True, "stem": "", "answer": "", "type": "answer", "difficulty": 3, "tags": []},
        }

    def test_missing_pdf_url_empty_qq_no_images_still_complete(self) -> None:
        plugin = bare_plugin()
        response = CommandResponse(success=True, message="ok", compile_result=None, file_payload=None)

        payload = plugin._build_writeback_payload(
            event=DummyEvent(),  # 无 user_id → qq 为空
            question_text="题目",
            structured=self._structured(),
            parsed_message=ParsedMessage(text="/ai 题目"),  # 无图片
            session_id="s1",
            base_name="base",
            used_model="model-x",
            response=response,
        )

        self.assertEqual(payload["qq"], "")
        self.assertEqual(payload["question_images"], [])
        self.assertEqual(payload["meta"]["pdf_url"], "")
        self.assertEqual(payload["meta"]["model"], "model-x")
        self.assertEqual(payload["meta"]["base_name"], "base")
        self.assertEqual(payload["meta"]["session_id"], "s1")
        # 字段契约完整性（设计文档 §5.4）
        self.assertEqual(
            set(payload.keys()),
            {"external_id", "question_text", "question_images", "qq", "meta", "answer_text", "analysis_text", "draft"},
        )
        self.assertEqual(set(payload["meta"].keys()), {"model", "base_name", "pdf_url", "session_id"})
        self.assertEqual(set(payload["draft"].keys()), {"stem", "answer", "analysis", "type", "difficulty", "images", "tags"})

    def test_answer_fallback_chain_and_file_payload_without_url(self) -> None:
        plugin = bare_plugin()
        structured = self._structured()
        structured["summary"] = ""  # answer 与 summary 均空 → 占位串

        with self.subTest("file_payload 无 url 键"):
            response = CommandResponse(success=True, message="ok", file_payload={"path": "/tmp/a.pdf"})
            payload = plugin._build_writeback_payload(
                event=DummyEvent(message_id="9"),
                question_text="原始提问",
                structured=structured,
                parsed_message=ParsedMessage(text="/ai 题目"),
                session_id="s",
                base_name="b",
                used_model="m",
                response=response,
            )
            self.assertEqual(payload["meta"]["pdf_url"], "")
            self.assertEqual(payload["answer_text"], "（模型未给出答案）")
            self.assertEqual(payload["draft"]["answer"], "（模型未给出答案）")
            # stem 缺失回落原始提问
            self.assertEqual(payload["draft"]["stem"], "原始提问")


class FailureIsolationReviewTests(unittest.TestCase):
    """失败隔离铁律的对抗性验证：submit_fn 抛任何异常都不得扩散。"""

    def test_worker_survives_after_submit_exceptions(self) -> None:
        processed: list[str] = []
        done = threading.Event()

        def submit(payload: dict) -> dict:
            external_id = payload["external_id"]
            if external_id == "boom-runtime":
                raise RuntimeError("runtime explosion")
            if external_id == "boom-value":
                raise ValueError("value explosion")
            if external_id == "boom-network":
                raise ConnectionError("network explosion")
            processed.append(external_id)
            if external_id == "final":
                done.set()
            return {}

        scheduler = WritebackScheduler(submit_fn=submit)
        scheduler.start()
        try:
            for external_id in ("boom-runtime", "boom-value", "boom-network", "final"):
                scheduler.enqueue({"external_id": external_id})
            self.assertTrue(done.wait(5), "worker 在异常后停止了工作")
        finally:
            scheduler.stop()

        self.assertEqual(processed, ["final"])

    def test_enqueue_hook_swallows_broken_scheduler(self) -> None:
        plugin = bare_plugin()

        class BrokenScheduler:
            def enqueue(self, payload: dict) -> bool:
                raise RuntimeError("scheduler explosion")

        plugin.writeback_scheduler = BrokenScheduler()

        try:
            plugin._enqueue_qa_writeback({"external_id": "qq:1"})
        except Exception as exc:
            self.fail(f"_enqueue_qa_writeback 向上泄漏异常: {exc}")

    def test_enqueue_hook_missing_scheduler_attribute_is_safe(self) -> None:
        plugin = bare_plugin()
        del plugin.writeback_scheduler

        try:
            plugin._enqueue_qa_writeback({"external_id": "qq:1"})
        except Exception as exc:
            self.fail(f"调度器属性缺失时钩子抛错: {exc}")


class WorkerExplosionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """端到端：真实调度器 + 爆炸 submit_fn 时，handle_event 返回值不受影响。"""

    async def test_handle_event_response_unchanged_when_worker_fails(self) -> None:
        answer = "正文\n[ARCHIVE]\n[STEM] 题干\n[/ARCHIVE]"
        plugin = WritebackIntegrationTests._make_plugin(None, answer)
        consumed = threading.Event()

        def submit(payload: dict) -> dict:
            consumed.set()
            raise RuntimeError("writeback explosion")

        plugin.writeback_scheduler = WritebackScheduler(submit_fn=submit)
        plugin.writeback_scheduler.start()
        patches = WritebackIntegrationTests._patch_pipeline(None, plugin)
        for item in patches:
            item.start()
        try:
            response = await plugin.handle_event(DummyEvent(message_id="1"))
        finally:
            for item in patches:
                item.stop()
            plugin.writeback_scheduler.stop()

        self.assertTrue(response.success)
        self.assertEqual(response.message, "编译成功")
        self.assertTrue(consumed.wait(2), "worker 未消费到任务")


class SpyTransport:
    """间谍传输层：记录实例化与全部调用，用于验证『零网络调用』与真实装配。"""

    instances: list["SpyTransport"] = []

    def __init__(self) -> None:
        SpyTransport.instances.append(self)
        self.json_calls: list[str] = []
        self.form_calls: list[str] = []

    def post_json(self, url: str, payload: dict, headers: dict, timeout: int) -> HttpResponse:
        self.json_calls.append(url)
        if url.endswith("/api/open/v1/qa/requests"):
            return HttpResponse(status=200, body={"request": {"id": 9}, "deduplicated": True})
        return HttpResponse(status=200, body={})

    def post_form(self, url: str, form: dict, headers: dict, timeout: int) -> HttpResponse:
        self.form_calls.append(url)
        return token_response()


class ConfigEnablementReviewTests(unittest.TestCase):
    """配置启用判定（设计 §6）：开关关闭/凭据缺失 → 空转且零网络；全配置 → 真实装配。"""

    def setUp(self) -> None:
        SpyTransport.instances = []

    def _build_plugin(self, enabled: str, base_url: str, client_id: str, client_secret: str) -> Plugin:
        env = {
            "XELATEX_ARCHIVE_ENABLED": enabled,
            "XELATEX_QBK_BASE_URL": base_url,
            "XELATEX_QBK_CLIENT_ID": client_id,
            "XELATEX_QBK_CLIENT_SECRET": client_secret,
        }
        with (
            patch.object(Plugin, "_start_file_server"),
            patch("main.HttpTransport", SpyTransport),
            patch.dict(os.environ, env),
        ):
            return Plugin(None, None)

    def test_disabled_when_all_credentials_absent(self) -> None:
        plugin = self._build_plugin("", "", "", "")

        self.assertFalse(plugin.archive_enabled)
        self.assertFalse(plugin.writeback_scheduler.enabled)
        self.assertFalse(plugin.writeback_scheduler.enqueue({"external_id": "qq:1"}))
        self.assertEqual(SpyTransport.instances, [], "未启用时不得构造传输层/客户端（零网络）")
        self.assertFalse(any(t.name == "qbk-writeback-worker" for t in threading.enumerate()))

    def test_switch_off_with_full_credentials_still_disabled(self) -> None:
        plugin = self._build_plugin("false", "http://qbk.test", "cid", "csecret")

        self.assertFalse(plugin.writeback_scheduler.enabled)
        self.assertFalse(plugin.writeback_scheduler.enqueue({"external_id": "qq:1"}))
        self.assertEqual(SpyTransport.instances, [])

    def test_missing_secret_disables(self) -> None:
        plugin = self._build_plugin("true", "http://qbk.test", "cid", "")

        self.assertFalse(plugin.writeback_scheduler.enabled)
        self.assertEqual(SpyTransport.instances, [])

    def test_missing_client_id_disables(self) -> None:
        plugin = self._build_plugin("true", "http://qbk.test", "", "csecret")

        self.assertFalse(plugin.writeback_scheduler.enabled)
        self.assertEqual(SpyTransport.instances, [])

    def test_fully_configured_wires_real_client_with_lazy_token(self) -> None:
        plugin = self._build_plugin("true", "http://qbk.test", "cid", "csecret")
        try:
            self.assertTrue(plugin.archive_enabled)
            self.assertTrue(plugin.writeback_scheduler.enabled)
            self.assertEqual(len(SpyTransport.instances), 1)
            transport = SpyTransport.instances[0]
            # 令牌懒加载：初始化完成时尚未发起任何网络请求
            self.assertEqual(transport.form_calls, [])
            self.assertEqual(transport.json_calls, [])
            self.assertTrue(any(t.name == "qbk-writeback-worker" for t in threading.enumerate()))

            # 端到端穿过真实 QaWritebackClient：token → qa/requests → answer → archive
            self.assertTrue(
                plugin.writeback_scheduler.enqueue(
                    {"external_id": "qq:77", "qq": "10001", "question_text": "题目", "draft": {}}
                )
            )
            deadline = time.time() + 5
            while time.time() < deadline and len(transport.json_calls) < 3:
                time.sleep(0.01)

            self.assertEqual(len(transport.form_calls), 1)
            self.assertIn("/api/oauth/token", transport.form_calls[0])
            self.assertEqual(
                transport.json_calls,
                [
                    "http://qbk.test/api/open/v1/qa/requests",
                    "http://qbk.test/api/open/v1/qa/requests/9/answer",
                    "http://qbk.test/api/open/v1/archive/entries",
                ],
            )
        finally:
            plugin.writeback_scheduler.stop()


class RetryMatrixReviewTests(unittest.TestCase):
    """重试矩阵（设计 §2.4）补全：工程师只测了 400，这里补 403/404 与令牌端点矩阵。"""

    def test_403_and_404_are_permanent_without_retry(self) -> None:
        for status in (403, 404):
            with self.subTest(status=status):

                def responder(url: str, body: dict, status: int = status) -> HttpResponse:
                    if url.endswith("/api/oauth/token"):
                        return token_response()
                    return HttpResponse(status=status, body={})

                transport = MockTransport(responder)
                with self.assertRaises(WritebackError) as ctx:
                    make_client(transport).submit({"external_id": "qq:1", "draft": {}})

                self.assertEqual(ctx.exception.status, status)
                requests_calls = [call for call in transport.calls if call[1].endswith("/api/open/v1/qa/requests")]
                self.assertEqual(len(requests_calls), 1, f"{status} 应为永久失败且不重试")

    def test_token_endpoint_retries_on_5xx_then_succeeds(self) -> None:
        state = {"token_calls": 0}

        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                state["token_calls"] += 1
                if state["token_calls"] == 1:
                    return HttpResponse(status=503, body={})
                return token_response()
            if url.endswith("/api/open/v1/qa/requests"):
                return HttpResponse(status=200, body={"request": {"id": 1}})
            return HttpResponse(status=200, body={})

        transport = MockTransport(responder)
        with patch("main.time.sleep"):
            summary = make_client(transport).submit({"external_id": "qq:1", "draft": {}})

        self.assertEqual(state["token_calls"], 2)
        self.assertEqual(summary["qa_request_id"], 1)

    def test_token_endpoint_400_is_permanent_without_retry(self) -> None:
        state = {"token_calls": 0}

        def responder(url: str, body: dict) -> HttpResponse:
            state["token_calls"] += 1
            return HttpResponse(status=400, body={"error": "invalid_client"})

        transport = MockTransport(responder)
        with self.assertRaises(WritebackError) as ctx:
            make_client(transport).submit({"external_id": "qq:1", "draft": {}})

        self.assertEqual(state["token_calls"], 1, "令牌 400（凭据问题）重试无意义，必须立即失败")
        self.assertEqual(ctx.exception.status, 400)

    def test_token_response_missing_fields_is_permanent(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            return HttpResponse(status=200, body={"access_token": "", "expires_in": 100})

        transport = MockTransport(responder)
        with self.assertRaises(WritebackError):
            make_client(transport).submit({"external_id": "qq:1", "draft": {}})

        self.assertEqual(len(transport.calls), 1, "响应体缺字段属永久失败，不得重试")

    def test_deduplicated_200_counts_as_success(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            if url.endswith("/api/open/v1/qa/requests"):
                return HttpResponse(status=200, body={"request": {"id": 5}, "deduplicated": True})
            return HttpResponse(status=200, body={})

        summary = make_client(MockTransport(responder)).submit({"external_id": "qq:dup", "draft": {}})

        self.assertTrue(summary["deduplicated"])
        self.assertEqual(summary["qa_request_id"], 5)
        self.assertTrue(summary["answer_step_ok"])
        self.assertTrue(summary["archive_step_ok"])


class DecouplingReviewTests(unittest.TestCase):
    """解耦验收（设计 §4 升级路线）：调度层不感知 HTTP，enqueue 签名是稳定契约。"""

    def test_scheduler_runs_with_any_callable_submit_fn(self) -> None:
        calls: list[dict] = []
        scheduler = WritebackScheduler(submit_fn=calls.append)
        scheduler.start()
        try:
            scheduler.enqueue({"external_id": "qq:1"})
            deadline = time.time() + 2
            while time.time() < deadline and not calls:
                time.sleep(0.01)
        finally:
            scheduler.stop()

        self.assertEqual(calls, [{"external_id": "qq:1"}])

    def test_scheduler_source_has_no_http_dependency(self) -> None:
        source = inspect.getsource(WritebackScheduler)

        # 校验「导入/实例化/调用」而非裸子串：docstring 里对类名的说明性引用不算耦合
        for forbidden in ("urllib", "HttpTransport(", "QaWritebackClient(", "post_json", "post_form"):
            self.assertNotIn(forbidden, source, "调度层源码不得感知传输层/业务层（解耦验收标准）")

    def test_enqueue_signature_matches_design_contract(self) -> None:
        signature = inspect.signature(WritebackScheduler.enqueue)

        self.assertEqual(list(signature.parameters), ["self", "payload"])
        resolved = inspect.signature(WritebackScheduler.enqueue, eval_str=True)
        self.assertIs(resolved.return_annotation, bool)


class LogHygieneReviewTests(unittest.TestCase):
    """日志卫生（设计 §9.1）：全链路日志不得出现凭据与完整令牌。"""

    def test_full_chain_logs_do_not_leak_secrets(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return HttpResponse(
                    status=200,
                    body={"access_token": "super-secret-token-value", "expires_in": 2592000},
                )
            if url.endswith("/api/open/v1/qa/requests"):
                return HttpResponse(status=200, body={"request": {"id": 1}})
            return HttpResponse(status=200, body={})

        client = QaWritebackClient(MockTransport(responder), "http://qbk.test", "client-id-xyz", "client-secret-abc")
        scheduler = WritebackScheduler(submit_fn=client.submit)
        scheduler.start()
        try:
            with self.assertLogs("astrbot_plugin_xelatex_pdf", level="DEBUG") as captured:
                self.assertTrue(
                    scheduler.enqueue({"external_id": "qq:1", "qq": "10001", "question_text": "题目", "draft": {}})
                )
                deadline = time.time() + 5
                while time.time() < deadline:
                    if any("回写成功" in record.getMessage() for record in captured.records):
                        break
                    time.sleep(0.01)
        finally:
            scheduler.stop()

        all_messages = "\n".join(record.getMessage() for record in captured.records)
        self.assertIn("回写成功", all_messages, "成功路径日志缺失，测试前提不成立")
        self.assertNotIn("client-secret-abc", all_messages)
        self.assertNotIn("super-secret-token-value", all_messages)
        basic_credentials = base64.b64encode(b"client-id-xyz:client-secret-abc").decode("ascii")
        self.assertNotIn(basic_credentials, all_messages)


if __name__ == "__main__":
    unittest.main()
