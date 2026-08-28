from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from main import (
    ARCHIVE_TAG_LIMIT,
    ARCHIVE_TYPE_WHITELIST,
    CommandResponse,
    CompileResult,
    HttpResponse,
    MessageAttachment,
    ParsedMessage,
    PLUGIN_VERSION,
    Plugin,
    QaWritebackClient,
    SessionMemory,
    WritebackError,
    WritebackScheduler,
    WRITEBACK_QUEUE_MAXSIZE,
)


class DummyImage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.name = "question.png"


class DummyReply:
    def __init__(self) -> None:
        self.text = "引用中的题目"
        self.chain = [DummyImage("https://example.com/question.png")]


def bare_plugin() -> Plugin:
    plugin = Plugin.__new__(Plugin)
    plugin.context = None
    plugin.active_model_name = "text-model"
    plugin.available_models = []
    plugin.vision_model_name = "vision-model"
    plugin.fallback_vision_models = []
    plugin.text_api_url = "https://api.example.com/v1"
    plugin.text_api_key = "test-key"
    plugin.chat_history = {}
    plugin.max_history_rounds = 5
    plugin.last_tex_source_by_session = {}
    plugin.last_pdf_by_session = {}
    plugin.last_base_name_by_session = {}
    plugin.file_server_port = 8765
    plugin.public_file_host = ""
    # 归档回写：默认空转调度器（未启用），测试按需替换为真实 submit_fn
    plugin.writeback_scheduler = WritebackScheduler(submit_fn=None)
    return plugin


def plugin_with_dirs(temp_root: Path) -> Plugin:
    """在临时目录下初始化分类目录结构的插件实例。"""
    plugin = bare_plugin()
    plugin.data_dir = temp_root
    plugin.asset_dir = temp_root / "assets"
    plugin.source_dir = temp_root / "sources"
    plugin.log_dir = temp_root / "logs"
    plugin.pdf_dir = temp_root / "pdfs"
    plugin.export_dir = plugin.pdf_dir
    for category_dir in (plugin.asset_dir, plugin.source_dir, plugin.log_dir, plugin.pdf_dir):
        category_dir.mkdir(parents=True, exist_ok=True)
    return plugin


class SessionMemoryTests(unittest.TestCase):
    def test_append_keeps_only_latest_rounds(self) -> None:
        memory = SessionMemory(session_id="test")

        for index in range(7):
            memory.append(str(index), str(index), limit=5)

        self.assertEqual([item["user"] for item in memory.rounds], ["2", "3", "4", "5", "6"])


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_is_consistent_across_release_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        document_class = (root / "astrbot-elegantbook.cls").read_text(encoding="utf-8")

        metadata_version = re.search(r"^version:\s*(\S+)", metadata, re.MULTILINE)
        self.assertIsNotNone(metadata_version)
        self.assertEqual(metadata_version.group(1), PLUGIN_VERSION)
        self.assertIn(f"## {PLUGIN_VERSION}", changelog)
        self.assertIn(f"v{PLUGIN_VERSION} AstrBot derivative", document_class)

    def test_configuration_schema_is_valid_json(self) -> None:
        root = Path(__file__).resolve().parents[1]

        with (root / "_conf_schema.json").open(encoding="utf-8") as schema_file:
            schema = json.load(schema_file)

        self.assertIn("text_api_url", schema)
        self.assertIn("ocr_api_url", schema)


class MessageParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_text_does_not_hide_reply_attachment(self) -> None:
        plugin = bare_plugin()

        parsed = await plugin.parse_message([DummyReply()])

        self.assertEqual(parsed.reply_text, "引用中的题目\n[引用附件] https://example.com/question.png")
        self.assertEqual(len(parsed.images), 1)
        self.assertEqual(parsed.images[0].url, "https://example.com/question.png")

    async def test_model_switch_skips_attachment_and_model_work(self) -> None:
        plugin = bare_plugin()
        parsed = ParsedMessage(text="/ai -m another-model")

        with (
            patch.object(plugin, "parse_message", return_value=parsed),
            patch.object(plugin, "localize_attachments") as localize,
            patch.object(plugin, "generate_answer") as generate,
        ):
            response = await plugin.handle_event("ignored")

        self.assertTrue(response.success)
        self.assertEqual(response.message, "已切换模型：another-model")
        self.assertEqual(plugin.active_model_name, "another-model")
        localize.assert_not_called()
        generate.assert_not_called()


class ModelPromptTests(unittest.TestCase):
    def test_attachment_context_is_built_once(self) -> None:
        plugin = bare_plugin()
        parsed = ParsedMessage(
            text="/ai 分析附件",
            images=[MessageAttachment(kind="image", path="question.png")],
        )

        with (
            patch.object(plugin, "_build_attachment_context", return_value="OCR 结果") as attachment_context,
            patch.object(plugin, "_call_text_model", return_value="回答"),
        ):
            answer, model = plugin.generate_answer("/ai 分析附件", parsed, "session")

        self.assertEqual(answer, "回答")
        self.assertEqual(model, "text-model")
        attachment_context.assert_called_once_with(parsed)


class FileSafetyTests(unittest.TestCase):
    def test_source_export_stays_inside_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = plugin_with_dirs(Path(temp_dir))

            exported = plugin._persist_tex_source("../../outside/session", "content")

            self.assertEqual(exported.parent, plugin.source_dir)
            self.assertNotIn("..", exported.name)
            self.assertEqual(exported.read_text(encoding="utf-8"), "content")

    def test_compile_source_copies_only_approved_images(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as work_dir:
            plugin = bare_plugin()
            plugin.asset_dir = Path(data_dir)
            plugin.data_dir = Path(data_dir)
            plugin.export_dir = Path(data_dir)
            image_path = Path(data_dir) / "question.png"
            image_path.write_bytes(b"not-a-real-image")
            source = f"before\\includegraphics[width=1cm]{{{image_path}}}after"

            prepared = plugin._prepare_compile_source(source, Path(work_dir))

            self.assertIn(r"\includegraphics[width=1cm]{attachment_1.png}", prepared)
            self.assertEqual((Path(work_dir) / "attachment_1.png").read_bytes(), b"not-a-real-image")

    def test_compile_source_rejects_images_outside_data_directories(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as work_dir:
            plugin = bare_plugin()
            plugin.asset_dir = Path(data_dir)
            plugin.data_dir = Path(data_dir)
            plugin.export_dir = Path(data_dir)

            prepared = plugin._prepare_compile_source(
                r"\includegraphics{/etc/passwd}",
                Path(work_dir),
            )

            self.assertEqual(prepared, r"\fbox{\textit{图片附件不可用}}")

    def test_successful_command_cleans_compile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = plugin_with_dirs(Path(temp_dir))
            with tempfile.TemporaryDirectory() as work_dir:
                work_path = Path(work_dir)
                pdf_path = work_path / "main.pdf"
                pdf_path.write_bytes(b"%PDF-1.7\n%%EOF")
                compile_result = CompileResult(
                    success=True,
                    workdir_path=work_path,
                    pdf_path=pdf_path,
                    log_path=None,
                    stdout="",
                    stderr="",
                )

                with patch.object(plugin, "compile_tex", return_value=compile_result):
                    response = plugin.handle_command("/ai test", tex_source="source", base_name="123_题目_20260825_120000")

                self.assertTrue(response.success)
                self.assertFalse(work_path.exists())
                self.assertIsNone(compile_result.workdir_path)
                self.assertIsNotNone(compile_result.pdf_path)
                self.assertEqual(compile_result.pdf_path.parent, plugin.pdf_dir)
                self.assertTrue(compile_result.pdf_path.exists())


class NamingConventionTests(unittest.TestCase):
    """验证「QQ号_题目关键词_时间戳」统一命名与分类存储。"""

    def test_build_base_name_contains_qq_keyword_and_timestamp(self) -> None:
        plugin = bare_plugin()
        base_name = plugin._build_base_name("123456789", "/ai 求解偏序集哈斯图")

        self.assertTrue(base_name.startswith("123456789_"))
        self.assertIn("求解偏序集哈斯图", base_name)
        # 时间戳形如 20260825_120000
        self.assertRegex(base_name, r"_\d{8}_\d{6}$")

    def test_build_base_name_falls_back_when_qq_missing(self) -> None:
        plugin = bare_plugin()
        base_name = plugin._build_base_name("", "/ai 题目")

        self.assertTrue(base_name.startswith("unknown_"))

    def test_keyword_strips_command_prefix_and_punctuation(self) -> None:
        plugin = bare_plugin()
        keyword = plugin._extract_question_keyword("/ai 证明：根号2是无理数！")

        self.assertNotIn("/ai", keyword)
        self.assertNotIn("：", keyword)
        self.assertNotIn("！", keyword)
        self.assertIn("证明", keyword)

    def test_three_artifacts_share_same_base_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = plugin_with_dirs(Path(temp_dir))
            base_name = "123456789_哈斯图_20260825_120000"

            source_path = plugin._persist_tex_source(base_name, "tex content")
            pdf_path = plugin._copy_to_export_dir(Path(__file__), base_name=base_name)

            self.assertEqual(source_path.parent, plugin.source_dir)
            self.assertEqual(source_path.stem, base_name)
            self.assertIsNotNone(pdf_path)
            self.assertEqual(pdf_path.parent, plugin.pdf_dir)
            self.assertEqual(pdf_path.stem, base_name)

    def test_compile_log_archived_to_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = plugin_with_dirs(Path(temp_dir))
            base_name = "123456789_哈斯图_20260825_120000"
            with tempfile.TemporaryDirectory() as work_dir:
                log_path = Path(work_dir) / "main.log"
                log_path.write_text("compile log", encoding="utf-8")
                compile_result = CompileResult(
                    success=True,
                    workdir_path=Path(work_dir),
                    pdf_path=None,
                    log_path=log_path,
                    stdout="",
                    stderr="",
                )

                plugin._cleanup_compile_result(compile_result, base_name=base_name)

                archived = plugin.log_dir / f"{base_name}.log"
                self.assertTrue(archived.exists())
                self.assertEqual(archived.read_text(encoding="utf-8"), "compile log")

    def test_public_file_url_routes_by_category(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = plugin_with_dirs(Path(temp_dir))
            pdf_file = plugin.pdf_dir / "a.pdf"
            source_file = plugin.source_dir / "a.tex"
            log_file = plugin.log_dir / "a.log"

            self.assertIn("/pdf/a.pdf", plugin._public_file_url(pdf_file))
            self.assertIn("/source/a.tex", plugin._public_file_url(source_file))
            self.assertIn("/log/a.log", plugin._public_file_url(log_file))


class StructuredAnswerTests(unittest.TestCase):
    """验证 [TITLE]/[SUMMARY] 结构化解析与去硬编码后的降级行为。"""

    def test_parse_structured_answer_extracts_title_summary_body(self) -> None:
        plugin = bare_plugin()
        answer = "[TITLE] 根号2的无理性证明\n[SUMMARY] 用反证法证明根号2是无理数。\n\n## 证明\n假设根号2是有理数。"

        result = plugin._parse_structured_answer(answer)

        self.assertEqual(result["title"], "根号2的无理性证明")
        self.assertEqual(result["summary"], "用反证法证明根号2是无理数。")
        self.assertEqual(result["body"], "## 证明\n假设根号2是有理数。")

    def test_parse_structured_answer_falls_back_without_markers(self) -> None:
        plugin = bare_plugin()
        answer = "## 证明\n假设根号2是有理数。"

        result = plugin._parse_structured_answer(answer)

        self.assertEqual(result["title"], "")
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["body"], answer)

    def test_tex_source_uses_llm_title_and_summary(self) -> None:
        plugin = bare_plugin()
        tex = plugin.build_tex_source(
            ParsedMessage(text=""),
            body_override="正文内容",
            question_text="证明根号2是无理数",
            title_override="LLM 生成的标题",
            summary_override="LLM 生成的提要",
        )

        self.assertIn("LLM 生成的标题", tex)
        self.assertIn("LLM 生成的提要", tex)
        self.assertIn("内容提要", tex)

    def test_tex_source_omits_summary_box_when_no_summary(self) -> None:
        plugin = bare_plugin()
        tex = plugin.build_tex_source(
            ParsedMessage(text=""),
            body_override="正文内容",
            question_text="证明根号2是无理数",
            title_override="",
            summary_override="",
        )

        # 无 LLM 提要时，提要盒整体省略，不再硬编码套话
        # （导言区的 \newtcolorbox 定义仍在，只检查使用处）
        self.assertNotIn("\\begin{answerabstractbox}", tex)
        # 标题回退为问题截取，不再出现硬编码分类标题
        self.assertIn("证明根号2是无理数", tex)
        self.assertNotIn("数学证明与结构化解答", tex)

    def test_no_hardcoded_insight_notes(self) -> None:
        plugin = bare_plugin()
        tex = plugin.build_tex_source(
            ParsedMessage(text=""),
            body_override="正文内容",
            question_text="画哈斯图并证明结论",
        )

        # 去硬编码后不再出现写死的洞察笔记
        self.assertNotIn("图形题：优先保留原生 TikZ/图环境", tex)
        self.assertNotIn("证明题：建议先写结论", tex)


class DummyEvent:
    """模拟 AstrBot 事件对象：携带消息 id、QQ 号与会话 id。"""

    def __init__(self, message_id: str = "", user_id: str = "", session_id: str = "group_1_user_1") -> None:
        self.message_id = message_id
        self.user_id = user_id
        self.session_id = session_id


class MockTransport:
    """mock HttpTransport：按 URL 子串分发脚本化响应，记录全部调用。零真实网络。"""

    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[tuple[str, str, dict, dict]] = []

    def post_json(self, url: str, payload: dict, headers: dict, timeout: int) -> HttpResponse:
        self.calls.append(("json", url, dict(payload), dict(headers)))
        return self._responder(url, payload)

    def post_form(self, url: str, form: dict, headers: dict, timeout: int) -> HttpResponse:
        self.calls.append(("form", url, dict(form), dict(headers)))
        return self._responder(url, form)


def token_response() -> HttpResponse:
    return HttpResponse(status=200, body={"access_token": "token-1", "expires_in": 2592000})


def make_client(transport: MockTransport) -> QaWritebackClient:
    return QaWritebackClient(transport, "http://qbk.test", "cid", "csecret")


class ArchiveParseTests(unittest.TestCase):
    """[ARCHIVE] 围栏块解析容错矩阵（研究文档 §3.4）。"""

    def test_full_archive_block_parsed(self) -> None:
        plugin = bare_plugin()
        answer = (
            "[TITLE] 极限求解\n[SUMMARY] 用洛必达法则求极限。\n\n## 解答\n正文内容。\n\n"
            "[ARCHIVE]\n"
            "[STEM] 求极限 $\\lim_{x\\to 0}\\frac{\\sin x}{x}$。\n"
            "[ANSWER] 1\n"
            "[TYPE] calc\n"
            "[DIFFICULTY] 2\n"
            "[TAGS] 极限, 洛必达法则,中值定理\n"
            "[/ARCHIVE]\n"
        )

        result = plugin._parse_structured_answer(answer)

        self.assertEqual(result["title"], "极限求解")
        self.assertEqual(result["summary"], "用洛必达法则求极限。")
        self.assertEqual(result["body"], "## 解答\n正文内容。")
        archive = result["archive"]
        self.assertTrue(archive["present"])
        self.assertEqual(archive["stem"], "求极限 $\\lim_{x\\to 0}\\frac{\\sin x}{x}$。")
        self.assertEqual(archive["answer"], "1")
        self.assertEqual(archive["type"], "calc")
        self.assertEqual(archive["difficulty"], 2)
        self.assertEqual(archive["tags"], ["极限", "洛必达法则", "中值定理"])

    def test_missing_archive_block_marks_not_present(self) -> None:
        plugin = bare_plugin()

        result = plugin._parse_structured_answer("[TITLE] 标题\n正文")

        self.assertFalse(result["archive"]["present"])
        self.assertEqual(result["body"], "正文")

    def test_unclosed_archive_fence_parses_to_end(self) -> None:
        plugin = bare_plugin()

        result = plugin._parse_structured_answer("正文\n[ARCHIVE]\n[STEM] 题干\n[ANSWER] A")

        archive = result["archive"]
        self.assertTrue(archive["present"])
        self.assertEqual(archive["stem"], "题干")
        self.assertEqual(archive["answer"], "A")
        self.assertEqual(result["body"], "正文")

    def test_bad_lines_fall_back_field_by_field(self) -> None:
        plugin = bare_plugin()
        answer = "[ARCHIVE]\n[STEM] 题干\n[TYPE] 无效类型\n[DIFFICULTY] 9\n[/ARCHIVE]"

        archive = plugin._parse_structured_answer(answer)["archive"]

        self.assertEqual(archive["stem"], "题干")
        self.assertEqual(archive["answer"], "")
        self.assertEqual(archive["type"], "answer")
        self.assertEqual(archive["difficulty"], 3)
        self.assertEqual(archive["tags"], [])

    def test_illegal_type_falls_back_to_answer(self) -> None:
        plugin = bare_plugin()

        archive = plugin._parse_structured_answer("[ARCHIVE]\n[TYPE] essay\n[/ARCHIVE]")["archive"]

        self.assertEqual(archive["type"], "answer")
        for legal_type in ARCHIVE_TYPE_WHITELIST:
            parsed = plugin._parse_structured_answer(f"[ARCHIVE]\n[TYPE] {legal_type}\n[/ARCHIVE]")
            self.assertEqual(parsed["archive"]["type"], legal_type)

    def test_illegal_difficulty_falls_back_to_three(self) -> None:
        plugin = bare_plugin()

        for bad_value in ("0", "6", "abc", "-1", "3.5", ""):
            archive = plugin._parse_structured_answer(f"[ARCHIVE]\n[DIFFICULTY] {bad_value}\n[/ARCHIVE]")["archive"]
            self.assertEqual(archive["difficulty"], 3)

    def test_archive_block_stripped_from_body(self) -> None:
        plugin = bare_plugin()
        answer = "正文第一行\n[ARCHIVE]\n[STEM] 题干\n[ANSWER] B\n[/ARCHIVE]\n正文最后一行"

        result = plugin._parse_structured_answer(answer)

        self.assertNotIn("[ARCHIVE]", result["body"])
        self.assertNotIn("[/ARCHIVE]", result["body"])
        self.assertNotIn("[STEM]", result["body"])
        self.assertEqual(result["body"], "正文第一行\n正文最后一行")

    def test_archive_only_answer_does_not_leak_markers_back(self) -> None:
        plugin = bare_plugin()
        answer = "[ARCHIVE]\n[STEM] 题干\n[ANSWER] B\n[/ARCHIVE]"

        result = plugin._parse_structured_answer(answer)

        # body 为空时不得回落原文（否则归档标记会被带回正文、污染 PDF）
        self.assertEqual(result["body"], "")
        self.assertTrue(result["archive"]["present"])

    def test_markers_outside_fence_are_not_parsed_as_archive(self) -> None:
        plugin = bare_plugin()

        result = plugin._parse_structured_answer("正文里提到 [ANSWER] 字样\n另一行")

        self.assertFalse(result["archive"]["present"])
        self.assertIn("[ANSWER] 字样", result["body"])

    def test_tags_deduplicated_and_truncated(self) -> None:
        plugin = bare_plugin()
        tags = ",".join([f"t{i}" for i in range(25)] + ["t0", "t1"])

        archive = plugin._parse_structured_answer(f"[ARCHIVE]\n[TAGS] {tags}\n[/ARCHIVE]")["archive"]

        self.assertEqual(len(archive["tags"]), ARCHIVE_TAG_LIMIT)
        self.assertEqual(len(set(archive["tags"])), len(archive["tags"]))

    def test_parse_always_returns_full_four_keys(self) -> None:
        plugin = bare_plugin()

        for text in ("", "[TITLE] 只有标题", "[ARCHIVE]\n坏行\x00\n[/ARCHIVE]"):
            result = plugin._parse_structured_answer(text)
            self.assertEqual(set(result.keys()), {"title", "summary", "body", "archive"})


class ExternalIdTests(unittest.TestCase):
    def test_message_id_takes_priority(self) -> None:
        plugin = bare_plugin()

        external_id = plugin._build_external_id(DummyEvent(message_id="98765"), "题目")

        self.assertEqual(external_id, "qq:98765")

    def test_message_id_found_on_message_obj(self) -> None:
        plugin = bare_plugin()

        class EventWithMessageObj:
            def __init__(self) -> None:
                self.message_obj = DummyEvent(message_id="42")

        self.assertEqual(plugin._build_external_id(EventWithMessageObj(), "题目"), "qq:42")

    def test_fallback_uses_timestamp_and_digest(self) -> None:
        plugin = bare_plugin()

        external_id = plugin._build_external_id(DummyEvent(), "证明根号2是无理数")

        self.assertRegex(external_id, r"^ts:\d{14}:[0-9a-f]{16}$")
        # 同一题同秒重发：幂等键一致
        self.assertEqual(external_id, plugin._build_external_id(DummyEvent(), "证明根号2是无理数"))
        self.assertNotEqual(external_id, plugin._build_external_id(DummyEvent(), "另一道题"))

    def test_none_event_falls_back(self) -> None:
        plugin = bare_plugin()

        self.assertRegex(plugin._build_external_id(None, "题目"), r"^ts:\d{14}:[0-9a-f]{16}$")


class WritebackClientTests(unittest.TestCase):
    """回写客户端：令牌缓存 / 401 续期 / 重试矩阵 / 三步顺序（全部 mock，零真实网络）。"""

    def test_three_step_order_and_request_bodies(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            if url.endswith("/api/open/v1/qa/requests"):
                return HttpResponse(status=201, body={"request": {"id": 77}, "deduplicated": False})
            return HttpResponse(status=200, body={})

        transport = MockTransport(responder)
        client = make_client(transport)
        payload = {
            "external_id": "qq:1",
            "qq": "10001",
            "question_text": "题目文本",
            "question_images": ["http://host/asset/a.png"],
            "meta": {"model": "m", "base_name": "b", "pdf_url": "http://host/pdf/x.pdf", "session_id": "s"},
            "answer_text": "答案A",
            "analysis_text": "解析正文",
            "draft": {"stem": "题干", "answer": "答案A", "analysis": "解析正文", "type": "calc", "difficulty": 2, "images": [], "tags": ["极限"]},
        }

        summary = client.submit(payload)

        self.assertEqual(summary["qa_request_id"], 77)
        self.assertTrue(summary["answer_step_ok"])
        self.assertTrue(summary["archive_step_ok"])
        urls = [url for (_kind, url, _body, _headers) in transport.calls]
        self.assertEqual(
            urls,
            [
                "http://qbk.test/api/oauth/token",
                "http://qbk.test/api/open/v1/qa/requests",
                "http://qbk.test/api/open/v1/qa/requests/77/answer",
                "http://qbk.test/api/open/v1/archive/entries",
            ],
        )
        request_body = transport.calls[1][2]
        self.assertEqual(request_body["external_id"], "qq:1")
        self.assertEqual(request_body["channel"], "qq_bot")
        self.assertEqual(request_body["channel_subject"], "10001")
        self.assertEqual(request_body["qq"], "10001")
        self.assertEqual(request_body["question_text"], "题目文本")
        self.assertEqual(request_body["question_images"], ["http://host/asset/a.png"])
        self.assertEqual(request_body["meta"]["model"], "m")
        answer_body = transport.calls[2][2]
        self.assertEqual(answer_body["answer_text"], "答案A")
        self.assertEqual(answer_body["analysis_text"], "解析正文")
        self.assertEqual(answer_body["external_ref"], "http://host/pdf/x.pdf")
        archive_body = transport.calls[3][2]
        self.assertEqual(archive_body["qa_request_id"], 77)
        self.assertEqual(archive_body["external_id"], "qq:1")
        self.assertEqual(archive_body["draft"]["type"], "calc")

    def test_token_is_cached_across_submits(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            if url.endswith("/api/open/v1/qa/requests"):
                return HttpResponse(status=200, body={"request": {"id": 1}, "deduplicated": True})
            return HttpResponse(status=200, body={})

        transport = MockTransport(responder)
        client = make_client(transport)
        payload = {"external_id": "qq:1", "answer_text": "A", "draft": {}}

        client.submit(payload)
        client.submit(payload)

        token_calls = [call for call in transport.calls if call[1].endswith("/api/oauth/token")]
        self.assertEqual(len(token_calls), 1)

    def test_token_request_uses_basic_auth_and_client_credentials(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            if url.endswith("/api/open/v1/qa/requests"):
                return HttpResponse(status=200, body={"request": {"id": 1}})
            return HttpResponse(status=200, body={})

        transport = MockTransport(responder)
        make_client(transport).submit({"external_id": "qq:1", "draft": {}})

        kind, url, form, headers = transport.calls[0]
        self.assertEqual(kind, "form")
        self.assertEqual(url, "http://qbk.test/api/oauth/token")
        self.assertEqual(form, {"grant_type": "client_credentials"})
        # Basic base64("cid:csecret")，secret 只进请求头不进日志
        self.assertEqual(headers["Authorization"], "Basic Y2lkOmNzZWNyZXQ=")

    def test_401_triggers_single_token_refresh_and_resend(self) -> None:
        state = {"requests_calls": 0, "token_calls": 0}

        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                state["token_calls"] += 1
                return HttpResponse(status=200, body={"access_token": f"token-{state['token_calls']}", "expires_in": 2592000})
            if url.endswith("/api/open/v1/qa/requests"):
                state["requests_calls"] += 1
                if state["requests_calls"] == 1:
                    return HttpResponse(status=401, body={})
                return HttpResponse(status=200, body={"request": {"id": 9}})
            return HttpResponse(status=200, body={})

        transport = MockTransport(responder)
        summary = make_client(transport).submit({"external_id": "qq:1", "draft": {}})

        self.assertEqual(state["token_calls"], 2)
        self.assertEqual(state["requests_calls"], 2)
        self.assertEqual(summary["qa_request_id"], 9)
        # 首次请求带旧令牌，401 后强制刷新，重发带新令牌
        requests_calls = [call for call in transport.calls if call[1].endswith("/api/open/v1/qa/requests")]
        self.assertEqual(requests_calls[0][3]["Authorization"], "Bearer token-1")
        self.assertEqual(requests_calls[1][3]["Authorization"], "Bearer token-2")

    def test_double_401_is_permanent_failure(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            return HttpResponse(status=401, body={})

        transport = MockTransport(responder)

        with self.assertRaises(WritebackError) as ctx:
            make_client(transport).submit({"external_id": "qq:1", "draft": {}})

        self.assertEqual(ctx.exception.status, 401)

    def test_429_retries_with_backoff_then_succeeds(self) -> None:
        state = {"requests_calls": 0}

        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            if url.endswith("/api/open/v1/qa/requests"):
                state["requests_calls"] += 1
                if state["requests_calls"] <= 2:
                    return HttpResponse(status=429, body={})
                return HttpResponse(status=200, body={"request": {"id": 3}})
            return HttpResponse(status=200, body={})

        transport = MockTransport(responder)
        client = make_client(transport)

        with patch("main.time.sleep") as sleep_mock:
            summary = client.submit({"external_id": "qq:1", "draft": {}})

        self.assertEqual(state["requests_calls"], 3)
        self.assertEqual(summary["qa_request_id"], 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_5xx_retries_then_raises_when_exhausted(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            return HttpResponse(status=503, body={})

        transport = MockTransport(responder)
        client = make_client(transport)

        with patch("main.time.sleep"):
            with self.assertRaises(WritebackError) as ctx:
                client.submit({"external_id": "qq:1", "draft": {}})

        self.assertEqual(ctx.exception.status, 503)
        requests_calls = [call for call in transport.calls if call[1].endswith("/api/open/v1/qa/requests")]
        self.assertEqual(len(requests_calls), 3)

    def test_network_error_retries_then_raises(self) -> None:
        state = {"calls": 0}

        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            state["calls"] += 1
            raise ConnectionError("connection refused")

        transport = MockTransport(responder)

        with patch("main.time.sleep"):
            with self.assertRaises(WritebackError):
                make_client(transport).submit({"external_id": "qq:1", "draft": {}})

        self.assertEqual(state["calls"], 3)

    def test_400_is_permanent_without_retry(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            return HttpResponse(status=400, body={"error": "bad request"})

        transport = MockTransport(responder)

        with self.assertRaises(WritebackError) as ctx:
            make_client(transport).submit({"external_id": "qq:1", "draft": {}})

        self.assertEqual(ctx.exception.status, 400)
        requests_calls = [call for call in transport.calls if call[1].endswith("/api/open/v1/qa/requests")]
        self.assertEqual(len(requests_calls), 1)

    def test_missing_request_id_is_permanent_failure(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            return HttpResponse(status=200, body={"unexpected": True})

        transport = MockTransport(responder)

        with self.assertRaises(WritebackError):
            make_client(transport).submit({"external_id": "qq:1", "draft": {}})

    def test_answer_failure_does_not_block_archive_step(self) -> None:
        def responder(url: str, body: dict) -> HttpResponse:
            if url.endswith("/api/oauth/token"):
                return token_response()
            if url.endswith("/api/open/v1/qa/requests"):
                return HttpResponse(status=200, body={"request": {"id": 5}})
            if "/answer" in url:
                return HttpResponse(status=400, body={})
            return HttpResponse(status=201, body={"deduplicated": False})

        transport = MockTransport(responder)

        with patch("main.time.sleep"):
            summary = make_client(transport).submit({"external_id": "qq:1", "draft": {}})

        self.assertFalse(summary["answer_step_ok"])
        self.assertTrue(summary["archive_step_ok"])
        self.assertTrue(any(call[1].endswith("/api/open/v1/archive/entries") for call in transport.calls))


class SchedulerTests(unittest.TestCase):
    def test_disabled_scheduler_is_noop(self) -> None:
        scheduler = WritebackScheduler(submit_fn=None)

        self.assertFalse(scheduler.enqueue({"external_id": "qq:1"}))
        self.assertEqual(scheduler.pending_count(), 0)
        scheduler.start()
        scheduler.stop()

    def test_enqueue_delivers_payload_to_worker(self) -> None:
        received: list[dict] = []
        done = threading.Event()

        def submit(payload: dict) -> dict:
            received.append(payload)
            done.set()
            return {"qa_request_id": 1, "deduplicated": False}

        scheduler = WritebackScheduler(submit_fn=submit)
        scheduler.start()
        try:
            self.assertTrue(scheduler.enqueue({"external_id": "qq:1", "v": 1}))
            self.assertTrue(done.wait(2))
        finally:
            scheduler.stop()

        self.assertEqual(received, [{"external_id": "qq:1", "v": 1}])

    def test_full_queue_drops_newest_with_warning(self) -> None:
        scheduler = WritebackScheduler(submit_fn=lambda payload: {}, maxsize=2)
        # 不 start：任务留在队列里制造满队列状态

        self.assertTrue(scheduler.enqueue({"external_id": "a"}))
        self.assertTrue(scheduler.enqueue({"external_id": "b"}))
        with self.assertLogs("astrbot_plugin_xelatex_pdf", level="WARNING") as captured:
            self.assertFalse(scheduler.enqueue({"external_id": "c"}))

        self.assertEqual(scheduler.pending_count(), 2)
        self.assertTrue(any("队列已满" in record.getMessage() for record in captured.records))
        self.assertTrue(any("c" in record.getMessage() for record in captured.records))

    def test_default_capacity_matches_constant(self) -> None:
        scheduler = WritebackScheduler(submit_fn=lambda payload: {})

        self.assertEqual(scheduler._queue.maxsize, WRITEBACK_QUEUE_MAXSIZE)

    def test_stop_drains_pending_jobs_and_worker_exits(self) -> None:
        processed: list[str] = []
        release = threading.Event()

        def submit(payload: dict) -> dict:
            if not processed:
                release.wait(2)  # 首个任务阻塞，确保后续任务排队等待排空
            processed.append(payload["external_id"])
            return {}

        scheduler = WritebackScheduler(submit_fn=submit)
        scheduler.start()
        for external_id in ("a", "b", "c"):
            scheduler.enqueue({"external_id": external_id})
        time.sleep(0.05)
        release.set()
        scheduler.stop()

        self.assertEqual(processed, ["a", "b", "c"])
        self.assertEqual(scheduler.pending_count(), 0)

    def test_stop_is_idempotent(self) -> None:
        scheduler = WritebackScheduler(submit_fn=lambda payload: {})
        scheduler.stop()
        scheduler.start()
        scheduler.stop()
        scheduler.stop()


class WritebackIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """handle_event 成功分支的回写挂钩：失败隔离铁律的端到端验证。"""

    def _make_plugin(self, answer_text: str) -> Plugin:
        plugin = bare_plugin()
        plugin.data_dir = Path(tempfile.mkdtemp())
        for name in ("assets", "sources", "logs", "pdfs"):
            (plugin.data_dir / name).mkdir(parents=True, exist_ok=True)
        plugin.asset_dir = plugin.data_dir / "assets"
        plugin.source_dir = plugin.data_dir / "sources"
        plugin.log_dir = plugin.data_dir / "logs"
        plugin.pdf_dir = plugin.data_dir / "pdfs"
        plugin.export_dir = plugin.pdf_dir
        plugin._answer_text = answer_text
        return plugin

    def _patch_pipeline(self, plugin: Plugin):
        compile_result = CompileResult(
            success=True,
            workdir_path=None,
            pdf_path=plugin.pdf_dir / "a.pdf",
            log_path=None,
            stdout="",
            stderr="",
        )
        response = CommandResponse(
            success=True,
            message="编译成功",
            compile_result=compile_result,
            file_payload={"type": "file", "name": "a.pdf", "path": str(plugin.pdf_dir / "a.pdf"), "url": "http://host:8765/pdf/a.pdf"},
        )
        return (
            patch.object(plugin, "parse_message", return_value=ParsedMessage(text="/ai 求极限")),
            patch.object(plugin, "localize_attachments", side_effect=lambda parsed: parsed),
            patch.object(plugin, "generate_answer", return_value=(plugin._answer_text, "text-model")),
            patch.object(plugin, "build_tex_source", return_value="tex"),
            patch.object(plugin, "_persist_tex_source"),
            patch.object(plugin, "handle_command", return_value=response),
            patch.object(plugin, "append_history"),
        )

    async def test_compile_success_enqueues_writeback_payload(self) -> None:
        answer = (
            "[TITLE] 极限\n[SUMMARY] 提要。\n\n## 解答\n正文。\n\n"
            "[ARCHIVE]\n[STEM] 规范题干。\n[ANSWER] 1\n[TYPE] calc\n[DIFFICULTY] 2\n[TAGS] 极限,洛必达法则\n[/ARCHIVE]"
        )
        plugin = self._make_plugin(answer)
        enqueued: list[dict] = []
        delivered = threading.Event()

        def submit(payload: dict) -> dict:
            enqueued.append(payload)
            delivered.set()
            return {"qa_request_id": 1, "deduplicated": False}

        plugin.writeback_scheduler = WritebackScheduler(submit_fn=submit)
        plugin.writeback_scheduler.start()
        event = DummyEvent(message_id="555", user_id="10001")

        patches = self._patch_pipeline(plugin)
        for item in patches:
            item.start()
        try:
            response = await plugin.handle_event(event)
        finally:
            for item in patches:
                item.stop()
            plugin.writeback_scheduler.stop()

        self.assertTrue(response.success)
        self.assertTrue(delivered.wait(2), "worker 未在限时内消费回写任务")
        self.assertEqual(len(enqueued), 1)
        payload = enqueued[0]
        self.assertEqual(payload["external_id"], "qq:555")
        self.assertEqual(payload["question_text"], "求极限")
        self.assertEqual(payload["qq"], "10001")
        self.assertEqual(payload["answer_text"], "1")
        self.assertIn("正文", payload["analysis_text"])
        self.assertEqual(payload["meta"]["model"], "text-model")
        self.assertEqual(payload["meta"]["pdf_url"], "http://host:8765/pdf/a.pdf")
        self.assertEqual(payload["draft"]["stem"], "规范题干。")
        self.assertEqual(payload["draft"]["type"], "calc")
        self.assertEqual(payload["draft"]["difficulty"], 2)
        self.assertEqual(payload["draft"]["tags"], ["极限", "洛必达法则"])

    async def test_no_archive_block_skips_writeback(self) -> None:
        plugin = self._make_plugin("[TITLE] 闲聊\n[SUMMARY] 不是题目。\n\n正文。")
        scheduler = WritebackScheduler(submit_fn=lambda payload: {})
        plugin.writeback_scheduler = scheduler

        patches = self._patch_pipeline(plugin)
        for item in patches:
            item.start()
        try:
            response = await plugin.handle_event(DummyEvent(message_id="1"))
        finally:
            for item in patches:
                item.stop()

        self.assertTrue(response.success)
        # 无归档块 → 不入队（队列与消费端都应为空）
        self.assertEqual(scheduler.pending_count(), 0)

    async def test_compile_failure_does_not_enqueue(self) -> None:
        answer = "正文\n[ARCHIVE]\n[STEM] 题干\n[/ARCHIVE]"
        plugin = self._make_plugin(answer)
        scheduler = WritebackScheduler(submit_fn=lambda payload: {})
        plugin.writeback_scheduler = scheduler
        failed_response = CommandResponse(
            success=False,
            message="编译失败",
            compile_result=CompileResult(success=False, workdir_path=None, pdf_path=None, log_path=None, stdout="", stderr=""),
        )

        with (
            patch.object(plugin, "parse_message", return_value=ParsedMessage(text="/ai 题目")),
            patch.object(plugin, "localize_attachments", side_effect=lambda parsed: parsed),
            patch.object(plugin, "generate_answer", return_value=(answer, "text-model")),
            patch.object(plugin, "build_tex_source", return_value="tex"),
            patch.object(plugin, "_persist_tex_source"),
            patch.object(plugin, "handle_command", return_value=failed_response),
        ):
            response = await plugin.handle_event(DummyEvent(message_id="1"))

        self.assertFalse(response.success)
        self.assertEqual(scheduler.pending_count(), 0)

    async def test_writeback_exception_does_not_change_response(self) -> None:
        answer = "正文\n[ARCHIVE]\n[STEM] 题干\n[/ARCHIVE]"
        plugin = self._make_plugin(answer)
        plugin.writeback_scheduler = WritebackScheduler(submit_fn=lambda payload: {})

        patches = self._patch_pipeline(plugin)
        for item in patches:
            item.start()
        try:
            with patch.object(plugin, "_build_writeback_payload", side_effect=RuntimeError("payload boom")):
                response = await plugin.handle_event(DummyEvent(message_id="1"))
        finally:
            for item in patches:
                item.stop()

        self.assertTrue(response.success)
        self.assertEqual(response.message, "编译成功")

    async def test_scheduler_enqueue_failure_does_not_change_response(self) -> None:
        answer = "正文\n[ARCHIVE]\n[STEM] 题干\n[/ARCHIVE]"
        plugin = self._make_plugin(answer)

        class BrokenScheduler:
            def enqueue(self, payload: dict) -> bool:
                raise RuntimeError("enqueue boom")

        plugin.writeback_scheduler = BrokenScheduler()

        patches = self._patch_pipeline(plugin)
        for item in patches:
            item.start()
        try:
            response = await plugin.handle_event(DummyEvent(message_id="1"))
        finally:
            for item in patches:
                item.stop()

        self.assertTrue(response.success)
        self.assertEqual(response.message, "编译成功")


class AssetRouteTests(unittest.TestCase):
    def test_public_file_url_routes_asset_category(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = plugin_with_dirs(Path(temp_dir))
            asset_file = plugin.asset_dir / "question.png"

            self.assertIn("/asset/question.png", plugin._public_file_url(asset_file))

    def test_file_handler_serves_asset_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = plugin_with_dirs(Path(temp_dir))
            (plugin.asset_dir / "question.png").write_bytes(b"fake-image-bytes")
            server = ThreadingHTTPServer(("127.0.0.1", 0), plugin._build_file_handler())
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/asset/question.png", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"fake-image-bytes")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_question_image_urls_only_include_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = plugin_with_dirs(Path(temp_dir))
            image_path = plugin.asset_dir / "image_abc.png"
            image_path.write_bytes(b"x")
            parsed = ParsedMessage(
                text="/ai 题目",
                images=[
                    MessageAttachment(kind="image", path=str(image_path)),
                    MessageAttachment(kind="image", url="https://remote.example.com/x.png"),
                ],
            )

            urls = plugin._build_question_image_urls(parsed)

            self.assertEqual(len(urls), 1)
            self.assertIn("/asset/image_abc.png", urls[0])


class ArchiveDraftTests(unittest.TestCase):
    def test_draft_falls_back_to_question_and_summary(self) -> None:
        plugin = bare_plugin()
        structured = {
            "title": "",
            "summary": "提要答案",
            "body": "解析正文",
            "archive": {"present": True, "stem": "", "answer": "", "type": "answer", "difficulty": 3, "tags": []},
        }

        draft = plugin._build_archive_draft(structured, "原始提问", ["http://host/asset/a.png"])

        self.assertEqual(draft["stem"], "原始提问")
        self.assertEqual(draft["answer"], "提要答案")
        self.assertEqual(draft["analysis"], "解析正文")
        self.assertEqual(draft["images"], ["http://host/asset/a.png"])

    def test_draft_prefers_archive_fields(self) -> None:
        plugin = bare_plugin()
        structured = {
            "title": "",
            "summary": "提要",
            "body": "正文",
            "archive": {"present": True, "stem": "规范题干", "answer": "B", "type": "single", "difficulty": 4, "tags": ["集合"]},
        }

        draft = plugin._build_archive_draft(structured, "原始提问", [])

        self.assertEqual(draft["stem"], "规范题干")
        self.assertEqual(draft["answer"], "B")
        self.assertEqual(draft["type"], "single")
        self.assertEqual(draft["difficulty"], 4)
        self.assertEqual(draft["tags"], ["集合"])


if __name__ == "__main__":
    unittest.main()
