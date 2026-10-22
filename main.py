from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass, field
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import queue
import random
import re
import socket
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional


PLUGIN_VERSION = "0.7.0"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_OCR_IMAGE_BYTES = 15 * 1024 * 1024
MAX_OCR_PAGES = 12
NETWORK_TIMEOUT_SECONDS = 30
MODEL_TIMEOUT_SECONDS = 120
VISION_TIMEOUT_SECONDS = 300
# 图片方向判定只回答一个编号，超时要远短于 OCR，避免上游卡住拖慢整次问答
ORIENTATION_TIMEOUT_SECONDS = 60
# 一条消息内所有图片的方向判定共用这一个时间预算，上游失败一次即停止后续判定
ORIENTATION_BUDGET_SECONDS = 90
ORIENTATION_THUMBNAIL_SIZE = 768
# 超过该像素数不在进程内解码（小体积的纯色大图也可能解出 GB 级内存）
ORIENTATION_MAX_PIXELS = 40_000_000
# 长截图缩到 768 后字小到读不清，模型只能瞎猜，宽高比超过它就不做模型判定
ORIENTATION_MAX_ASPECT = 2.5
# 实测「问文字顶部朝哪边」在 7 个样本里只对 2 个；「四个旋转版本里挑正的那张」8/8 全对
ORIENTATION_PROMPT = (
    "下面 4 张图是同一张图片的 4 种旋转版本，依次编号 1、2、3、4。"
    "请找出其中文字方向正常（文字正立、可从左到右横向阅读）的那一张。只输出编号数字。"
)
PROCESS_TIMEOUT_SECONDS = 120
STALE_WORKDIR_MAX_AGE_SECONDS = 24 * 60 * 60
# 多题并行：一条指令最多拆几题由平台被动回复次数决定（每题一份 PDF 占一次回复）。
# QQ 官方机器人：群聊同一条消息最多被动回复 5 次（5 分钟内），单聊 4 次（60 分钟内）
QQ_OFFICIAL_GROUP_REPLY_SLOTS = 5
QQ_OFFICIAL_C2C_REPLY_SLOTS = 4
DEFAULT_REPLY_SLOTS = 5
MULTI_QUESTION_RANGE_LIMIT = 20
# 指令里出现这些词说明用户要的是把几题放在一起讲，不拆分
MULTI_QUESTION_OPT_OUT_WORDS = ("一起", "合并", "一份", "综合", "对比", "比较", "联系", "关系", "区别", "异同")
# 「利用第8题的结论解决第10题」里的第8题是引用，不是要作答的题
MULTI_QUESTION_REFERENCE_PREFIXES = ("利用", "根据", "参考", "参照", "结合", "借助", "由", "用", "按")
_QUESTION_TOKEN = r"(?:\d{1,3}|[一二三四五六七八九十两]{1,3})"
_QUESTION_RANGE_SEPARATORS = "-–~～—至到"
# 题号组：第8、10题 / 8-11题 / 第八到十题。数字前不能紧跟数字或小数点（「2.1题」不是第 1 题）
QUESTION_GROUP_PATTERN = re.compile(
    r"(第?)(?<![\d.．])(" + _QUESTION_TOKEN
    + r"(?:\s*(?:[、,，]|和|及|与|跟|[-–~～—至到])\s*第?" + _QUESTION_TOKEN + r")*)\s*道?题"
)
QUESTION_GROUP_SEPARATOR = re.compile(r"\s*([、,，]|和|及|与|跟|[-–~～—至到])\s*")
QUESTION_SPLIT_PROMPT = (
    "你是题目定位助手，只负责从题目图片的识别文本里找出指定题号的题目，不做解答。"
    "对每个要找的题号，先单独一行写 [Q 题号]，下一行起原样抄录该题完整题干："
    "保留原文措辞、数学公式（LaTeX）、小问 (1)(2) 与选项；"
    "若几道题共用前面的条件（如「设……，回答下列问题」），把共用条件一并抄进每道相关的题。"
    "文本里找不到的题号，统一写在最后一行：[MISSING] 题号, 题号。"
    "不要输出其他任何内容，不要编造题干。"
)
QUESTION_SPLIT_MARKER = re.compile(r"^[ \t]*\[Q\s*第?\s*(\d{1,3})\s*题?\][ \t]*", re.MULTILINE)
QUESTION_SPLIT_MISSING_LINE = re.compile(r"^[ \t]*\[MISSING\].*$", re.MULTILINE)
# 文本模型遇到限流/过载时的退避重试：并行作答会成倍放大上游 429，
# 不退避就会直接降级到备用模型。退避时间所有并发请求共享
THROTTLED_STATUS_CODES = frozenset({429, 502, 503, 504, 529})
TEXT_MODEL_THROTTLE_RETRIES = 2
TEXT_MODEL_MAX_BACKOFF_SECONDS = 20
# 定期清理：插件产物按保留天数删除；被题库引用（热链）的题图永不删除
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
PROTECTED_ASSETS_FILENAME = "qbk_referenced_assets.json"
# 答疑归档回写题库（旁路链路）相关常量
WRITEBACK_QUEUE_MAXSIZE = 64
WRITEBACK_TIMEOUT_SECONDS = 15
WRITEBACK_MAX_ATTEMPTS = 3
WRITEBACK_RETRY_BASE_DELAY = 1.0
TOKEN_EXPIRY_MARGIN_SECONDS = 60
ARCHIVE_TYPE_WHITELIST = {"single", "multiple", "blank", "proof", "calc", "answer"}
ARCHIVE_TAG_LIMIT = 20
WRITEBACK_LOG_PREFIX = "[qbk]"

# 数学片段识别：行间 \[...\] / $$...$$ 与行内 \(...\) / $...$。
# `_tex_inline` 用它暂存公式以免被文本转义破坏；编译失败净化器用它定位
# 需要补 \% 的数学区间（数学模式里裸 % 是注释符，会吞掉闭合定界符）。
MATH_SPAN_PATTERN = re.compile(
    r"\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$\$[\s\S]*?\$\$|\$[^$\n]+\$"
)

logger = logging.getLogger("astrbot_plugin_xelatex_pdf")

# 文本模型退避时间点（time.monotonic）：并发作答线程共享，一个请求被限流，所有请求一起等
_TEXT_MODEL_COOLDOWN_LOCK = threading.Lock()
_text_model_cool_until = 0.0
# 题库引用清单的读改写锁（回写入队与定期清理可能并发）
_PROTECTED_ASSETS_LOCK = threading.Lock()


class ChatCompletionHTTPError(RuntimeError):
    """模型接口 HTTP 错误；消息格式与原先的 RuntimeError 一致，额外带状态码与 Retry-After。"""

    def __init__(self, status: int, body: str, retry_after: Optional[int] = None) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.retry_after = retry_after


def _parse_retry_after(value: Any) -> Optional[int]:
    """Retry-After 只认 1-120 秒的整数；日期格式与异常值一律忽略。"""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if 1 <= seconds <= 120 else None

try:  # pragma: no cover - 运行在 AstrBot 环境时会走真实导入
    from astrbot.api import AstrBotConfig
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
    from astrbot.api.message_components import File
except Exception:  # pragma: no cover - 当前开发环境下使用兼容占位
    class _Filter:
        def command(self, *_args: Any, **_kwargs: Any):
            def decorator(func: Any) -> Any:
                return func

            return decorator

    filter = _Filter()

    class Star:  # type: ignore[no-redef]
        def __init__(self, context: Any = None) -> None:
            self.context = context

    class Context:  # type: ignore[no-redef]
        pass

    class AstrMessageEvent:  # type: ignore[no-redef]
        pass

    class AstrBotConfig:  # type: ignore[no-redef]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._data = {}

        def get(self, key: str, default: Any = None) -> Any:
            return self._data.get(key, default)

    class File:  # type: ignore[no-redef]
        def __init__(self, name: str = "", url: str = "", file: str = "") -> None:
            self.name = name
            self.url = url
            self.file = file

    def register(*_args: Any, **_kwargs: Any):
        def decorator(cls: Any) -> Any:
            return cls

        return decorator


@dataclass(slots=True)
class TexEnvironment:
    xelatex: Optional[str]
    latexmk: Optional[str]
    pdflatex: Optional[str]

    @property
    def available(self) -> bool:
        return self.xelatex is not None


@dataclass(slots=True)
class CompileResult:
    success: bool
    workdir_path: Optional[Path]
    pdf_path: Optional[Path]
    log_path: Optional[Path]
    stdout: str
    stderr: str


@dataclass(slots=True)
class MessageAttachment:
    kind: str
    name: str = ""
    url: str = ""
    path: str = ""
    raw: Any = None


@dataclass(slots=True)
class OrientationBudget:
    """一条消息内方向判定的共享预算：总时限 + 上游一旦失败就熔断。"""

    deadline: float
    failed: bool = False

    def timeout(self) -> int:
        if self.failed:
            return 0
        return int(min(ORIENTATION_TIMEOUT_SECONDS, self.deadline - time.monotonic()))


@dataclass(slots=True)
class ParsedMessage:
    text: str = ""
    reply_text: str = ""
    images: list[MessageAttachment] = field(default_factory=list)
    pdfs: list[MessageAttachment] = field(default_factory=list)
    raw_components: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class CommandRequest:
    command: str
    args: list[str]
    raw_text: str = ""


@dataclass(slots=True)
class CommandResponse:
    success: bool
    message: str
    compile_result: Optional[CompileResult] = None
    file_payload: Optional[dict[str, Any]] = None
    # 编译失败自动净化重试成功时携带修复后的源码，供上层同步会话存档与落盘版本
    repaired_source: Optional[str] = None


@dataclass(slots=True)
class ModelDirective:
    """「指定模型」指令的解析结果。

    persistent=True：「切换到X模型」，持久改全局默认；
    persistent=False：「用/使用X模型回答」，只对单题生效——
    question 非空时直接用它回答本题，为空时作为本会话下一题的一次性预设。
    """

    model: str
    persistent: bool
    question: str = ""


@dataclass(slots=True)
class AnswerJob:
    """一题的作答参数。单题与多题共用同一条作答→编译→存档→回写链路。"""

    command_text: str
    question_text: str
    # 多题模式：共享的附件上下文（OCR 只跑一次）与「只答第 N 题」的聚焦说明
    attachment_context: Optional[str] = None
    focus: str = ""
    # 多题模式：同一条 QQ 消息拆出多条回写记录，幂等键需按题区分
    external_id_suffix: str = ""
    # 多题模式：「第N题」，用作 PDF 标题前缀与失败消息前缀
    label: str = ""
    # 多题模式：副标题「围绕问题：…」用用户原话；题干带 LaTeX，放进副标题会被当纯文本转义
    subtitle_question: str = ""


@dataclass(slots=True)
class SessionMemory:
    session_id: str
    rounds: list[dict[str, str]] = field(default_factory=list)

    def append(self, user_text: str, assistant_text: str, limit: int = 5) -> None:
        self.rounds.append({"user": user_text, "assistant": assistant_text})
        if len(self.rounds) > limit:
            self.rounds = self.rounds[-limit:]

    def clear(self) -> None:
        self.rounds.clear()


@dataclass(slots=True)
class HttpResponse:
    """传输层统一响应：HTTP 错误（4xx/5xx）也转成本对象返回，不抛异常。"""

    status: int
    body: dict[str, Any]


class WritebackError(RuntimeError):
    """回写链路的永久性失败。携带 HTTP status（网络层失败时为 None），
    供上层记录日志与重试决策；可重试情形（网络错/429/5xx）在客户端内部消化，
    抛到调用方时一律代表「不应再重试」。"""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class WritebackJob:
    """回写队列元素：入队时刻冻结全部字段，worker 消费时不再依赖外部状态。"""

    external_id: str
    payload: dict[str, Any]
    enqueued_at: float


class HttpTransport:
    """传输层当前实现：标准库 urllib，同步阻塞。

    与 `_post_chat_completion` 的关键差异：HTTPError 被捕获并读取正文，
    转成带 status 的 HttpResponse 返回（而不是直接抛 RuntimeError）——
    这是上层重试矩阵能区分 401/429/5xx/4xx 的前提。网络层异常原样上抛。
    升级传输层时整体替换本类即可，签名保持不变。
    """

    user_agent = f"astrbot-plugin-xelatex-pdf/{PLUGIN_VERSION}"

    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> HttpResponse:
        """POST JSON 请求；4xx/5xx 转为 HttpResponse 返回，网络异常上抛。"""
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged_headers = {"Content-Type": "application/json", "User-Agent": self.user_agent}
        merged_headers.update(headers)
        return self._perform(url, encoded, merged_headers, timeout)

    def post_form(self, url: str, form: dict[str, str], headers: dict[str, str], timeout: int) -> HttpResponse:
        """application/x-www-form-urlencoded 请求（令牌端点专用）。"""
        encoded = urllib.parse.urlencode(form).encode("utf-8")
        merged_headers = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.user_agent}
        merged_headers.update(headers)
        return self._perform(url, encoded, merged_headers, timeout)

    def _perform(self, url: str, encoded_body: bytes, headers: dict[str, str], timeout: int) -> HttpResponse:
        request = urllib.request.Request(url, data=encoded_body, method="POST")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(status=response.status, body=self._decode_body(response.read()))
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 不抛异常：读取错误正文尽力解析为 JSON，交给上层决策
            try:
                raw_body = exc.read()
            except Exception:
                raw_body = b""
            return HttpResponse(status=exc.code, body=self._decode_body(raw_body))

    @staticmethod
    def _decode_body(raw: bytes) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}


class QaWritebackClient:
    """题库开放 API 回写客户端：令牌缓存 + 三步 POST + 单请求重试。

    线程安全说明：当前仅被单个 worker 线程调用；令牌状态仍由
    threading.Lock 保护（双重检查），为多 worker 升级预留。
    """

    def __init__(
        self,
        transport: HttpTransport,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout: int = WRITEBACK_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._base_url = (base_url or "").rstrip("/")
        self._client_id = client_id or ""
        self._client_secret = client_secret or ""
        self._timeout = timeout
        self._token: str = ""
        self._token_deadline: float = 0.0
        self._token_lock = threading.Lock()

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """执行 ①提问 ②答复 ③归档 全链路，返回摘要（供日志）。

        部分成功语义：三步不是事务。① 失败则整任务终止（抛
        WritebackError）；②/③ 失败只记日志不重跑——重试只发生在单个
        HTTP 请求层面，避免答复步骤产生重复追加行。
        """
        external_id = str(payload.get("external_id", ""))
        request_body = {
            "external_id": external_id,
            "channel": "qq_bot",
            "channel_subject": payload.get("qq", ""),
            "qq": payload.get("qq", ""),
            "question_text": payload.get("question_text", ""),
            "question_images": list(payload.get("question_images") or []),
            "meta": dict(payload.get("meta") or {}),
        }
        request_result = self._post_with_auth("/api/open/v1/qa/requests", request_body)
        request_record = request_result.get("request") if isinstance(request_result.get("request"), dict) else None
        qa_request_id = request_record.get("id") if isinstance(request_record, dict) else None
        if qa_request_id is None:
            raise WritebackError("qa/requests 响应缺少 request.id", status=None)
        deduplicated = bool(request_result.get("deduplicated"))

        answer_step_ok = False
        try:
            self._post_with_auth(
                f"/api/open/v1/qa/requests/{qa_request_id}/answer",
                {
                    "answer_text": payload.get("answer_text") or "（模型未给出答案）",
                    "analysis_text": payload.get("analysis_text", ""),
                    "external_ref": (payload.get("meta") or {}).get("pdf_url", ""),
                },
            )
            answer_step_ok = True
        except WritebackError as exc:
            logger.warning(
                "%s 答复步骤失败（不影响归档步骤） external_id=%s status=%s err=%s",
                WRITEBACK_LOG_PREFIX, external_id, exc.status, exc,
            )

        archive_step_ok = False
        try:
            self._post_with_auth(
                "/api/open/v1/archive/entries",
                {
                    "qa_request_id": qa_request_id,
                    "external_id": external_id,
                    "draft": dict(payload.get("draft") or {}),
                },
            )
            archive_step_ok = True
        except WritebackError as exc:
            logger.warning(
                "%s 归档步骤失败 external_id=%s status=%s err=%s",
                WRITEBACK_LOG_PREFIX, external_id, exc.status, exc,
            )

        return {
            "qa_request_id": qa_request_id,
            "deduplicated": deduplicated,
            "answer_step_ok": answer_step_ok,
            "archive_step_ok": archive_step_ok,
        }

    def _get_token(self, force_refresh: bool = False) -> str:
        """取缓存令牌；过期（或强制刷新）时在锁内重新申请，双重检查避免重复刷新。"""
        if not force_refresh and self._token and time.monotonic() < self._token_deadline:
            return self._token
        with self._token_lock:
            if not force_refresh and self._token and time.monotonic() < self._token_deadline:
                return self._token
            token, expires_in = self._request_token()
            self._token = token
            self._token_deadline = time.monotonic() + max(float(expires_in), 0.0) - TOKEN_EXPIRY_MARGIN_SECONDS
            logger.info("%s 令牌刷新成功（expires_in=%ss）", WRITEBACK_LOG_PREFIX, expires_in)
            return self._token

    def _request_token(self) -> tuple[str, float]:
        """client_credentials 换取令牌；按重试矩阵处理瞬时故障，最终失败抛 WritebackError。"""
        credentials = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode("utf-8")).decode("ascii")
        url = f"{self._base_url}/api/oauth/token"
        headers = {"Authorization": f"Basic {credentials}"}
        form = {"grant_type": "client_credentials"}
        last_error: Optional[WritebackError] = None
        for attempt in range(1, WRITEBACK_MAX_ATTEMPTS + 1):
            try:
                response = self._transport.post_form(url, form, headers, self._timeout)
            except Exception as exc:  # 网络层异常：可重试
                last_error = WritebackError(f"令牌请求网络错误: {exc}", status=None)
            else:
                if response.status in (200, 201):
                    token = response.body.get("access_token")
                    expires_in = response.body.get("expires_in")
                    if isinstance(token, str) and token and expires_in is not None:
                        try:
                            return token, float(expires_in)
                        except (TypeError, ValueError):
                            last_error = WritebackError("令牌响应 expires_in 非法", status=response.status)
                    else:
                        # 响应体缺字段：重试无意义，永久失败
                        raise WritebackError("令牌响应缺少 access_token/expires_in", status=response.status)
                elif response.status in (429,) or 500 <= response.status < 600:
                    last_error = WritebackError("令牌请求瞬时失败", status=response.status)
                else:
                    raise WritebackError("令牌请求被拒绝（凭据或权限问题）", status=response.status)
            if attempt < WRITEBACK_MAX_ATTEMPTS:
                time.sleep(self._retry_delay(attempt))
        assert last_error is not None
        raise last_error

    def _post_with_auth(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """带 Bearer 头的 POST；401 → 强制刷新令牌重发一次（仅一次）。"""
        url = f"{self._base_url}{path}"
        response = self._post_with_retry(url, {"Authorization": f"Bearer {self._get_token()}"}, body)
        if response.status == 401:
            logger.info("%s 业务请求收到 401，强制刷新令牌重发一次 path=%s", WRITEBACK_LOG_PREFIX, path)
            response = self._post_with_retry(url, {"Authorization": f"Bearer {self._get_token(force_refresh=True)}"}, body)
        if response.status in (200, 201):
            return response.body
        raise WritebackError(f"请求失败 path={path}", status=response.status)

    def _post_with_retry(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> HttpResponse:
        """单请求重试矩阵：网络错/429/5xx 退避重发（≤3 次）；401 交给调用方续期；
        其余 4xx 永久失败立即抛出。返回的 HttpResponse 由调用方判定成功与否。"""
        last_error: Optional[WritebackError] = None
        for attempt in range(1, WRITEBACK_MAX_ATTEMPTS + 1):
            try:
                response = self._transport.post_json(url, body, headers, self._timeout)
            except Exception as exc:  # URLError / socket.timeout / ConnectionError 等：可重试
                last_error = WritebackError(f"网络错误: {exc}", status=None)
            else:
                if response.status == 401:
                    return response
                if response.status in (429,) or 500 <= response.status < 600:
                    last_error = WritebackError("服务端瞬时失败", status=response.status)
                else:
                    # 2xx 成功，或 400/403/404 等永久失败：都不重试
                    return response
            if attempt < WRITEBACK_MAX_ATTEMPTS:
                time.sleep(self._retry_delay(attempt))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        """指数退避 + 抖动：约 1s、2s……"""
        return WRITEBACK_RETRY_BASE_DELAY * 2 ** (attempt - 1) + random.uniform(0, 0.5)


class WritebackScheduler:
    """回写调度/队列层：有界队列 + 单 daemon worker 线程。

    只依赖「一个可调用的 submit_fn(payload) -> dict」，不感知
    HTTP/令牌/题库契约——那是 QaWritebackClient 的事。
    submit_fn 为 None（未配置/开关关闭）时，enqueue 静默 no-op。
    """

    def __init__(self, submit_fn: Optional[Any] = None, maxsize: int = WRITEBACK_QUEUE_MAXSIZE) -> None:
        self._submit_fn = submit_fn
        self._queue: "queue.Queue[Optional[WritebackJob]]" = queue.Queue(maxsize=maxsize)
        self._workers: list[threading.Thread] = []
        self._started = False

    @property
    def enabled(self) -> bool:
        return self._submit_fn is not None

    def enqueue(self, payload: dict[str, Any]) -> bool:
        """同步、非阻塞、永不抛错。入队成功返回 True；
        队列满丢弃返回 False（已记 WARNING）；未启用返回 False。"""
        if self._submit_fn is None:
            return False
        try:
            external_id = str(payload.get("external_id", ""))
            job = WritebackJob(external_id=external_id, payload=payload, enqueued_at=time.time())
            self._queue.put_nowait(job)
        except queue.Full:
            logger.warning(
                "%s 回写队列已满，丢弃本次任务 external_id=%s",
                WRITEBACK_LOG_PREFIX, payload.get("external_id", ""),
            )
            return False
        except Exception as exc:  # 兜底：生产端任何异常都不允许上抛
            logger.warning("%s 入队异常（已忽略） err=%s", WRITEBACK_LOG_PREFIX, exc)
            return False
        logger.debug("%s 回写任务已入队 external_id=%s pending=%d", WRITEBACK_LOG_PREFIX, external_id, self._queue.qsize())
        return True

    def start(self) -> None:
        """幂等启动 worker 线程；未启用时不做任何事。"""
        if self._submit_fn is None or self._started:
            return
        self._started = True
        worker = threading.Thread(target=self._worker_loop, name="qbk-writeback-worker", daemon=True)
        worker.start()
        self._workers.append(worker)

    def stop(self) -> None:
        """发送哨兵，worker 排空队列内剩余任务后退出；限时 join 5s，超时不等待。"""
        if not self._started:
            return
        self._started = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:  # 理论上哨兵入队不会满（worker 在消费），兜底不阻塞
            pass
        for worker in self._workers:
            if worker.is_alive():
                worker.join(5)
        self._workers.clear()

    def pending_count(self) -> int:
        """观测用：队列中待处理任务数。"""
        return self._queue.qsize()

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:  # 哨兵：排空后退出
                break
            try:
                summary = self._submit_fn(job.payload)
                logger.info(
                    "%s 回写成功 external_id=%s qa_request_id=%s deduplicated=%s",
                    WRITEBACK_LOG_PREFIX,
                    job.external_id,
                    summary.get("qa_request_id") if isinstance(summary, dict) else None,
                    summary.get("deduplicated") if isinstance(summary, dict) else None,
                )
            except Exception as exc:  # 兜底：任何异常只记日志，绝不上抛
                logger.warning("%s 回写失败 external_id=%s err=%s", WRITEBACK_LOG_PREFIX, job.external_id, exc)


@register("astrbot_plugin_xelatex_pdf", "CodeBuddy", "使用 XeLaTeX 生成 PDF 的 AstrBot 插件", PLUGIN_VERSION)
class Plugin(Star):
    """将多模态问答结果渲染为 XeLaTeX PDF 的 AstrBot 插件。"""

    command_aliases = {
        "/ai",
        "/ask",
        "/解答",
        "/解析",
        "/查模型",
        "查模型",
        "/ai_source",
        "ai_source",
        "/源文件",
        "源文件",
    }

    def __init__(self, context: Context | None = None, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config
        self.name = "astrbot_plugin_xelatex_pdf"
        self.initialized = False
        self.data_dir = Path("/AstrBot/data/pdf_reports") if Path("/AstrBot/data").exists() else Path(tempfile.gettempdir()) / "astrbot_plugin_xelatex_pdf"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 分类目录：附件 / LaTeX 源码 / 编译日志 / PDF 各自独立，存储结构清晰有序
        self.asset_dir = self.data_dir / "assets"
        self.source_dir = self.data_dir / "sources"
        self.log_dir = self.data_dir / "logs"
        self.pdf_dir = self.data_dir / "pdfs"
        for category_dir in (self.asset_dir, self.source_dir, self.log_dir, self.pdf_dir):
            category_dir.mkdir(parents=True, exist_ok=True)
        # export_dir 指向 PDF 分类目录，保持与既有文件回传链路兼容
        self.export_dir = self.pdf_dir
        self.history_file = self.data_dir / "chat_history.json"
        self.max_history_rounds = 5
        self.chat_history = self._load_history()
        self.last_tex_source_by_session: dict[str, str] = {}
        self.last_pdf_by_session: dict[str, Path] = {}
        # 记录每个会话最近一次生成的命名基名，保证源码/日志/PDF 三类文件同名对应
        self.last_base_name_by_session: dict[str, str] = {}
        # 「用X模型回答」单次指定：按会话暂存，下一题消费一次即清除，不碰全局默认
        self.pending_model_by_session: dict[str, str] = {}
        self.text_api_url = self._get_config_value("text_api_url", "XELATEX_TEXT_API_URL", "").rstrip("/")
        self.text_api_key = self._get_config_value("text_api_key", "XELATEX_TEXT_API_KEY", "")
        self.ocr_api_url = self._get_config_value("ocr_api_url", "XELATEX_OCR_API_URL", self.text_api_url).rstrip("/")
        self.ocr_api_key = self._get_config_value("ocr_api_key", "XELATEX_OCR_API_KEY", self.text_api_key)
        self.active_model_name = self._get_config_value("llm_model", "XELATEX_MODEL_NAME", "default")
        fallback_models = self._get_config_value("fallback_models", "XELATEX_AVAILABLE_MODELS", "")
        self.available_models = [item.strip() for item in fallback_models.split(",") if item.strip()]
        self._remember_available_model(self.active_model_name)
        self.vision_model_name = self._get_config_value("llm_vision_model", "XELATEX_VISION_MODEL", "")
        self.fallback_vision_models = [
            item.strip()
            for item in self._get_config_value("fallback_vision_models", "XELATEX_FALLBACK_VISION_MODELS", "").split(",")
            if item.strip()
        ]
        self.tex_environment = TexEnvironment(
            xelatex=shutil.which("xelatex"),
            latexmk=shutil.which("latexmk"),
            pdflatex=shutil.which("pdflatex"),
        )
        self.file_server_port = int(self._get_config_value("file_server_port", "XELATEX_FILE_SERVER_PORT", "8765") or 8765)
        self.file_server_host = "0.0.0.0"
        # 回传下载链接使用的 host；留空时自动探测容器主机名。
        # 机器人与插件同宿主机但插件在容器内时，应配置为 127.0.0.1 并在 Docker 映射端口。
        self.public_file_host = self._get_config_value("file_server_host", "XELATEX_FILE_SERVER_HOST", "")
        self._file_server: ThreadingHTTPServer | None = None
        self._file_server_thread: threading.Thread | None = None
        self._load_panel_settings()
        self._cleanup_task: Optional[asyncio.Task] = None
        # 答疑归档回写题库：读配置并做启用判定。四项任一缺失/开关关闭时
        # 挂空转调度器（enqueue no-op），不产生启动错误——回写是旁路。
        self.qbk_base_url = str(self._get_config_value("qbk_base_url", "XELATEX_QBK_BASE_URL", "http://172.17.0.1:3000") or "").rstrip("/")
        self.qbk_client_id = str(self._get_config_value("qbk_client_id", "XELATEX_QBK_CLIENT_ID", "") or "")
        self.qbk_client_secret = str(self._get_config_value("qbk_client_secret", "XELATEX_QBK_CLIENT_SECRET", "") or "")
        self.archive_enabled = str(self._get_config_value("archive_enabled", "XELATEX_ARCHIVE_ENABLED", "false") or "").strip().lower() in {"1", "true", "yes", "on"}
        self.writeback_scheduler = WritebackScheduler(submit_fn=None)
        if self.archive_enabled and self.qbk_base_url and self.qbk_client_id and self.qbk_client_secret:
            writeback_client = QaWritebackClient(HttpTransport(), self.qbk_base_url, self.qbk_client_id, self.qbk_client_secret)
            self.writeback_scheduler = WritebackScheduler(submit_fn=writeback_client.submit)
            self.writeback_scheduler.start()
            logger.info("%s 归档回写已启用 base_url=%s", WRITEBACK_LOG_PREFIX, self.qbk_base_url)
        else:
            logger.info("%s 归档回写未启用（开关关闭或凭据未配置）", WRITEBACK_LOG_PREFIX)
        self._start_file_server()

    def _load_panel_settings(self) -> None:
        """读取 WebUI 面板上的开关与数值。面板只做类型转换，范围在这里夹住。

        面板保存后 AstrBot 会热重载插件（先 terminate 旧实例再重新构造），这里随之重读。
        """
        self.multi_question_enabled = self._config_bool("multi_question_enabled", "XELATEX_MULTI_QUESTION_ENABLED", True)
        self.orientation_correction_enabled = self._config_bool("orientation_correction_enabled", "XELATEX_ORIENTATION_CORRECTION_ENABLED", True)
        # 回复上限至少 2：多题模式缺题/超额时要 1 条说明 + 至少 1 份 PDF
        self.reply_limit_group = self._config_int("reply_limit_group", "XELATEX_REPLY_LIMIT_GROUP", QQ_OFFICIAL_GROUP_REPLY_SLOTS, 2, 20)
        self.reply_limit_private = self._config_int("reply_limit_private", "XELATEX_REPLY_LIMIT_PRIVATE", QQ_OFFICIAL_C2C_REPLY_SLOTS, 2, 20)
        self.reply_limit_other = self._config_int("reply_limit_other", "XELATEX_REPLY_LIMIT_OTHER", DEFAULT_REPLY_SLOTS, 2, 20)
        self.file_retention_days = self._config_int("file_retention_days", "XELATEX_FILE_RETENTION_DAYS", 7, 0, 3650)
        self.cleanup_interval_hours = self._config_int("cleanup_interval_hours", "XELATEX_CLEANUP_INTERVAL_HOURS", CLEANUP_INTERVAL_SECONDS // 3600, 1, 168)

    def _config_bool(self, key: str, env_key: str, default: bool) -> bool:
        value = self._get_config_value(key, env_key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _config_int(self, key: str, env_key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self._get_config_value(key, env_key, default))
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, value))

    async def initialize(self) -> None:
        """插件启动钩子。"""
        self.initialized = True
        self._start_file_server()
        # 启动（含面板保存后的热重载）即清理一轮，之后每 cleanup_interval_hours 一轮
        if getattr(self, "_cleanup_task", None) is None:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup_loop())
        return None

    async def _periodic_cleanup_loop(self) -> None:
        while True:
            try:
                removed = await asyncio.to_thread(self._cleanup_expired_files)
                await asyncio.to_thread(self._cleanup_stale_workdirs)
                if any(removed.values()):
                    logger.info("定期清理完成 %s", " ".join(f"{name}={count}" for name, count in removed.items()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("定期清理失败（下轮重试） err=%s", exc)
            interval_hours = getattr(self, "cleanup_interval_hours", CLEANUP_INTERVAL_SECONDS // 3600)
            await asyncio.sleep(interval_hours * 3600)

    async def terminate(self) -> None:
        """插件卸载时停止定期清理与回写调度器，并关闭内置文件服务。"""
        cleanup_task = getattr(self, "_cleanup_task", None)
        self._cleanup_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            # asyncio.wait 不抛任务异常，也不吞掉 terminate 自身被取消（卸载超时）的信号
            await asyncio.wait([cleanup_task], timeout=5)
        scheduler = getattr(self, "writeback_scheduler", None)
        if scheduler is not None:
            # 哨兵排空 + 限时 join；worker 是 daemon 线程，超时不阻塞卸载
            await asyncio.to_thread(scheduler.stop)
        server = self._file_server
        self._file_server = None
        self.initialized = False
        if server is not None:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        thread = self._file_server_thread
        self._file_server_thread = None
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 2)

    def get_environment_status(self) -> dict[str, object]:
        return {
            "initialized": self.initialized,
            "xelatex": self.tex_environment.xelatex,
            "latexmk": self.tex_environment.latexmk,
            "pdflatex": self.tex_environment.pdflatex,
            "available": self.tex_environment.available,
            "active_model": self.active_model_name,
            "available_models": self.available_models,
            "text_api_configured": bool(self.text_api_url),
            "file_server": f"http://{socket.gethostname()}:{self.file_server_port}/pdf/{{filename}}",
        }

    @filter.command("ai", alias={"ask", "解答", "解析"})
    async def handle_ai(self, event: AstrMessageEvent):
        # 多题模式一条指令产出多份回复：每个 yield 由 AstrBot 各自作为一条被动回复发送。
        # aclosing：AstrBot 提前停止消费时逐层关闭，未完成的题立即取消而不是等 GC
        async with aclosing(self.handle_event_stream(event)) as responses:
            async for response in responses:
                async for result in self._yield_response(event, response):
                    yield result

    @filter.command("查模型")
    async def handle_model_list(self, event: AstrMessageEvent):
        response = await asyncio.to_thread(
            self.handle_command,
            "/查模型",
            session_id=self.get_session_id(event),
        )
        async for result in self._yield_response(event, response):
            yield result

    @filter.command("ai_source", alias={"源文件"})
    async def handle_source_export(self, event: AstrMessageEvent):
        response = await asyncio.to_thread(
            self.handle_command,
            "/ai_source",
            session_id=self.get_session_id(event),
        )
        async for result in self._yield_response(event, response):
            yield result

    async def _yield_response(self, event: AstrMessageEvent, response: CommandResponse):
        if response.file_payload:
            file_payload = response.file_payload
            # 直接发送文件本体，不向用户推送下载链接文本
            if hasattr(event, "chain_result"):
                yield event.chain_result([File(name=file_payload["name"], file=file_payload.get("path", ""), url=file_payload.get("url", ""))])
                return
            # 兜底：平台不支持文件消息段时，回退为文本 + 下载链接
            fallback_message = response.message
            if file_payload.get("url"):
                fallback_message = f"{response.message}\n下载链接：{file_payload['url']}"
            if hasattr(event, "plain_result") and fallback_message:
                yield event.plain_result(fallback_message)
            return
        if hasattr(event, "plain_result"):
            yield event.plain_result(response.message)
        else:
            yield response.message

    async def handle_event(self, event: Any) -> CommandResponse:
        """单响应入口（兼容旧调用方）：返回流里的最后一条回复。"""
        last: Optional[CommandResponse] = None
        async for response in self.handle_event_stream(event):
            last = response
        return last or CommandResponse(success=False, message="消息中没有可处理的命令")

    async def handle_event_stream(self, event: Any):
        session_id = self.get_session_id(event)
        parsed_message = await self.parse_message(event)
        command_text = self._extract_command_text(parsed_message)
        override_model: Optional[str] = None
        if not command_text:
            fallback_text = (parsed_message.text or parsed_message.reply_text or "").strip()
            if fallback_text:
                command_text = f"/ai {fallback_text}"
            else:
                directive = self._extract_model_switch(parsed_message.text or parsed_message.reply_text)
                if directive is not None:
                    response = self._apply_model_directive(directive, session_id)
                    if response is not None:
                        yield response
                        return
                yield CommandResponse(success=False, message="消息中没有可处理的命令")
                return

        directive = self._extract_model_switch(self._extract_user_question(command_text))
        if directive is not None:
            response = self._apply_model_directive(directive, session_id)
            if response is not None:
                yield response
                return
            # 单次指定且同条消息带问题：重建命令直接回答本题，不触碰全局默认模型
            command_text = f"/ai {directive.question}"
            override_model = directive.model

        if self._is_source_export_command(command_text):
            yield await asyncio.to_thread(
                self.handle_command,
                command_text,
                session_id=session_id,
            )
            return

        request = self.parse_command(command_text)
        if request is not None and request.args[:1] == ["clear"]:
            yield await asyncio.to_thread(
                self.handle_command,
                command_text,
                session_id=session_id,
            )
            return
        if request is not None and "-m" in request.args:
            # -m 一律单次生效：带问题 → 用指定模型回答本题；不带问题 → 预设本会话下一题
            m_model, m_question = self._extract_m_flag(request.args)
            if not m_question:
                if m_model:
                    self.pending_model_by_session[session_id] = m_model
                    yield CommandResponse(
                        success=True,
                        message=f"已为下一题指定模型：{m_model}（仅本会话下一题生效，不影响全局）",
                    )
                    return
                yield CommandResponse(success=False, message="未指定模型：请在 -m 后填写模型名")
                return
            command_text = f"/ai {m_question}"
            override_model = m_model

        question_text = self._extract_user_question(command_text)
        numbers = self._extract_question_numbers(question_text) if getattr(self, "multi_question_enabled", False) else []
        parsed_message = await asyncio.to_thread(self.localize_attachments, parsed_message)
        # 多题只在有题图/引用可找题时拆；纯文字提问里出现多个题号仍按原方式整体作答
        if numbers and (parsed_message.images or parsed_message.pdfs or parsed_message.reply_text):
            async with aclosing(self._answer_multiple(event, session_id, parsed_message, command_text, numbers, override_model)) as responses:
                async for response in responses:
                    yield response
            return
        yield await self._answer_job(event, session_id, parsed_message, AnswerJob(command_text=command_text, question_text=question_text), override_model)

    async def _answer_job(
        self,
        event: Any,
        session_id: str,
        parsed_message: ParsedMessage,
        job: AnswerJob,
        override_model: Optional[str],
    ) -> CommandResponse:
        """一题的作答→排版→编译→存档→回写。单题与多题（每题一次、并发）共用。"""
        command_text = job.command_text
        question_text = job.question_text

        answer_text, used_model = await asyncio.to_thread(
            self.generate_answer,
            command_text,
            parsed_message,
            session_id,
            override_model,
            attachment_context=job.attachment_context,
            focus=job.focus,
        )
        # 注意：不得把 used_model 写回 active_model_name——
        # 那会把单次指定或降级用到的模型固化成全局默认，污染所有会话
        # 解析 LLM 返回的 [TITLE]/[SUMMARY] 结构化标记，标题与提要由模型生成
        structured = self._parse_structured_answer(answer_text)
        if job.label:
            # 多份 PDF 靠标题里的题号区分
            structured["title"] = f"{job.label} {structured['title']}".strip()
        subtitle_options = {"subtitle_question": job.subtitle_question} if job.subtitle_question else {}
        tex_source = await asyncio.to_thread(
            self.build_tex_source,
            parsed_message,
            body_override=structured["body"],
            question_text=question_text,
            title_override=structured["title"],
            summary_override=structured["summary"],
            **subtitle_options,
        )
        self.last_tex_source_by_session[session_id] = tex_source
        # 统一命名：QQ号_题目关键词_时间戳；源码/日志/PDF 三类文件共用同一基名
        qq_number = self._extract_qq_number(event)
        base_name = self._build_base_name(qq_number, question_text)
        self.last_base_name_by_session[session_id] = base_name
        await asyncio.to_thread(self._persist_tex_source, base_name, tex_source)
        response = await asyncio.to_thread(
            self.handle_command,
            command_text,
            tex_source=tex_source,
            session_id=session_id,
            base_name=base_name,
        )

        # 编译失败净化重试成功时，会话存档与落盘源码都要换成修复后版本，
        # 否则 /源文件 导出的还是编译不过的旧源码
        if response.repaired_source:
            tex_source = response.repaired_source
            self.last_tex_source_by_session[session_id] = tex_source
            await asyncio.to_thread(self._persist_tex_source, base_name, tex_source)

        if response.success and response.compile_result and response.compile_result.success:
            self.append_history(session_id, question_text, structured["body"])
            if response.file_payload and response.file_payload.get("path"):
                self.last_pdf_by_session[session_id] = Path(response.file_payload["path"])
            # 归档回写（旁路链路）：组 payload → 入队。失败隔离铁律：
            # 这里任何异常都不允许改变 return response 的值。
            try:
                if structured["archive"]["present"]:
                    writeback_payload = self._build_writeback_payload(
                        event=event,
                        question_text=question_text,
                        structured=structured,
                        parsed_message=parsed_message,
                        session_id=session_id,
                        base_name=base_name,
                        used_model=used_model,
                        response=response,
                    )
                    if job.external_id_suffix:
                        writeback_payload["external_id"] += job.external_id_suffix
                    self._enqueue_qa_writeback(writeback_payload)
            except Exception as exc:
                logger.warning("%s 回写组装/入队失败（已忽略） err=%s", WRITEBACK_LOG_PREFIX, exc)

        if job.label and not response.file_payload:
            response.message = f"{job.label}：{response.message}"
        return response

    async def _answer_multiple(
        self,
        event: Any,
        session_id: str,
        parsed_message: ParsedMessage,
        command_text: str,
        numbers: list[int],
        override_model: Optional[str],
    ):
        """一张图里的多道题：OCR 一次 → 找出各题题干 → 每题并行作答各出一份 PDF，谁先好先发。"""
        # 会话级一次性预设只消费一次，所有题共用，不能被第一道完成的题吃掉
        if not override_model:
            override_model = (self.pending_model_by_session.pop(session_id, "") or "").strip() or None
        slots = self._reply_slots(event)
        attachment_context = await asyncio.to_thread(self._build_attachment_context, parsed_message)
        stems = await asyncio.to_thread(self._split_questions, attachment_context, numbers, override_model)
        if stems is None:
            # 拆题失败不放弃：每题拿完整附件内容 + 「只答第 N 题」自行定位
            found, missing = list(numbers), []
            stems = {}
        else:
            found = [number for number in numbers if number in stems]
            missing = [number for number in numbers if number not in stems]
        if not found:
            yield CommandResponse(
                success=False,
                message=f"没在图片/引用里找到{self._format_question_numbers(numbers)}，请确认题号或换一张更清晰的图",
            )
            return

        # 回复次数有限：缺题或超额时占一次发说明，其余全给 PDF
        capacity = slots
        if missing or len(found) > slots:
            capacity = slots - 1
        answered, overflow = found[:capacity], found[capacity:]
        notice = self._format_multi_question_notice(answered, missing, overflow)
        jobs = [
            AnswerJob(
                command_text=command_text,
                question_text=f"第{number}题：{stems[number]}" if stems.get(number) else f"第{number}题",
                attachment_context=attachment_context,
                focus=self._build_question_focus(number, answered, stems.get(number, "")),
                external_id_suffix=f":q{number}",
                label=f"第{number}题",
                subtitle_question=self._extract_user_question(command_text),
            )
            for number in answered
        ]
        tasks = [asyncio.ensure_future(self._answer_job_safely(event, session_id, parsed_message, job, override_model)) for job in jobs]
        try:
            if notice:
                yield CommandResponse(success=True, message=notice)
            for next_done in asyncio.as_completed(tasks):
                yield await next_done
        finally:
            for task in tasks:
                task.cancel()

    async def _answer_job_safely(
        self,
        event: Any,
        session_id: str,
        parsed_message: ParsedMessage,
        job: AnswerJob,
        override_model: Optional[str],
    ) -> CommandResponse:
        # 一题出错不能拖垮同批其他题
        try:
            return await self._answer_job(event, session_id, parsed_message, job, override_model)
        except Exception as exc:
            logger.warning("多题作答失败 %s err=%s", job.label, exc)
            return CommandResponse(success=False, message=f"{job.label}：作答失败，请稍后单独重试这一题")

    async def parse_message(self, message: Any) -> ParsedMessage:
        parsed = ParsedMessage()
        event_text = self._get_event_text(message)
        if event_text:
            parsed.text = event_text
        components = self._extract_components(message)
        parsed.raw_components.extend(components)

        for component in components:
            component_type = component.__class__.__name__.lower()
            if "reply" in component_type:
                reply_payload = await self._extract_reply_payload(message, component)
                if reply_payload["text"]:
                    parsed.reply_text = reply_payload["text"]
                parsed.images.extend(reply_payload["images"])
                parsed.pdfs.extend(reply_payload["pdfs"])
                continue

            text = self._get_component_text(component)
            if text and text != parsed.text:
                parsed.text = f"{parsed.text}\n{text}".strip()
                continue

            attachment = self._component_to_attachment(component)
            if attachment is None:
                continue
            if attachment.kind == "image":
                parsed.images.append(attachment)
            elif attachment.kind == "pdf":
                parsed.pdfs.append(attachment)

        if isinstance(message, str):
            parsed.text = message.strip()

        parsed.images = self._dedupe_attachments(parsed.images)
        parsed.pdfs = self._dedupe_attachments(parsed.pdfs)
        return parsed

    def parse_command(self, text: str) -> Optional[CommandRequest]:
        stripped = text.strip()
        if not stripped:
            return None

        parts = stripped.split()
        command = parts[0]
        if command not in self.command_aliases:
            return None

        return CommandRequest(command=command, args=parts[1:], raw_text=stripped)

    def handle_command(self, text: str, tex_source: Optional[str] = None, session_id: str = "default", base_name: Optional[str] = None) -> CommandResponse:
        request = self.parse_command(text)
        if request is None:
            return CommandResponse(success=False, message="未识别的命令")

        if request.command in {"/查模型", "查模型"}:
            return CommandResponse(success=True, message=self._format_model_list(session_id=session_id))

        if request.command in {"/ai_source", "ai_source", "/源文件", "源文件"}:
            source = self.last_tex_source_by_session.get(session_id)
            if not source:
                return CommandResponse(success=False, message="当前会话没有可导出的源文件")
            # 与本次 PDF 共用同一「QQ号_题目关键词」基名，保证三类文件一一对应
            base_name = self.last_base_name_by_session.get(session_id) or session_id
            source_path = self._persist_tex_source(base_name, source)
            return CommandResponse(
                success=True,
                message="已导出源文件",
                file_payload=self.build_file_payload(source_path),
            )

        if request.args[:1] == ["clear"]:
            self.clear_session_history(session_id)
            return CommandResponse(success=True, message="会话已清空")

        if "-m" in request.args:
            # 单次生效：只预设本会话下一题，不改全局默认（带问题的形态由 handle_event 拦截）
            model_name, _question = self._extract_m_flag(request.args)
            if model_name:
                self.pending_model_by_session[session_id] = model_name
            return CommandResponse(
                success=True,
                message=f"已为下一题指定模型：{model_name or '未指定'}（仅本会话下一题生效，不影响全局）",
            )

        if tex_source is None:
            return CommandResponse(success=True, message="命令已接收，等待 TeX 内容")

        compile_result = self.compile_tex(tex_source)
        repaired_source: Optional[str] = None
        if not compile_result.success:
            # 编译失败自动净化重试：仅针对已识别的错误模式修复后重试一次，
            # 避免盲目重试掩盖真实错误
            repaired = self._repair_tex_source(tex_source, compile_result)
            if repaired is not None:
                self._cleanup_compile_result(compile_result, base_name=base_name)
                compile_result = self.compile_tex(repaired)
                if compile_result.success:
                    repaired_source = repaired
                logger.info(
                    "编译失败净化重试：%s",
                    "成功" if compile_result.success else "仍失败",
                )
        if compile_result.success:
            file_payload = self.build_file_payload(compile_result.pdf_path, base_name=base_name)
            if file_payload is None or not file_payload.get("path"):
                compile_result.success = False
                compile_result.stderr = "PDF 已编译，但无法复制到导出目录"
                message = self.build_compile_error_message(compile_result)
                self._cleanup_compile_result(compile_result, base_name=base_name)
                return CommandResponse(
                    success=False,
                    message=message,
                    compile_result=compile_result,
                    repaired_source=repaired_source,
                )
            exported_path = Path(file_payload["path"])
            self._cleanup_compile_result(compile_result, exported_path=exported_path, base_name=base_name)
            return CommandResponse(
                success=True,
                message="编译成功",
                compile_result=compile_result,
                file_payload=file_payload,
                repaired_source=repaired_source,
            )
        message = self.build_compile_error_message(compile_result)
        self._cleanup_compile_result(compile_result, base_name=base_name)
        return CommandResponse(
            success=False,
            message=message,
            compile_result=compile_result,
            repaired_source=repaired_source,
        )

    def _cleanup_compile_result(self, compile_result: CompileResult, exported_path: Optional[Path] = None, base_name: Optional[str] = None) -> None:
        # 删除临时工作目录前，先把编译日志按统一命名归档到 logs/ 分类目录
        self._archive_compile_log(compile_result, base_name)
        workdir_path = compile_result.workdir_path
        if workdir_path is not None:
            shutil.rmtree(workdir_path, ignore_errors=True)
        compile_result.workdir_path = None
        compile_result.log_path = None
        compile_result.pdf_path = exported_path

    def _archive_compile_log(self, compile_result: CompileResult, base_name: Optional[str] = None) -> None:
        log_path = compile_result.log_path
        if log_path is None or not Path(log_path).exists():
            return
        log_dir = getattr(self, "log_dir", None)
        if log_dir is None:
            return
        name = self._safe_filename_component(base_name or "compile", default="compile")
        target = Path(log_dir) / f"{name}.log"
        try:
            shutil.copyfile(log_path, target)
        except OSError:
            return

    def _cleanup_expired_files(self) -> dict[str, int]:
        """删除 assets/pdfs/sources/logs 下超过保留天数的文件，返回各目录删除数。

        只动这四个目录的直接子文件（不递归、不跟随符号链接）；数据根目录下的
        chat_history.json 与题库引用清单不在范围内。被题库热链的题图永不删除。
        """
        # 配置是整数天，0 关闭；最短也保留一天，不会删到正在作答、还没发出去的文件
        retention_days = int(getattr(self, "file_retention_days", 0) or 0)
        if retention_days <= 0:
            return {}
        cutoff = time.time() - retention_days * 24 * 60 * 60
        protected = self._load_protected_assets()
        removed: dict[str, int] = {}
        for name, directory in (("assets", self.asset_dir), ("pdfs", self.pdf_dir), ("sources", self.source_dir), ("logs", self.log_dir)):
            count = 0
            try:
                entries = list(Path(directory).iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_file():
                        continue
                    if name == "assets" and entry.name in protected:
                        continue
                    if entry.stat().st_mtime >= cutoff:
                        continue
                    entry.unlink()
                    count += 1
                except OSError:
                    continue
            removed[name] = count
        return removed

    def _protected_assets_path(self) -> Path:
        return Path(self.data_dir) / PROTECTED_ASSETS_FILENAME

    def _load_protected_assets(self) -> set[str]:
        """题库引用清单：回写时提交给题库的题图文件名。题库审阅板直接热链这些 URL，删了就永久裂图。"""
        try:
            data = json.loads(self._protected_assets_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(item) for item in data} if isinstance(data, list) else set()

    def _remember_protected_assets(self, image_urls: list[str]) -> None:
        names = {Path(urllib.parse.urlsplit(url).path).name for url in image_urls if "/asset/" in url}
        names.discard("")
        if not names:
            return
        with _PROTECTED_ASSETS_LOCK:
            merged = self._load_protected_assets() | names
            path = self._protected_assets_path()
            temp_path = path.with_name(f".{path.name}.tmp")
            try:
                temp_path.write_text(json.dumps(sorted(merged), ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temp_path, path)
            except OSError as exc:
                temp_path.unlink(missing_ok=True)
                logger.warning("题库引用清单写入失败 err=%s", exc)

    def _cleanup_stale_workdirs(self) -> None:
        temp_root = Path(tempfile.gettempdir()).resolve()
        cutoff = time.time() - STALE_WORKDIR_MAX_AGE_SECONDS
        for pattern in ("xelatex_pdf_*", "xelatex_ocr_*"):
            for candidate in temp_root.glob(pattern):
                try:
                    resolved = candidate.resolve()
                    if resolved.parent != temp_root or not resolved.is_dir():
                        continue
                    if resolved.stat().st_mtime < cutoff:
                        shutil.rmtree(resolved, ignore_errors=True)
                except OSError:
                    continue

    def compile_tex(self, tex_source: str) -> CompileResult:
        if not self.tex_environment.xelatex:
            return CompileResult(
                success=False,
                workdir_path=None,
                pdf_path=None,
                log_path=None,
                stdout="",
                stderr="xelatex 不可用",
            )

        workdir_path = Path(tempfile.mkdtemp(prefix="xelatex_pdf_"))
        tex_path = workdir_path / "main.tex"
        pdf_path = workdir_path / "main.pdf"
        log_path = workdir_path / "main.log"
        class_source = self._find_elegantbook_class()
        if class_source is None:
            return CompileResult(
                success=False,
                workdir_path=workdir_path,
                pdf_path=None,
                log_path=None,
                stdout="",
                stderr="astrbot-elegantbook.cls 未找到，请重新安装插件或检查插件目录",
            )
        shutil.copyfile(class_source, workdir_path / "astrbot-elegantbook.cls")
        tex_source = self._prepare_compile_source(tex_source, workdir_path)
        tex_path.write_text(tex_source, encoding="utf-8")

        tex_env = os.environ.copy()
        tex_env.update({"openin_any": "p", "openout_any": "p"})
        try:
            process = subprocess.run(
                [
                    self.tex_environment.xelatex,
                    "-no-shell-escape",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-file-line-error",
                    "-output-directory",
                    ".",
                    tex_path.name,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=PROCESS_TIMEOUT_SECONDS,
                env=tex_env,
                cwd=workdir_path,
            )
        except subprocess.TimeoutExpired as exc:
            return CompileResult(
                success=False,
                workdir_path=workdir_path,
                pdf_path=None,
                log_path=log_path if log_path.exists() else None,
                stdout=str(exc.stdout or ""),
                stderr=f"xelatex 编译超过 {PROCESS_TIMEOUT_SECONDS} 秒，已终止",
            )

        pdf_valid = process.returncode == 0 and self._is_valid_pdf(pdf_path)
        return CompileResult(
            success=pdf_valid,
            workdir_path=workdir_path,
            pdf_path=pdf_path if pdf_valid else None,
            log_path=log_path if log_path.exists() else None,
            stdout=process.stdout,
            stderr=process.stderr,
        )

    def _prepare_compile_source(self, tex_source: str, workdir_path: Path) -> str:
        """Copy approved image inputs into the restricted TeX working directory."""
        configured_roots = [
            getattr(self, "asset_dir", None),
            getattr(self, "data_dir", None),
            getattr(self, "export_dir", None),
        ]
        allowed_roots = {
            Path(root).resolve()
            for root in configured_roots
            if root is not None and Path(root).exists()
        }
        copied_paths: dict[Path, str] = {}

        def replace_image(match: re.Match[str]) -> str:
            raw_reference = match.group(2).strip()
            try:
                source_path = Path(raw_reference).resolve(strict=True)
            except (OSError, RuntimeError):
                return r"\fbox{\textit{图片附件不可用}}"

            if not any(source_path.is_relative_to(root) for root in allowed_roots):
                return r"\fbox{\textit{图片附件不可用}}"
            if source_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
                return r"\fbox{\textit{图片附件不可用}}"
            if source_path.stat().st_size > MAX_ATTACHMENT_BYTES:
                return r"\fbox{\textit{图片附件过大}}"

            relative_name = copied_paths.get(source_path)
            if relative_name is None:
                relative_name = f"attachment_{len(copied_paths) + 1}{source_path.suffix.lower()}"
                shutil.copyfile(source_path, workdir_path / relative_name)
                copied_paths[source_path] = relative_name
            return f"{match.group(1)}{relative_name}{match.group(3)}"

        pattern = re.compile(r"(\\includegraphics(?:\[[^\]]*\])?\{)([^{}]+)(\})")
        return pattern.sub(replace_image, tex_source)

    def _is_valid_pdf(self, pdf_path: Path) -> bool:
        if not pdf_path.exists() or pdf_path.stat().st_size < 1024:
            return False
        try:
            with pdf_path.open("rb") as file_obj:
                header = file_obj.read(8)
                file_obj.seek(max(pdf_path.stat().st_size - 2048, 0))
                tail = file_obj.read()
        except Exception:
            return False
        return header.startswith(b"%PDF-") and b"%%EOF" in tail

    def _find_elegantbook_class(self) -> Optional[Path]:
        plugin_class = Path(__file__).resolve().parent / "astrbot-elegantbook.cls"
        if plugin_class.exists():
            return plugin_class
        fallback_class = Path("/root/typesetting_helper/elegantbook_副本.cls")
        if fallback_class.exists():
            return fallback_class
        return None

    def build_file_payload(self, file_path: Optional[Path], base_name: Optional[str] = None) -> Optional[dict[str, Any]]:
        if file_path is None:
            return None
        file_path = Path(file_path)
        # 已落在任一分类目录内的文件直接回传；临时目录产物先复制到 PDF 分类目录
        if not self._is_managed_path(file_path):
            copied = self._copy_to_export_dir(file_path, base_name=base_name)
            if copied is None:
                return None
            file_path = copied
        return {
            "type": "file",
            "name": file_path.name,
            "path": str(file_path),
            "url": self._public_file_url(file_path),
        }

    def _is_managed_path(self, file_path: Path) -> bool:
        try:
            resolved_parent = file_path.resolve().parent
        except OSError:
            return False
        managed_dirs = [
            getattr(self, "data_dir", None),
            getattr(self, "pdf_dir", None),
            getattr(self, "source_dir", None),
            getattr(self, "log_dir", None),
            getattr(self, "asset_dir", None),
        ]
        for managed_dir in managed_dirs:
            if managed_dir is None:
                continue
            try:
                if resolved_parent == Path(managed_dir).resolve():
                    return True
            except OSError:
                continue
        return False

    def _is_diagram_request(self, question: str, parsed_message: ParsedMessage) -> bool:
        text = f"{question}\n{parsed_message.text}\n{parsed_message.reply_text}"
        return any(keyword in text for keyword in ("哈斯图", "偏序集", "画", "绘制", "生成到同一张图片", "TikZ", "tikzpicture", "graph"))

    def _build_render_instruction(self, question: str, parsed_message: ParsedMessage) -> str:
        if self._is_diagram_request(question, parsed_message):
            return (
                "这是一个图形/作图题。请直接根据用户原文和附件上下文生成可编译的 LaTeX 正文片段，不要要求用户补充信息。"
                "如果题目给出了集合和关系，请自行解析出节点、覆盖关系和层次结构；如果是多个偏序集，请按 (1)(2)(3) 分段。"
                "优先输出 TikZ 代码或带说明的 LaTeX 片段，确保插件可以直接编译出图。"
            )
        return (
            "请直接回答用户问题，并把答案写成适合 ElegantBook 的正文片段。"
            "如果适合，请优先使用定义、定理、性质、例题、注、笔记、证明、解答等环境。"
        )

    def _build_user_render_prompt(
        self,
        question: str,
        parsed_message: ParsedMessage,
        *,
        attachment_context: Optional[str] = None,
        focus: str = "",
    ) -> str:
        parts = [f"用户原文：\n{question.strip()}"]
        if focus:
            parts.append(focus)
        if parsed_message.reply_text:
            parts.append(f"引用消息：\n{parsed_message.reply_text.strip()}")
        if parsed_message.text and parsed_message.text.strip() and parsed_message.text.strip() != question.strip():
            parts.append(f"补充文本：\n{parsed_message.text.strip()}")
        # 多题模式由调用方传入共享的附件上下文，OCR 不随题数重复
        if attachment_context is None:
            attachment_context = self._build_attachment_context(parsed_message)
        if attachment_context:
            parts.append(attachment_context)
        parts.append(self._build_render_instruction(question, parsed_message))
        parts.append("输出要求：不要输出完整 LaTeX 文档，不要寒暄，不要说无法识别；直接输出可插入模板正文的内容。")
        return "\n\n".join(parts)

    def generate_answer(
        self,
        command_text: str,
        parsed_message: ParsedMessage,
        session_id: str,
        override_model: Optional[str] = None,
        *,
        attachment_context: Optional[str] = None,
        focus: str = "",
    ) -> tuple[str, str]:
        question = self._extract_user_question(command_text)
        if not question:
            question = parsed_message.text.strip() if parsed_message.text.strip() else "请给出回应。"

        if not self.text_api_url or not self.text_api_key:
            return "文本模型接口未配置。请在插件配置中设置 text_api_url、text_api_key 和 llm_model。", self.active_model_name

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "你是一个严谨、简洁的中文学术助手。你的回答会被程序转换并插入 ElegantBook LaTeX 模板的正文区域。"
                    "请输出适合 ElegantBook 正文排版的 Markdown/LaTeX 片段，而不是完整 LaTeX 文档。"
                    "输出格式（必须严格遵守）：第一行写 [TITLE] 加一个简短的文档标题（15 字以内，概括本次内容，不要书名号）；"
                    "第二行写 [SUMMARY] 加一两句内容提要（概括核心问题、方法与结论，60 字以内）；"
                    "之后空一行，直接输出正文。不要输出其他标记或解释。"
                    "结构要求：正文使用清晰标题、短段落、项目列表和必要公式；标题使用 Markdown 的 #、##、### 等层级即可。"
                    "ElegantBook 环境要求：遇到定义、定理、性质、例题、注、笔记、证明、解答时，优先直接使用对应环境："
                    "\\begin{definition}...\\end{definition}、\\begin{theorem}...\\end{theorem}、\\begin{property}...\\end{property}、"
                    "\\begin{example}...\\end{example}、\\begin{remark}...\\end{remark}、\\begin{note}...\\end{note}、"
                    "\\begin{proof}...\\end{proof}、\\begin{solution}...\\end{solution}。"
                    "也可以用 Markdown 标题写“## 定义”“#### 例 1”“#### 注”等，外部程序会自动转换为 ElegantBook 环境。"
                    "数学要求：行内公式优先使用 \\(...\\) 或 $...$；行间公式使用 \\[...\\] 或 $$...$$；可使用 amsmath/amsfonts/amssymb 常见命令，如 \\mathbb、\\varepsilon、\\delta、\\Rightarrow。"
                    "内容要求：直接回答用户问题，不要寒暄，不要说“下面是”。标题与提要必须根据本次回答的实际内容撰写，不要写空话套话。"
                    "归档要求（供程序自动读取）：如果你判断本次回答完整解答了一道可收录的题目（题干条件充分、解答完整），"
                    "请在正文最后空一行，输出如下归档块，块内每行一个标记、顺序固定："
                    "[ARCHIVE]"
                    "[STEM] 规范题干：把用户口语化提问或图片转述整理为正式题目表述，保留全部已知条件与数学符号（行内公式用 $...$），"
                    "不遗漏条件，不混入解答内容；选择题须把选项完整写入（A. … B. …）"
                    "[ANSWER] 最终答案：一行给出（选择题写选项字母，填空题写结果，证明/论述题写“见解析”）"
                    "[TYPE] 题型：single（单选）/ multiple（多选）/ blank（填空）/ proof（证明）/ calc（计算）/ answer（解答）六选一"
                    "[DIFFICULTY] 难度：1-5 的整数（1 最易）"
                    "[TAGS] 知识点：至多 5 个标签，用英文逗号分隔，例如 极限,洛必达法则,中值定理"
                    "[/ARCHIVE]"
                    "若本次内容不是完整题目（闲聊、工具命令、绘图请求、条件不全、追问讨论），不要输出归档块。"
                    "归档块会被程序剥离、不排版进 PDF；标记格式必须严格一致。"
                    "禁止输出：\\documentclass、\\begin{document}、\\end{document}、\\usepackage、\\author、\\date、\\maketitle、页眉页脚设置、模型名、回答时间、作者信息。"
                    "不要把模型名或回答时间放在正文开头；这些元信息由外部模板写入页脚。"
                ),
            }
        ]
        memory = self.chat_history.get(session_id)
        if memory is not None:
            for item in memory.rounds[-self.max_history_rounds:]:
                user_text = item.get("user", "").strip()
                assistant_text = item.get("assistant", "").strip()
                if user_text:
                    messages.append({"role": "user", "content": user_text})
                if assistant_text:
                    messages.append({"role": "assistant", "content": assistant_text})

        messages.append(
            {
                "role": "user",
                "content": self._build_user_render_prompt(
                    question,
                    parsed_message,
                    attachment_context=attachment_context,
                    focus=focus,
                ),
            }
        )

        model_queue = self._build_model_queue(
            override_model=override_model,
            session_id=session_id,
        )
        last_error = ""
        for model_name in model_queue:
            try:
                answer = self._call_text_model(model_name, messages)
            except Exception as exc:
                last_error = str(exc)
                continue
            if answer.strip():
                return answer.strip(), model_name

        return f"文本模型调用失败：{last_error or '模型未返回内容'}", self.active_model_name

    def _call_text_model(self, model_name: str, messages: list[dict[str, str]]) -> str:
        global _text_model_cool_until
        attempt = 0
        while True:
            # 共享退避：任一并发请求被限流后，所有文本请求一起等，不去反复撞限流的账号
            with _TEXT_MODEL_COOLDOWN_LOCK:
                wait = _text_model_cool_until - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                data = self._post_chat_completion(
                    api_url=self.text_api_url,
                    api_key=self.text_api_key,
                    payload={"model": model_name, "messages": messages},
                    timeout=MODEL_TIMEOUT_SECONDS,
                )
                return self._extract_chat_content(data)
            except ChatCompletionHTTPError as exc:
                if exc.status not in THROTTLED_STATUS_CODES or attempt >= TEXT_MODEL_THROTTLE_RETRIES:
                    raise
                delay = min(exc.retry_after or 4 * 2 ** attempt, TEXT_MODEL_MAX_BACKOFF_SECONDS)
                with _TEXT_MODEL_COOLDOWN_LOCK:
                    _text_model_cool_until = max(_text_model_cool_until, time.monotonic() + delay)
                logger.info("文本模型限流退避 model=%s status=%s delay=%ss attempt=%d", model_name, exc.status, delay, attempt + 1)
                attempt += 1

    def _post_chat_completion(self, api_url: str, api_key: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(f"{api_url.rstrip('/')}/chat/completions", data=encoded, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", f"astrbot-plugin-xelatex-pdf/{PLUGIN_VERSION}")
        request.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")[:500]
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            raise ChatCompletionHTTPError(exc.code, body, retry_after) from exc
        if not isinstance(data, dict):
            raise RuntimeError("响应不是 JSON 对象")
        return data

    def _extract_chat_content(self, data: dict[str, Any]) -> str:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            raise RuntimeError("响应中没有 choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts)
        return str(content or "")

    def _call_vision_model(self, image_paths: list[str]) -> str:
        if not image_paths or not self.ocr_api_url or not self.ocr_api_key or not self.vision_model_name:
            return ""

        image_urls: list[str] = []
        for image_path in image_paths:
            data_url = self._image_to_data_url(image_path)
            if data_url:
                image_urls.append(data_url)
        if not image_urls:
            return ""

        model_queue = [self.vision_model_name]
        for model_name in self.fallback_vision_models:
            if model_name and model_name not in model_queue:
                model_queue.append(model_name)

        prompt = "请精准提取这些图片中的所有文本内容。若包含数学公式，请使用清晰且规范的 LaTeX 语法输出，不要遗漏细节。"
        for model_name in model_queue:
            try:
                data = self._post_chat_completion(
                    api_url=self.ocr_api_url,
                    api_key=self.ocr_api_key,
                    payload={
                        "model": model_name,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    *[
                                        {"type": "image_url", "image_url": {"url": image_url}}
                                        for image_url in image_urls
                                    ],
                                ],
                            }
                        ],
                    },
                    timeout=VISION_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            content = self._extract_chat_content(data).strip()
            if content:
                return content
        return ""

    def _collect_ocr_inputs(self, parsed_message: ParsedMessage) -> tuple[list[str], list[Path]]:
        image_paths: list[str] = []
        temp_dirs: list[Path] = []

        for image in parsed_message.images:
            source = image.path or image.url or image.name
            local_path = self._resolve_local_path(source)
            if local_path:
                image_paths.append(local_path)

        for pdf in parsed_message.pdfs:
            source = pdf.path or pdf.url or pdf.name
            local_path = self._resolve_local_path(source)
            if not local_path:
                continue
            rendered_paths, temp_dir = self._render_pdf_to_images(local_path)
            image_paths.extend(rendered_paths)
            if temp_dir is not None:
                temp_dirs.append(temp_dir)

        deduped: list[str] = []
        seen: set[str] = set()
        for path in image_paths:
            if path and path not in seen:
                seen.add(path)
                deduped.append(path)
        return deduped, temp_dirs

    def _render_pdf_to_images(self, pdf_path: str) -> tuple[list[str], Optional[Path]]:
        temp_dir = Path(tempfile.mkdtemp(prefix="xelatex_pdf_pages_"))
        image_paths: list[str] = []
        try:
            try:
                import fitz  # type: ignore
            except Exception:
                fitz = None  # type: ignore[assignment]
            if fitz is not None:
                document = fitz.open(pdf_path)
                try:
                    for index in range(min(len(document), MAX_OCR_PAGES)):
                        page = document[index]
                        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                        page_path = temp_dir / f"page_{index + 1}.png"
                        pix.save(page_path)
                        image_paths.append(str(page_path))
                finally:
                    document.close()
            if image_paths:
                return image_paths, temp_dir

            pdftoppm = shutil.which("pdftoppm")
            if pdftoppm is None:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return [], None
            prefix = temp_dir / "page"
            subprocess.run(
                [pdftoppm, "-f", "1", "-l", str(MAX_OCR_PAGES), "-png", pdf_path, str(prefix)],
                capture_output=True,
                text=True,
                check=False,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            image_paths = [str(path) for path in sorted(temp_dir.glob("page-*.png"))]
            if not image_paths:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return [], None
            return image_paths, temp_dir
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return [], None

    def _image_to_data_url(self, image_path: str) -> str:
        local_path = self._resolve_local_path(image_path)
        if not local_path:
            return ""
        try:
            if Path(local_path).stat().st_size > MAX_OCR_IMAGE_BYTES:
                return ""
            with open(local_path, "rb") as file_obj:
                raw = base64.b64encode(file_obj.read()).decode("utf-8")
        except Exception:
            return ""
        suffix = Path(local_path).suffix.lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }
        mime_type = mime_map.get(suffix, "image/png")
        return f"data:{mime_type};base64,{raw}"

    def _build_model_queue(
        self,
        override_model: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> list[str]:
        """组装本次回答的模型降级队列。

        优先级：显式 override（同条消息指定）> 会话级一次性预设（消费即清除）
        > 全局默认 + 可用模型列表。单次指定绝不写入全局状态。
        """
        head: Optional[str] = (override_model or "").strip() or None
        if head is None and session_id is not None:
            head = (self.pending_model_by_session.pop(session_id, "") or "").strip() or None
        models: list[str] = []
        if head:
            models.append(head)
        models.append(self.active_model_name)
        models.extend(self.available_models)
        result: list[str] = []
        for model in models:
            model = (model or "").strip()
            if model and model not in result:
                result.append(model)
        return result or ["default"]

    @staticmethod
    def _extract_m_flag(args: list[str]) -> tuple[str, str]:
        """从命令参数中剥离 -m 及其模型名，返回 (模型名, 剩余问题文本)。"""
        args = list(args)
        model_name = ""
        if "-m" in args:
            model_index = args.index("-m")
            if model_index + 1 < len(args):
                model_name = args[model_index + 1]
                del args[model_index:model_index + 2]
            else:
                del args[model_index:model_index + 1]
        return model_name.strip(), " ".join(args).strip()

    def _extract_user_question(self, command_text: str) -> str:
        request = self.parse_command(command_text)
        if request is None:
            return command_text.strip()
        _model_name, question = self._extract_m_flag(request.args)
        return question

    def _build_attachment_context(self, parsed_message: ParsedMessage) -> str:
        parts: list[str] = []
        if parsed_message.reply_text:
            parts.append(f"引用消息：\n{parsed_message.reply_text}")

        image_paths, temp_dirs = self._collect_ocr_inputs(parsed_message)
        try:
            vision_text = self._call_vision_model(image_paths)
        finally:
            for temp_dir in temp_dirs:
                shutil.rmtree(temp_dir, ignore_errors=True)
        # 排查「附件有没有被取到」只能靠这一行：图片/PDF 数、送 OCR 的页数、OCR 字数
        logger.info(
            "附件上下文 images=%d pdfs=%d ocr_inputs=%d ocr_chars=%d reply=%s",
            len(parsed_message.images),
            len(parsed_message.pdfs),
            len(image_paths),
            len(vision_text),
            bool(parsed_message.reply_text),
        )

        if vision_text:
            parts.append("附件 OCR 内容：\n" + vision_text[:12000])
        else:
            pdf_texts: list[str] = []
            for pdf in parsed_message.pdfs:
                source = pdf.path or pdf.url or pdf.name
                if not source:
                    continue
                extracted = self._extract_pdf_text(source)
                if extracted:
                    pdf_texts.append(extracted[:6000])
            if pdf_texts:
                parts.append("PDF 附件文本：\n" + "\n\n".join(pdf_texts))
            elif parsed_message.images:
                names = [image.name or image.path or image.url for image in parsed_message.images]
                names = [name for name in names if name]
                if names:
                    parts.append("图片附件（OCR 未取到结果）：" + "，".join(names))
        return "\n\n".join(parts)

    def _parse_structured_answer(self, answer_text: str) -> dict[str, Any]:
        """解析 LLM 返回的 [TITLE]/[SUMMARY] 结构化标记与 [ARCHIVE] 归档围栏块。

        返回 {"title", "summary", "body", "archive"} 完整四键。契约：
        任何解析异常都不向调用方抛出——标记缺失或解析失败时对应字段回落
        默认值，由调用方决定降级策略，不伪造内容。归档块在 body 中整体
        剥离（围栏外的 [ANSWER] 等字样一律不解析），保证 PDF 零污染。
        """
        title = ""
        summary = ""
        archive = self._empty_archive_dict()
        try:
            body_lines: list[str] = []
            archive_lines: list[str] = []
            inside_archive = False
            archive_opened = False
            for line in (answer_text or "").splitlines():
                stripped = line.strip()
                if not inside_archive and stripped == "[ARCHIVE]":
                    inside_archive = True
                    archive_opened = True
                    continue
                if inside_archive and stripped == "[/ARCHIVE]":
                    inside_archive = False
                    continue
                if inside_archive:
                    archive_lines.append(stripped)
                    continue
                if not title and stripped.startswith("[TITLE]"):
                    title = stripped.removeprefix("[TITLE]").strip()
                    continue
                if not summary and stripped.startswith("[SUMMARY]"):
                    summary = stripped.removeprefix("[SUMMARY]").strip()
                    continue
                body_lines.append(line)
            if archive_opened:
                archive = self._parse_archive_block(archive_lines)
            body = "\n".join(body_lines).strip()
            # 既有降级：无标记无正文时 body 回落原文；但检测到归档块时不得回落，
            # 否则会把 [ARCHIVE] 标记重新带回正文、污染 PDF
            if not archive_opened:
                body = body or (answer_text or "").strip()
        except Exception as exc:  # 契约兜底：解析层永不上抛
            logger.warning("%s 结构化解析异常（已回落默认值） err=%s", WRITEBACK_LOG_PREFIX, exc)
            return {
                "title": "",
                "summary": "",
                "body": (answer_text or "").strip(),
                "archive": self._empty_archive_dict(),
            }
        return {
            "title": title,
            "summary": summary,
            "body": body,
            "archive": archive,
        }

    @staticmethod
    def _empty_archive_dict() -> dict[str, Any]:
        """未检测到归档块时的缺省结构；present=False 时调用方跳过整个回写。"""
        return {
            "present": False,
            "stem": "",
            "answer": "",
            "type": "answer",
            "difficulty": 3,
            "tags": [],
        }

    def _parse_archive_block(self, lines: list[str]) -> dict[str, Any]:
        """逐行容错解析归档块内容：单行坏只丢该字段并回落默认，不整体作废。"""
        archive = self._empty_archive_dict()
        archive["present"] = True
        for line in lines:
            if line.startswith("[STEM]"):
                archive["stem"] = line.removeprefix("[STEM]").strip()
            elif line.startswith("[ANSWER]"):
                archive["answer"] = line.removeprefix("[ANSWER]").strip()
            elif line.startswith("[TYPE]"):
                value = line.removeprefix("[TYPE]").strip().lower()
                archive["type"] = value if value in ARCHIVE_TYPE_WHITELIST else "answer"
            elif line.startswith("[DIFFICULTY]"):
                value = line.removeprefix("[DIFFICULTY]").strip()
                if value.isdigit() and 1 <= int(value) <= 5:
                    archive["difficulty"] = int(value)
                else:
                    archive["difficulty"] = 3
            elif line.startswith("[TAGS]"):
                tags: list[str] = []
                for tag in line.removeprefix("[TAGS]").split(","):
                    tag = tag.strip()
                    if tag and tag not in tags:
                        tags.append(tag)
                archive["tags"] = tags[:ARCHIVE_TAG_LIMIT]
        return archive

    def build_tex_source(self, parsed_message: ParsedMessage, body_override: Optional[str] = None, question_text: str = "", title_override: Optional[str] = None, summary_override: Optional[str] = None, subtitle_question: Optional[str] = None) -> str:
        if body_override is None:
            body_parts = [parsed_message.reply_text, parsed_message.text]
            body = "\n".join(part for part in body_parts if part).strip() or "请输入要编译的内容。"
        else:
            body = body_override.strip() or "模型未返回有效内容。"
        image_block = self._build_image_block(parsed_message.images)
        pdf_block = self._build_pdf_block(parsed_message.pdfs)
        answer_model = self._escape_tex_metadata(self.active_model_name or "default")
        answer_time = self._escape_tex_metadata(time.strftime("%Y-%m-%d %H:%M:%S"))
        # 标题优先取 LLM 返回，缺失时回退问题截取；不再按关键词猜测分类标题
        title_text = self._escape_tex_metadata((title_override or "").strip() or self._build_document_title(question_text, body))
        subtitle_text = self._escape_tex_metadata(self._build_document_subtitle(question_text if subtitle_question is None else subtitle_question))
        # 内容提要仅当 LLM 返回时才渲染，避免硬编码套话
        summary_box = ""
        if summary_override and summary_override.strip():
            summary_box = (
                "\\begin{answerabstractbox}\n"
                "\\textbf{内容提要}\\quad " + self._tex_inline(summary_override.strip()) + "\n"
                "\\end{answerabstractbox}\n"
            )
        question_card = self._build_question_card(question_text)
        return (
            "\\documentclass[lang=cn,11pt,color=blue,device=normal]{astrbot-elegantbook}\n"
            "\\usepackage{amsmath,amsfonts,amssymb}\n"
            "\\usepackage{tikz}\n"
            "\\usepackage{graphicx}\n"
            "\\usepackage{float}\n"
            "\\usepackage{tcolorbox}\n"
            "\\usetikzlibrary{arrows.meta,calc,positioning,fit,matrix,graphs,decorations.pathreplacing}\n"
            "\\definecolor{answerprimary}{HTML}{2563EB}\n"
            "\\definecolor{answersecondary}{HTML}{6B7280}\n"
            "\\definecolor{answeraccent}{HTML}{F3F6FB}\n"
            "\\definecolor{answerline}{HTML}{D6E4FF}\n"
            "\\definecolor{answergold}{HTML}{B7791F}\n"
            f"\\newcommand{{\\AnswerModel}}{{{answer_model}}}\n"
            f"\\newcommand{{\\AnswerTime}}{{{answer_time}}}\n"
            f"\\newcommand{{\\AnswerTitle}}{{{title_text}}}\n"
            f"\\newcommand{{\\AnswerSubtitle}}{{{subtitle_text}}}\n"
            "\\geometry{a4paper,top=2.2cm,bottom=2.8cm,left=2.3cm,right=2.4cm,includefoot}\n"
            "\\hypersetup{draft}\n"
            "\\setlength{\\parindent}{0pt}\n"
            "\\setlength{\\parskip}{0.72em}\n"
            "\\hypersetup{pdfauthor={},pdftitle={\\AnswerTitle}}\n"
            "\\newtcolorbox{answerabstractbox}{colback=answeraccent,colframe=answerline,boxrule=0.5pt,arc=1.2mm,left=1.2mm,right=1.2mm,top=1mm,bottom=1mm}\n"
            "\\newtcolorbox{answerkeybox}{colback=white,colframe=answerline,boxrule=0.5pt,arc=1.2mm,left=1.2mm,right=1.2mm,top=1mm,bottom=1mm}\n"
            "\\newcommand{\\AnswerFooterLine}{%\n"
            "\\parbox{\\textwidth}{\\color{answersecondary}\\rule{\\textwidth}{0.4pt}\\\\[-0.1em]\\small 模型：\\AnswerModel\\hfill\\thepage\\hfill 时间：\\AnswerTime}%\n"
            "}\n"
            "\\fancypagestyle{answerfooter}{%\n"
            "\\fancyhf{}%\n"
            "\\renewcommand{\\headrulewidth}{0pt}%\n"
            "\\renewcommand{\\footrulewidth}{0pt}%\n"
            "\\fancyhead[L]{\\small\\color{answersecondary}\\nouppercase{\\leftmark}}%\n"
            "\\fancyhead[R]{\\small\\color{answersecondary}\\AnswerModel}%\n"
            "\\fancyfoot[C]{\\AnswerFooterLine}%\n"
            "}\n"
            "\\fancypagestyle{plain}{%\n"
            "\\fancyhf{}%\n"
            "\\renewcommand{\\headrulewidth}{0pt}%\n"
            "\\renewcommand{\\footrulewidth}{0pt}%\n"
            "\\fancyfoot[C]{\\AnswerFooterLine}%\n"
            "}\n"
            "\\pagestyle{answerfooter}\n"
            "\\setlength{\\footskip}{30pt}\n"
            "\\begin{document}\n"
            "\\thispagestyle{answerfooter}\n"
            "\\begin{center}\n"
            "{\\Huge\\bfseries\\color{answerprimary} \\AnswerTitle}\\par\n"
            "\\vspace{0.45em}\n"
            "{\\large\\color{answersecondary} \\AnswerSubtitle}\\par\n"
            "\\vspace{0.5em}\n"
            "{\\color{answerprimary}\\rule{0.72\\textwidth}{1.1pt}}\\par\n"
            "\\end{center}\n"
            "\\vspace{0.5em}\n"
            f"{summary_box}"
            f"{question_card}"
            "\\vspace{0.5em}\n"
            f"{self._markdown_to_tex(body)}\n"
            f"{image_block}"
            f"{pdf_block}"
            "\\end{document}\n"
        )

    def _build_document_title(self, question_text: str, body: str) -> str:
        # 标题回退策略：仅截取问题原文，不再按关键词猜测分类标题
        seed = (question_text or body or "智能回答").strip()
        normalized = re.sub(r"\s+", " ", seed)
        title = normalized[:28].strip("，。；：,.!?！？ ")
        return title or "智能回答"

    def _build_document_subtitle(self, question_text: str) -> str:
        question = re.sub(r"\s+", " ", (question_text or "").strip())
        if not question:
            return "ElegantBook 智能解答"
        snippet = question[:36].strip("，。；：,.!?！？ ")
        return f"围绕问题：{snippet}"

    def _build_question_card(self, question_text: str) -> str:
        question = (question_text or "").strip()
        if not question:
            return ""
        escaped = self._tex_inline(question[:1800])
        return (
            "\\begin{answerkeybox}\n"
            "\\textbf{题目聚焦}\\quad " + escaped + "\n"
            "\\end{answerkeybox}\n"
        )

    def _markdown_to_tex(self, text: str) -> str:
        text = self._normalize_latex_blocks(text)
        lines = text.splitlines()
        output: list[str] = []
        in_itemize = False
        in_enumerate = False
        raw_list_envs: list[str] = []
        in_code = False
        code_fence_lang = ""
        in_display_math = False
        display_math_lines: list[str] = []
        code_lines: list[str] = []
        in_latex_env = False
        latex_env_name = ""
        latex_env_header = ""
        latex_env_body: list[str] = []
        block_env: Optional[str] = None
        block_lines: list[str] = []

        env_pattern = r"definition|theorem|property|example|remark|note|proof|solution"

        def semantic_environment(title: str) -> Optional[str]:
            normalized = re.sub(r"\s+", "", title)
            if re.match(r"^(定义|Definition)", normalized, re.IGNORECASE):
                return "definition"
            if re.match(r"^(定理|Theorem)", normalized, re.IGNORECASE):
                return "theorem"
            if re.match(r"^(性质|Property)", normalized, re.IGNORECASE):
                return "property"
            if re.match(r"^(例|例题|Example)", normalized, re.IGNORECASE):
                return "example"
            if re.match(r"^(注|备注|Remark)", normalized, re.IGNORECASE):
                return "remark"
            if re.match(r"^(笔记|Note)", normalized, re.IGNORECASE):
                return "note"
            if re.match(r"^(证明|Proof)", normalized, re.IGNORECASE):
                return "proof"
            if re.match(r"^(步骤|思路|分析|做法)", normalized, re.IGNORECASE):
                return "solution"
            if re.match(r"^(结论|结果|总结|答案)", normalized, re.IGNORECASE):
                return "remark"
            if re.match(r"^(注意|提示|说明)", normalized, re.IGNORECASE):
                return "note"
            return None

        def emit(content: str) -> None:
            if block_env is not None:
                block_lines.append(content)
            else:
                output.append(content)

        def flush_block() -> None:
            nonlocal block_env, block_lines
            if block_env is None:
                return
            inner = "\n".join(block_lines).strip()
            if inner:
                output.append(f"\\begin{{{block_env}}}")
                output.append(inner)
                output.append(f"\\end{{{block_env}}}")
            block_env = None
            block_lines = []

        def close_lists() -> None:
            nonlocal in_itemize, in_enumerate
            if in_itemize:
                emit("\\end{itemize}")
                in_itemize = False
            if in_enumerate:
                emit("\\end{enumerate}")
                in_enumerate = False

        for raw_line in lines:
            line = raw_line.rstrip()
            stripped = line.strip()

            if in_latex_env:
                end_match = re.match(r"^\\end\{(" + env_pattern + r")\}$", stripped)
                if end_match and end_match.group(1) == latex_env_name:
                    inner = "\n".join(latex_env_body).strip()
                    rendered_inner = self._markdown_to_tex(inner) if inner else ""
                    header_to_emit = latex_env_header
                    if latex_env_name == "example":
                        option_match = re.match(r"^\\begin\{example\}\[(.*)\]$", latex_env_header)
                        if option_match:
                            title_line = self._markdown_to_tex(option_match.group(1).strip())
                            header_to_emit = r"\begin{example}"
                            rendered_inner = (title_line + "\n\n" + rendered_inner).strip() if rendered_inner else title_line
                    emit(header_to_emit)
                    if rendered_inner:
                        emit(rendered_inner)
                    emit(f"\\end{{{latex_env_name}}}")
                    latex_env_body = []
                    latex_env_name = ""
                    latex_env_header = ""
                    in_latex_env = False
                else:
                    latex_env_body.append(line)
                continue

            begin_match = re.match(r"^(\\begin\{(" + env_pattern + r")\}(?:\[.*\])?)\s*$", stripped)
            if begin_match:
                close_lists()
                flush_block()
                in_latex_env = True
                latex_env_header = begin_match.group(1)
                latex_env_name = begin_match.group(2)
                latex_env_body = []
                continue

            if in_display_math:
                display_math_lines.append(line)
                if stripped in {r"\]", "$$"}:
                    for ml in display_math_lines:
                        emit(self._normalize_math_span(ml))
                    display_math_lines = []
                    in_display_math = False
                continue
            if stripped in {r"\[", "$$"}:
                close_lists()
                in_display_math = True
                display_math_lines = [line]
                continue

            if stripped.startswith("```"):
                fence_lang = stripped[3:].strip().lower()
                if in_code:
                    if code_fence_lang in {"latex", "tex", "tikz"}:
                        close_lists()
                        flush_block()
                        for cl in code_lines:
                            output.append(cl)
                    else:
                        emit("\\begin{quote}\\ttfamily\\small")
                        for cl in code_lines:
                            emit(self._escape_tex(cl))
                        emit("\\end{quote}")
                    code_lines = []
                    code_fence_lang = ""
                    in_code = False
                else:
                    close_lists()
                    flush_block()
                    in_code = True
                    code_fence_lang = fence_lang
                    code_lines = []
                continue
            if in_code:
                code_lines.append(line)
                continue

            if not stripped:
                close_lists()
                emit("")
                continue
            raw_list_begin = re.fullmatch(r"\\begin\{(itemize|enumerate)\}", stripped)
            if raw_list_begin:
                close_lists()
                raw_list_envs.append(raw_list_begin.group(1))
                emit(stripped)
                continue
            raw_list_end = re.fullmatch(r"\\end\{(itemize|enumerate)\}", stripped)
            if raw_list_end and raw_list_envs and raw_list_envs[-1] == raw_list_end.group(1):
                close_lists()
                raw_list_envs.pop()
                emit(stripped)
                continue
            raw_item = re.fullmatch(r"\\item(?:\s+(.*))?", stripped)
            if raw_list_envs and raw_item:
                close_lists()
                emit("\\item " + self._tex_inline(raw_item.group(1) or ""))
                continue
            if stripped in {"---", "***", "___"}:
                close_lists()
                flush_block()
                output.append("\\begin{center}\\color{answerline}\\rule{0.82\\textwidth}{0.8pt}\\end{center}")
                continue
            if stripped.startswith(">"):
                close_lists()
                quote_text = stripped.lstrip("> ").strip()
                emit(f"\\begin{{note}}{self._tex_inline(quote_text)}\\end{{note}}")
                continue

            heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
            if heading:
                close_lists()
                level = len(heading.group(1))
                raw_title = heading.group(2).strip()
                env_name = semantic_environment(raw_title)
                if env_name:
                    flush_block()
                    block_env = env_name
                    continue
                flush_block()
                title = self._tex_inline(raw_title)
                if level == 1:
                    output.append(f"\\section*{{{title}}}")
                elif level == 2:
                    output.append(f"\\subsection*{{{title}}}")
                else:
                    output.append(f"\\paragraph*{{{title}}}")
                continue

            bullet = re.match(r"^[-*+]\s+(.+)$", stripped)
            if bullet:
                if in_enumerate:
                    emit("\\end{enumerate}")
                    in_enumerate = False
                if not in_itemize:
                    emit("\\begin{itemize}")
                    in_itemize = True
                emit(f"\\item {self._tex_inline(bullet.group(1))}")
                continue
            numbered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
            if numbered:
                if in_itemize:
                    emit("\\end{itemize}")
                    in_itemize = False
                if not in_enumerate:
                    emit("\\begin{enumerate}")
                    in_enumerate = True
                emit(f"\\item {self._tex_inline(numbered.group(1))}")
                continue

            close_lists()
            emit(self._tex_inline(stripped))

        if in_display_math:
            for ml in display_math_lines:
                emit(self._normalize_math_span(ml))
        if in_latex_env:
            inner = "\n".join(latex_env_body).strip()
            emit(latex_env_header)
            if inner:
                emit(self._markdown_to_tex(inner))
            emit(f"\\end{{{latex_env_name}}}")
        if in_code:
            if code_fence_lang in {"latex", "tex", "tikz"}:
                for cl in code_lines:
                    output.append(cl)
            else:
                emit("\\begin{quote}\\ttfamily\\small")
                for cl in code_lines:
                    emit(self._escape_tex(cl))
                emit("\\end{quote}")
        close_lists()
        flush_block()
        return "\n".join(output)

    def _normalize_latex_blocks(self, text: str) -> str:
        text = text.replace("\u200b", "")
        text = re.sub(r"\\left\s*\{", r"\\left\\{", text)
        text = re.sub(r"\\right\s*\}", r"\\right\\}", text)
        text = re.sub(r"\\bigl\s*\{", r"\\bigl\\{", text)
        text = re.sub(r"\\bigr\s*\}", r"\\bigr\\}", text)
        text = re.sub(r"\\Bigl\s*\{", r"\\Bigl\\{", text)
        text = re.sub(r"\\Bigr\s*\}", r"\\Bigr\\}", text)
        text = text.replace("\\{", "{").replace("\\}", "}")
        text = re.sub(r"\\left\s*\{", r"\\left\\{", text)
        text = re.sub(r"\\right\s*\}", r"\\right\\}", text)
        text = text.replace("\\_", "_")
        text = text.replace("\\%", "%").replace("\\&", "&")
        return text

    def _sanitize_math_percent(self, text: str) -> str:
        """数学模式里裸 % 是注释符：吞掉行尾后会让公式定界符失效、编译报
        `Missing $ inserted`。把未转义的 % 补成 \\%（已转义的 \\% 不动）。"""
        return re.sub(r"(?<!\\)%", r"\\%", text)

    def _normalize_math_span(self, text: str) -> str:
        text = self._sanitize_math_percent(text)
        return re.sub(r"\^\\sqrt\{([^{}]+)\}", r"^{\\sqrt{\1}}", text)

    def _tex_inline(self, text: str) -> str:
        math_chunks: list[str] = []
        code_chunks: list[str] = []

        def stash_math(match: re.Match[str]) -> str:
            math_chunks.append(match.group(0))
            return f"@@MATH{len(math_chunks) - 1}@@"

        def stash_code(match: re.Match[str]) -> str:
            code_chunks.append(match.group(1))
            return f"@@CODE{len(code_chunks) - 1}@@"

        masked = MATH_SPAN_PATTERN.sub(stash_math, text)
        masked = re.sub(r"`([^`]+)`", stash_code, masked)
        bold_chunks: list[str] = []

        def stash_bold(match: re.Match[str]) -> str:
            bold_chunks.append(match.group(1))
            return f"@@BOLD{len(bold_chunks) - 1}@@"

        masked = re.sub(r"\\textbf\{([^{}]*)\}", stash_bold, masked)
        masked = self._escape_tex(masked)
        masked = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", masked)
        masked = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\\emph{\1}", masked)
        for index, value in enumerate(bold_chunks):
            masked = masked.replace(f"@@BOLD{index}@@", f"\\textbf{{{self._escape_tex(value)}}}")
        for index, value in enumerate(code_chunks):
            masked = masked.replace(f"@@CODE{index}@@", f"\\texttt{{{self._escape_tex(value)}}}")
        for index, value in enumerate(math_chunks):
            # 数学段原样还原，但裸 % 必须转义，否则编译炸在公式定界符上
            masked = masked.replace(f"@@MATH{index}@@", self._normalize_math_span(value))
        return masked

    def _escape_tex_metadata(self, text: str) -> str:
        return (
            text.replace("\\", r"\textbackslash{}")
            .replace("&", r"\&")
            .replace("%", r"\%")
            .replace("$", r"\$")
            .replace("#", r"\#")
            .replace("_", r"\_")
            .replace("{", r"\{")
            .replace("}", r"\}")
            .replace("~", r"\textasciitilde{}")
            .replace("^", r"\textasciicircum{}")
        )

    def build_compile_error_message(self, compile_result: CompileResult) -> str:
        log_snippet = self._read_log_snippet(compile_result.log_path)
        stderr_snippet = (compile_result.stderr or "").strip()
        stdout_snippet = (compile_result.stdout or "").strip()
        parts = ["编译失败"]
        if stderr_snippet:
            parts.append(f"stderr: {stderr_snippet[:500]}")
        if stdout_snippet:
            parts.append(f"stdout: {stdout_snippet[:500]}")
        if log_snippet:
            parts.append(f"log: {log_snippet[:800]}")
        return "\n".join(parts)

    def _get_config_value(self, key: str, env_key: str, default: Any = "") -> Any:
        if self.config is not None:
            value = self.config.get(key, None)
            if value not in (None, ""):
                return value
        return os.environ.get(env_key, default)

    def _remember_available_model(self, model_name: str) -> None:
        model_name = (model_name or "").strip()
        if not model_name:
            return
        if model_name not in self.available_models:
            self.available_models.insert(0, model_name)

    def _set_active_model(self, model_name: str) -> None:
        model_name = (model_name or "").strip()
        if not model_name:
            return
        self.active_model_name = model_name
        self._remember_available_model(model_name)

    def _fetch_remote_models(self) -> list[str]:
        if not self.text_api_url:
            return []
        request = urllib.request.Request(f"{self.text_api_url}/models")
        if self.text_api_key:
            request.add_header("Authorization", f"Bearer {self.text_api_key}")
            request.add_header("User-Agent", f"astrbot-plugin-xelatex-pdf/{PLUGIN_VERSION}")
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models: list[str] = []
        raw_models = payload.get("data", []) if isinstance(payload, dict) else []
        for item in raw_models:
            model_id = None
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
            elif item is not None:
                model_id = str(item)
            if model_id:
                models.append(str(model_id))
        return models

    def _format_model_list(self, session_id: Optional[str] = None) -> str:
        remote_models: list[str] = []
        remote_error = ""
        try:
            remote_models = self._fetch_remote_models()
        except Exception as exc:
            remote_error = str(exc)
        if remote_models:
            self.available_models = remote_models
            self._remember_available_model(self.active_model_name)
        models = remote_models or self.available_models or [self.active_model_name]
        lines = [
            f"当前模型：{self.active_model_name}",
            "可用模型：" + ", ".join(models),
        ]
        if session_id is not None:
            pending = self.pending_model_by_session.get(session_id)
            if pending:
                lines.append(f"本会话下一题已指定：{pending}（一次性，不影响全局）")
        if remote_error:
            lines.append(f"远程查询失败，已使用本地列表：{remote_error[:200]}")
        return "\n".join(lines)

    def _read_log_snippet(self, log_path: Optional[Path]) -> str:
        if log_path is None or not log_path.exists():
            return ""
        try:
            content = log_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        return content[-2000:]

    def _sanitize_math_in_body(self, tex_source: str) -> str:
        """只对 \\begin{document} 之后的正文区做数学段 % 净化。

        导言区由模板生成，其中的 % 是有意的行尾注释（如 \\fancypagestyle{...}{%），
        绝不能转义；正文区的数学片段里裸 % 才是编译炸弹。
        """

        def sanitize_spans(region: str) -> str:
            return MATH_SPAN_PATTERN.sub(
                lambda match: self._sanitize_math_percent(match.group(0)),
                region,
            )

        begin_marker = "\\begin{document}"
        end_marker = "\\end{document}"
        begin_index = tex_source.find(begin_marker)
        end_index = tex_source.rfind(end_marker)
        if begin_index == -1 or end_index == -1 or end_index <= begin_index:
            return sanitize_spans(tex_source)
        body_start = begin_index + len(begin_marker)
        return (
            tex_source[:body_start]
            + sanitize_spans(tex_source[body_start:end_index])
            + tex_source[end_index:]
        )

    def _repair_tex_source(self, tex_source: str, compile_result: CompileResult) -> Optional[str]:
        """证据式修复：仅当日志/输出里出现已识别的错误模式时才尝试修复，
        修复后与原文相同则返回 None（不触发无意义重试）。

        当前覆盖的模式：
        - `Missing $ inserted`：数学模式里裸 % 吞掉公式定界符 → 净化数学段 %。
        """
        evidence = " ".join(
            part
            for part in (
                compile_result.stderr or "",
                compile_result.stdout or "",
                self._read_log_snippet(compile_result.log_path),
            )
            if part
        )
        if "Missing $ inserted" not in evidence:
            return None
        repaired = self._sanitize_math_in_body(tex_source)
        if repaired == tex_source:
            return None
        return repaired

    def _build_image_block(self, images: list[MessageAttachment]) -> str:
        if not images:
            return ""
        lines = ["\\begin{remark}以下为与本次解答相关的图片或图形附件，可结合正文说明进行阅读。\\end{remark}", "\\section*{图片附件}"]
        for image in images:
            reference = self._tex_file_reference(image.path or image.url or image.name)
            if not reference:
                continue
            lines.append("\\begin{figure}[h]")
            lines.append("\\centering")
            lines.append(f"\\includegraphics[width=0.9\\linewidth]{{{reference}}}")
            lines.append("\\end{figure}")
        return "\n".join(lines) + "\n"

    def _build_pdf_block(self, pdfs: list[MessageAttachment]) -> str:
        if not pdfs:
            return ""
        lines = ["\\begin{note}以下为输入 PDF 的提取内容或引用说明。\\end{note}", "\\section*{PDF 附件}"]
        for pdf in pdfs:
            source = pdf.path or pdf.url or pdf.name
            if not source:
                continue
            extracted_text = self._extract_pdf_text(source)
            if extracted_text:
                lines.append("\\subsection*{PDF 文本}")
                lines.append(self._escape_tex(extracted_text[:6000]))
            else:
                lines.append(f"\\noindent 附件：\\texttt{{{self._escape_tex(Path(source).name or source)}}}\\\\")
        return "\n".join(lines) + "\n"

    def _extract_pdf_text(self, source: str) -> str:
        local_source = source.replace("file://", "")
        if not local_source or not os.path.exists(local_source):
            return ""
        if shutil.which("pdftotext") is not None:
            try:
                result = subprocess.run(
                    ["pdftotext", "-layout", local_source, "-"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=PROCESS_TIMEOUT_SECONDS,
                )
            except Exception:
                result = None
            else:
                text = (result.stdout or "").strip()
                if text:
                    return text
        return self._ocr_pdf_or_image(local_source)

    def _ocr_pdf_or_image(self, local_source: str) -> str:
        tesseract = shutil.which("tesseract")
        if tesseract is None:
            return ""

        suffix = Path(local_source).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
            return self._ocr_image(local_source, tesseract)
        if suffix != ".pdf":
            return ""

        pdftoppm = shutil.which("pdftoppm")
        if pdftoppm is None:
            return ""

        temp_dir = Path(tempfile.mkdtemp(prefix="xelatex_ocr_"))
        try:
            prefix = temp_dir / "page"
            subprocess.run(
                [pdftoppm, "-png", "-singlefile", local_source, str(prefix)],
                capture_output=True,
                text=True,
                check=False,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            image_path = temp_dir / "page.png"
            if not image_path.exists():
                return ""
            return self._ocr_image(str(image_path), tesseract)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _ocr_image(self, image_path: str, tesseract: str) -> str:
        try:
            result = subprocess.run(
                [tesseract, image_path, "stdout", "-l", os.environ.get("XELATEX_OCR_LANG", "chi_sim+eng")],
                capture_output=True,
                text=True,
                check=False,
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
        except Exception:
            return ""
        return (result.stdout or "").strip()

    def _tex_file_reference(self, reference: str) -> str:
        return Path(reference.replace("file://", "")).as_posix()

    def localize_attachments(self, parsed_message: ParsedMessage) -> ParsedMessage:
        parsed_message.images = self._localize_attachments(parsed_message.images)
        parsed_message.pdfs = self._localize_attachments(parsed_message.pdfs)
        # 拍照/转发的截图常整体侧转 90°，视觉 OCR 对侧转文字会整段编造题目；
        # 在 OCR 与 PDF 插图之前统一转正（OCR 和 \includegraphics 读的是同一个文件）
        if not getattr(self, "orientation_correction_enabled", True):
            return parsed_message
        budget = OrientationBudget(deadline=time.monotonic() + ORIENTATION_BUDGET_SECONDS)
        for image in parsed_message.images:
            self._normalize_image_orientation(image, budget)
        return parsed_message

    def _normalize_image_orientation(self, attachment: MessageAttachment, budget: Optional[OrientationBudget] = None) -> None:
        """把已本地化到 assets/ 的图片原地转正：先应用 EXIF 方向，再请视觉模型挑正向。

        只改插件自己下载的副本；任何失败都保持原图不动，不影响后续问答。
        判定只用缩略图，确认要旋转时才整图解码一次。
        """
        local_path = Path(attachment.path) if attachment.path else None
        asset_dir = getattr(self, "asset_dir", None)
        if local_path is None or asset_dir is None:
            return
        try:
            if not local_path.is_file() or local_path.resolve().parent != Path(asset_dir).resolve():
                return
        except (OSError, ValueError):
            return
        try:
            from PIL import Image, ImageOps  # type: ignore
        except Exception:
            return
        if budget is None:
            budget = OrientationBudget(deadline=time.monotonic() + ORIENTATION_BUDGET_SECONDS)

        try:
            with Image.open(local_path) as opened:
                image_format = opened.format
                # 动图只转首帧会丢动画，直接跳过；超大图不在进程内解码
                if image_format not in {"JPEG", "PNG", "WEBP", "BMP", "TIFF"} or getattr(opened, "n_frames", 1) > 1:
                    return
                if opened.width * opened.height > ORIENTATION_MAX_PIXELS:
                    return
                exif_rotated = opened.getexif().get(0x0112, 1) not in (0, 1)
                width, height = opened.size
                # JPEG 走 draft 按比例降采样解码，判定阶段只持有缩略图大小的内存
                opened.draft("RGB", (ORIENTATION_THUMBNAIL_SIZE, ORIENTATION_THUMBNAIL_SIZE))
                if exif_rotated:
                    preview = ImageOps.exif_transpose(opened)
                    preview.thumbnail((ORIENTATION_THUMBNAIL_SIZE, ORIENTATION_THUMBNAIL_SIZE))
                else:
                    opened.thumbnail((ORIENTATION_THUMBNAIL_SIZE, ORIENTATION_THUMBNAIL_SIZE))
                    preview = opened.copy()
        except Exception:
            return

        angle = 0
        if max(width, height) <= ORIENTATION_MAX_ASPECT * min(width, height):
            angle = self._detect_upright_rotation(preview, budget)
        if not angle and not exif_rotated:
            return
        if self._rewrite_rotated_image(local_path, image_format, angle):
            logger.info("图片方向已转正 file=%s exif=%s rotate_ccw=%d", local_path.name, exif_rotated, angle)

    def _rewrite_rotated_image(self, local_path: Path, image_format: str, angle: int) -> bool:
        """整图解码 → 应用 EXIF 方向与逆时针 angle 度旋转 → 原子替换；失败保持原图并返回 False。"""
        from PIL import Image, ImageOps  # type: ignore

        # transpose 是无损的像素重排
        rotations = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}
        temp_path: Optional[Path] = None
        try:
            # 同一源并发本地化时各写各的临时文件，互不踩踏
            fd, temp_name = tempfile.mkstemp(prefix=f".{local_path.name}.", suffix=".rotating", dir=local_path.parent)
            os.close(fd)
            temp_path = Path(temp_name)
            with Image.open(local_path) as opened:
                icc_profile = opened.info.get("icc_profile")
                image = ImageOps.exif_transpose(opened)
            if angle:
                image = image.transpose(rotations[angle])
            save_options: dict[str, Any] = {"icc_profile": icc_profile} if icc_profile else {}
            if image_format == "JPEG":
                # 不写回 EXIF：方向已烘焙进像素，保留方向标签会被查看器二次旋转
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(temp_path, format="JPEG", quality=95, **save_options)
            elif image_format == "WEBP":
                image.save(temp_path, format="WEBP", lossless=True, **save_options)
            else:
                image.save(temp_path, format=image_format)
            os.replace(temp_path, local_path)
        except Exception as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            logger.warning("图片方向转正写回失败（保持原图） file=%s err=%s", local_path.name, exc)
            return False
        return True

    def _detect_upright_rotation(self, preview: Any, budget: OrientationBudget) -> int:
        """返回让图片文字正立需要的逆时针旋转角度（0/90/180/270），判定不了返回 0。

        把同一张图的四种旋转并排给视觉模型挑正向的那张（比较题比方向判断题稳得多）。
        判为需要旋转时再倒序问一次，两次一致才采纳：误转一张本来正的图，
        比不转一张侧着的图更糟。只用主视觉模型，备用模型未验证过这项能力。
        """
        if not getattr(self, "ocr_api_url", "") or not getattr(self, "ocr_api_key", "") or not getattr(self, "vision_model_name", ""):
            return 0
        try:
            from PIL import Image  # type: ignore

            # 透明底直接转 RGB 会变成全黑，四张候选一模一样，先铺白底
            if preview.mode in {"RGBA", "LA", "PA"} or (preview.mode == "P" and "transparency" in preview.info):
                rgba = preview.convert("RGBA")
                thumbnail = Image.new("RGB", rgba.size, (255, 255, 255))
                thumbnail.paste(rgba, mask=rgba.getchannel("A"))
            else:
                thumbnail = preview.convert("RGB")
            candidates: dict[int, str] = {}
            for angle, method in ((0, None), (90, Image.Transpose.ROTATE_90), (180, Image.Transpose.ROTATE_180), (270, Image.Transpose.ROTATE_270)):
                rotated = thumbnail if method is None else thumbnail.transpose(method)
                buffer = io.BytesIO()
                rotated.save(buffer, format="JPEG", quality=80)
                candidates[angle] = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception:
            return 0

        first = self._ask_upright_candidate(candidates, [0, 90, 180, 270], budget)
        if not first:
            return 0
        confirmed = self._ask_upright_candidate(candidates, [270, 180, 90, 0], budget)
        if confirmed != first:
            logger.info("图片方向两次判定不一致（保持原图） first=%s confirm=%s", first, confirmed)
            return 0
        return first

    def _ask_upright_candidate(self, candidates: dict[int, str], order: list[int], budget: OrientationBudget) -> Optional[int]:
        timeout = budget.timeout()
        if timeout <= 0:
            return None
        content: list[dict[str, Any]] = [{"type": "text", "text": ORIENTATION_PROMPT}]
        for index, angle in enumerate(order, 1):
            content.append({"type": "text", "text": f"图 {index}："})
            content.append({"type": "image_url", "image_url": {"url": candidates[angle]}})
        try:
            data = self._post_chat_completion(
                api_url=self.ocr_api_url,
                api_key=self.ocr_api_key,
                payload={
                    "model": self.vision_model_name,
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": content}],
                },
                timeout=timeout,
            )
            answer = self._extract_chat_content(data)
        except Exception as exc:
            # 429/超时后本条消息的其余图片不再判定：别在限流的 key 上、紧挨着 OCR 继续加请求
            budget.failed = True
            logger.warning("图片方向判定调用失败（保持原图，本条消息停止判定） err=%s", exc)
            return None
        match = re.search(r"[1-4]", answer or "")
        if match is None:
            return None
        return order[int(match.group()) - 1]


    def _localize_attachments(self, attachments: list[MessageAttachment]) -> list[MessageAttachment]:
        localized: list[MessageAttachment] = []
        for item in attachments:
            localized_item = self._localize_attachment(item)
            if localized_item is not None:
                localized.append(localized_item)
        return localized

    def _localize_attachment(self, attachment: MessageAttachment) -> Optional[MessageAttachment]:
        source = attachment.url or attachment.path
        if not source:
            return attachment

        source_path = urllib.parse.urlsplit(source).path if source.startswith(("http://", "https://")) else source
        requested_suffix = Path(attachment.name or source_path).suffix.lower()
        if attachment.kind == "pdf":
            target_suffix = ".pdf"
        else:
            allowed_image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
            target_suffix = requested_suffix if requested_suffix in allowed_image_suffixes else ".png"
        source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]
        target_name = f"{attachment.kind}_{source_digest}{target_suffix}"
        target_path = self.asset_dir / target_name

        try:
            if source.startswith(("http://", "https://")):
                request = urllib.request.Request(source, headers={"User-Agent": f"astrbot-plugin-xelatex-pdf/{PLUGIN_VERSION}"})
                with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > MAX_ATTACHMENT_BYTES:
                        return attachment
                    total = 0
                    with target_path.open("wb") as target:
                        while chunk := response.read(64 * 1024):
                            total += len(chunk)
                            if total > MAX_ATTACHMENT_BYTES:
                                raise ValueError("附件超过大小限制")
                            target.write(chunk)
            else:
                local_source = source.replace("file://", "")
                if os.path.exists(local_source):
                    if Path(local_source).stat().st_size > MAX_ATTACHMENT_BYTES:
                        return attachment
                    shutil.copyfile(local_source, target_path)
                else:
                    return attachment
        except Exception:
            target_path.unlink(missing_ok=True)
            return attachment

        return MessageAttachment(
            kind=attachment.kind,
            name=attachment.name or target_path.name,
            url=target_path.as_uri(),
            path=str(target_path),
            raw=attachment.raw,
        )

    def _escape_url_text(self, text: str) -> str:
        return text.replace("\\", "/").replace("%", r"\%").replace("{", r"\{").replace("}", r"\}")

    def _escape_tex(self, text: str) -> str:
        return (
            text.replace("\\", r"\textbackslash{}")
            .replace("&", r"\&")
            .replace("%", r"\%")
            .replace("$", r"\$")
            .replace("#", r"\#")
            .replace("_", r"\_")
            .replace("{", r"\{")
            .replace("}", r"\}")
            .replace("~", r"\textasciitilde{}")
            .replace("^", r"\textasciicircum{}")
        )

    def get_session_id(self, event: Any) -> str:
        if event is None:
            return "default"
        session_id = getattr(event, "session_id", None)
        if session_id:
            return str(session_id)
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        sender_id = getattr(sender, "user_id", "unknown_user")
        group_id = getattr(message_obj, "group_id", getattr(event, "group_id", "private"))
        return f"{group_id}_{sender_id}"

    def append_history(self, session_id: str, user_text: str, assistant_text: str) -> None:
        memory = self.chat_history.get(session_id)
        if memory is None:
            memory = SessionMemory(session_id=session_id)
            self.chat_history[session_id] = memory
        memory.append(user_text, assistant_text, limit=self.max_history_rounds)
        self._save_history()

    def clear_session_history(self, session_id: str) -> None:
        memory = self.chat_history.get(session_id)
        if memory is None:
            return
        memory.clear()
        self._save_history()

    def _load_history(self) -> dict[str, SessionMemory]:
        if not self.history_file.exists():
            return {}
        try:
            data = json.loads(self.history_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

        loaded: dict[str, SessionMemory] = {}
        if isinstance(data, dict):
            for session_id, rounds in data.items():
                if not isinstance(rounds, list):
                    continue
                memory = SessionMemory(session_id=session_id)
                for item in rounds:
                    if not isinstance(item, dict):
                        continue
                    memory.rounds.append(
                        {
                            "user": str(item.get("user", "")),
                            "assistant": str(item.get("assistant", "")),
                        }
                    )
                loaded[session_id] = memory
        return loaded

    def _save_history(self) -> None:
        payload = {
            session_id: memory.rounds
            for session_id, memory in self.chat_history.items()
            if memory.rounds
        }
        self.history_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_event_text(self, message: Any) -> str:
        if isinstance(message, str):
            return message.strip()
        for attr in ("message_str", "raw_message"):
            value = getattr(message, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        getter = getattr(message, "get_message_str", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = None
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _extract_components(self, message: Any) -> list[Any]:
        if isinstance(message, str):
            return []
        if isinstance(message, list):
            return message
        for source in (message, getattr(message, "message_obj", None)):
            if source is None:
                continue
            for attr in ("message", "components", "chain", "message_chain"):
                value = getattr(source, attr, None)
                if isinstance(value, list):
                    return value
        return [message]

    def _get_component_text(self, component: Any) -> str:
        if isinstance(component, str):
            return component.strip()
        for attr in ("text", "content", "message"):
            value = getattr(component, attr, None)
            if isinstance(value, str):
                return value.strip()
        return ""

    async def _extract_reply_payload(self, event: Any, component: Any) -> dict[str, Any]:
        direct_text = ""
        for attr in ("text", "content", "message"):
            value = getattr(component, attr, None)
            if isinstance(value, str) and value.strip():
                direct_text = value.strip()
                break

        chain = getattr(component, "chain", None)
        if isinstance(chain, list) and chain:
            return self._parse_reply_components(chain, fallback_text=direct_text)

        fetched = await self._fetch_reply_message_via_adapter(event, component)
        if fetched is not None:
            return self._parse_reply_message_data(fetched, fallback_text=direct_text)

        return {"text": direct_text, "images": [], "pdfs": []}

    def _parse_reply_components(self, components: list[Any], fallback_text: str = "") -> dict[str, Any]:
        texts: list[str] = []
        images: list[MessageAttachment] = []
        pdfs: list[MessageAttachment] = []

        if fallback_text:
            texts.append(fallback_text)

        for item in components:
            text = self._get_component_text(item)
            if text:
                texts.append(text)
            attachment = self._component_to_attachment(item)
            if attachment is None:
                continue
            if attachment.kind == "image":
                images.append(attachment)
            elif attachment.kind == "pdf":
                pdfs.append(attachment)

        attachment_refs = [attachment.url or attachment.path or attachment.name for attachment in [*images, *pdfs]]
        parts = [text for text in texts if text]
        parts.extend(f"[引用附件] {ref}" for ref in attachment_refs if ref)
        return {
            "text": "\n".join(parts),
            "images": self._dedupe_attachments(images),
            "pdfs": self._dedupe_attachments(pdfs),
        }

    async def _fetch_reply_message_via_adapter(self, event: Any, component: Any) -> Any:
        possible_id_attrs = ["start_id", "id", "message_id", "msg_id", "reply_id", "target_id"]
        target_msg_id = None
        for attr in possible_id_attrs:
            attr_value = getattr(component, attr, None)
            if attr_value:
                target_msg_id = attr_value
                break
        if not target_msg_id:
            return None

        platform_name = ""
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                platform_name = getter() or ""
            except Exception:
                platform_name = ""
        if not platform_name or self.context is None or not hasattr(self.context, "get_platform_inst"):
            return None

        adapter = None
        candidate_names = [platform_name, "default", "qq", "qq_official", "aiocqhttp", "OneBot"]
        seen_names: set[str] = set()
        for candidate in candidate_names:
            candidate = (candidate or "").strip()
            if not candidate or candidate in seen_names:
                continue
            seen_names.add(candidate)
            try:
                adapter = self.context.get_platform_inst(candidate)
            except Exception:
                adapter = None
            if adapter:
                break
        if adapter is None:
            return None

        group_id = getattr(getattr(event, "message_obj", None), "group_id", getattr(event, "group_id", None))
        api_methods = [
            ("get_msg", {"message_id": target_msg_id}),
            ("get_group_msg_history", {"group_id": group_id, "message_seq": target_msg_id}),
            ("get_forward_msg", {"id": target_msg_id}),
        ]
        for api_name, params in api_methods:
            if api_name == "get_group_msg_history" and not params.get("group_id"):
                continue
            try:
                caller = getattr(adapter, "call_api", None)
                if not callable(caller):
                    continue
                result = caller(api_name, **params)
                if asyncio.iscoroutine(result):
                    try:
                        result = await result
                    except Exception:
                        continue
                if result:
                    return result
            except Exception:
                continue
        return None

    def _parse_reply_message_data(self, msg_data: Any, fallback_text: str = "") -> dict[str, Any]:
        actual_msg = None
        if isinstance(msg_data, dict):
            msg_paths = [
                ["message"],
                ["data", "message"],
                ["data", "messages"],
                ["messages"],
                ["content"],
            ]
            for path in msg_paths:
                temp_data = msg_data
                try:
                    for key in path:
                        temp_data = temp_data.get(key)
                        if temp_data is None:
                            break
                    if temp_data is not None:
                        actual_msg = temp_data
                        break
                except Exception:
                    continue
        else:
            actual_msg = msg_data

        texts: list[str] = [fallback_text] if fallback_text else []
        images: list[MessageAttachment] = []
        pdfs: list[MessageAttachment] = []

        if isinstance(actual_msg, list):
            for segment in actual_msg:
                if not isinstance(segment, dict):
                    continue
                seg_type = str(segment.get("type", "") or "").lower()
                seg_data = segment.get("data", {}) if isinstance(segment.get("data", {}), dict) else {}
                if seg_type == "text":
                    text = str(seg_data.get("text", "") or "").strip()
                    if text:
                        texts.append(text)
                    continue
                name = str(seg_data.get("name", "") or seg_data.get("file", "") or "")
                url = str(seg_data.get("url", "") or seg_data.get("file", "") or "")
                path = str(seg_data.get("path", "") or "")
                lower_name = name.lower() or url.lower() or path.lower()
                if seg_type == "image" or lower_name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")):
                    images.append(MessageAttachment(kind="image", name=name, url=url, path=path))
                elif seg_type == "file" and lower_name.endswith(".pdf"):
                    pdfs.append(MessageAttachment(kind="pdf", name=name, url=url, path=path))
        elif isinstance(actual_msg, str):
            cq_matches = re.findall(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]", actual_msg)
            for seg_type, raw_params in cq_matches:
                params: dict[str, str] = {}
                if raw_params:
                    for pair in raw_params.split(","):
                        if "=" not in pair:
                            continue
                        key, value = pair.split("=", 1)
                        params[key] = value
                seg_type = seg_type.lower()
                name = str(params.get("name", "") or params.get("file", "") or "")
                url = str(params.get("url", "") or params.get("file", "") or "")
                path = str(params.get("path", "") or "")
                lower_name = name.lower() or url.lower() or path.lower()
                if seg_type == "image" or lower_name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")):
                    images.append(MessageAttachment(kind="image", name=name, url=url, path=path))
                elif seg_type == "file" and lower_name.endswith(".pdf"):
                    pdfs.append(MessageAttachment(kind="pdf", name=name, url=url, path=path))
            pure_text = re.sub(r"\[CQ:[^\]]+\]", "", actual_msg).strip()
            if pure_text:
                texts.append(pure_text)

        attachment_refs = [attachment.url or attachment.path or attachment.name for attachment in [*images, *pdfs]]
        parts = [text for text in texts if text]
        parts.extend(f"[引用附件] {ref}" for ref in attachment_refs if ref)
        return {
            "text": "\n".join(parts),
            "images": self._dedupe_attachments(images),
            "pdfs": self._dedupe_attachments(pdfs),
        }

    def _extract_reply_text(self, component: Any) -> str:
        direct_text = ""
        for attr in ("text", "content", "message"):
            value = getattr(component, attr, None)
            if isinstance(value, str) and value.strip():
                direct_text = value.strip()
                break
        chain = getattr(component, "chain", None)
        if isinstance(chain, list) and chain:
            payload = self._parse_reply_components(chain, fallback_text=direct_text)
            return str(payload.get("text", "") or "")
        return direct_text

    def _dedupe_attachments(self, attachments: list[MessageAttachment]) -> list[MessageAttachment]:
        deduped: list[MessageAttachment] = []
        seen: set[tuple[str, str, str, str]] = set()
        for attachment in attachments:
            key = (
                attachment.kind,
                attachment.name or "",
                attachment.url or "",
                attachment.path or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(attachment)
        return deduped

    def _resolve_local_path(self, source: str) -> str:
        source = (source or "").strip()
        if not source:
            return ""

        candidates: list[str] = []
        if source.startswith("file://"):
            candidates.append(source.replace("file://", "", 1))
        else:
            candidates.append(source)

        base_name = Path(candidates[0]).name if candidates else Path(source).name
        if base_name:
            candidates.extend(
                [
                    str(self.asset_dir / base_name),
                    str(self.data_dir / base_name),
                    str(self.export_dir / base_name),
                ]
            )

        seen: set[str] = set()
        for candidate in candidates:
            candidate = (candidate or "").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if os.path.exists(candidate):
                return candidate
        return ""

    def _component_to_attachment(self, component: Any) -> Optional[MessageAttachment]:
        component_type = component.__class__.__name__.lower()
        name = str(getattr(component, "name", "") or getattr(component, "file", "") or "")
        url = str(getattr(component, "url", "") or "")
        path = str(getattr(component, "path", "") or getattr(component, "file_path", "") or "")
        lower_name = name.lower() or url.lower() or path.lower()

        if "image" in component_type:
            return MessageAttachment(kind="image", name=name, url=url, path=path, raw=component)
        if "file" in component_type and lower_name.endswith(".pdf"):
            return MessageAttachment(kind="pdf", name=name, url=url, path=path, raw=component)
        if lower_name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return MessageAttachment(kind="image", name=name, url=url, path=path, raw=component)
        if lower_name.endswith(".pdf"):
            return MessageAttachment(kind="pdf", name=name, url=url, path=path, raw=component)
        return None

    def _extract_command_text(self, parsed_message: ParsedMessage) -> str:
        for line in parsed_message.text.splitlines():
            stripped = line.strip()
            command = stripped.split(maxsplit=1)[0] if stripped else ""
            if command in self.command_aliases:
                return stripped
        if parsed_message.reply_text:
            for line in parsed_message.reply_text.splitlines():
                stripped = line.strip()
                command = stripped.split(maxsplit=1)[0] if stripped else ""
                if command in self.command_aliases:
                    return stripped
        return ""

    def _extract_model_switch(self, text: str) -> Optional[ModelDirective]:
        """解析「指定模型」指令，区分持久切换与单次指定。

        - 「切换到X模型」：持久切换，改全局默认（保留给真想改默认的人）。
        - 「用/使用X模型回答」：单次指定，只影响本题或本会话下一题。
        - 「用/使用X模型回答 问题内容」：单次指定并直接回答同条消息里的问题。
        """
        stripped = text.strip()
        persistent_pattern = r"^切换到\s+(.+?)\s*模型$"
        match = re.match(persistent_pattern, stripped)
        if match:
            return ModelDirective(model=match.group(1).strip(), persistent=True)

        one_shot_with_question = re.compile(
            r"^(?:使用|用)\s+(.+?)\s+模型回答[:：]?\s+(.+)$",
            re.DOTALL,
        )
        match = one_shot_with_question.match(stripped)
        if match:
            return ModelDirective(
                model=match.group(1).strip(),
                persistent=False,
                question=match.group(2).strip(),
            )

        one_shot_pattern = r"^(?:使用|用)\s+(.+?)\s+模型回答[:：]?\s*$"
        match = re.match(one_shot_pattern, stripped)
        if match:
            return ModelDirective(model=match.group(1).strip(), persistent=False)
        return None

    def _apply_model_directive(self, directive: ModelDirective, session_id: str) -> Optional[CommandResponse]:
        """执行模型指令。返回 CommandResponse 表示本轮直接回复；
        返回 None 表示「单次指定且同条带问题」，调用方应继续走回答流程。"""
        if directive.persistent:
            self._set_active_model(directive.model)
            return CommandResponse(success=True, message=f"已切换模型：{directive.model}")
        if directive.question:
            return None
        self.pending_model_by_session[session_id] = directive.model
        return CommandResponse(
            success=True,
            message=f"已为下一题指定模型：{directive.model}（仅本会话下一题生效，不影响全局）",
        )

    def _extract_question_numbers(self, text: str) -> list[int]:
        """指令里点了 ≥2 个题号时返回升序去重的题号，否则返回空列表（按单题处理）。

        认「第8、10题」「8-11题」「第8题和第10题」「第八到十题」；
        带「一起/合并/对比/比较…」等词表示要放在一起讲，不拆分。
        """
        if not text or any(word in text for word in MULTI_QUESTION_OPT_OUT_WORDS):
            return []
        numbers: set[int] = set()
        for match in QUESTION_GROUP_PATTERN.finditer(text):
            parts = QUESTION_GROUP_SEPARATOR.split(match.group(2))
            tokens = [part.lstrip("第") for part in parts[0::2]]
            separators = parts[1::2]
            # 「下一题」「这一题」里的「一」不是题号：中文数字题号必须带「第」
            if not tokens[0].isdigit() and not match.group(1):
                continue
            if text[:match.start()].rstrip().endswith(MULTI_QUESTION_REFERENCE_PREFIXES):
                continue
            values = [self._question_token_to_int(token) for token in tokens]
            if any(value is None or not 1 <= value <= 999 for value in values):
                continue
            group: set[int] = {values[0]}
            for index, separator in enumerate(separators):
                left, right = values[index], values[index + 1]
                if separator in _QUESTION_RANGE_SEPARATORS:
                    # 「1-100题」这种显然不是要逐题各出一份
                    if not left <= right or right - left > MULTI_QUESTION_RANGE_LIMIT:
                        group = set()
                        break
                    group.update(range(left, right + 1))
                else:
                    group.add(right)
            numbers |= group
        return sorted(numbers) if len(numbers) >= 2 else []

    @staticmethod
    def _question_token_to_int(token: str) -> Optional[int]:
        if token.isdigit():
            return int(token)
        digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if "十" in token:
            head, _, tail = token.partition("十")
            if (head and head not in digits) or (tail and tail not in digits):
                return None
            return (digits[head] if head else 1) * 10 + (digits[tail] if tail else 0)
        return digits.get(token) if len(token) == 1 else None

    @staticmethod
    def _format_question_numbers(numbers: list[int]) -> str:
        return "第" + "、".join(str(number) for number in numbers) + "题"

    def _format_multi_question_notice(self, answered: list[int], missing: list[int], overflow: list[int]) -> str:
        """缺题或超出回复次数时才发的说明；一切正常返回空串，不占回复次数。"""
        problems: list[str] = []
        if missing:
            problems.append(f"没在图片/引用里找到{self._format_question_numbers(missing)}")
        if overflow:
            problems.append(f"一条消息最多回复 {len(answered) + 1} 次，{self._format_question_numbers(overflow)}请另发一条指令")
        if not problems:
            return ""
        return f"正在分别作答{self._format_question_numbers(answered)}，每题完成后单独发一份 PDF。" + "；".join(problems) + "。"

    def _build_question_focus(self, number: int, answered: list[int], stem: str) -> str:
        others = [other for other in answered if other != number]
        if others:
            lines = [f"本次只解答第{number}题；{self._format_question_numbers(others)}会另外单独解答，这里不要涉及。"]
        else:
            lines = [f"本次只解答第{number}题，不要解答其他题目。"]
        if stem:
            lines.append(f"第{number}题题干：\n{stem}")
        else:
            lines.append(f"请在下面的附件内容里找到第{number}题的完整题干再作答。")
        lines.append("附件里的其他题目只用作共用条件的参考。")
        return "\n".join(lines)

    def _reply_slots(self, event: Any) -> int:
        """一条指令最多能被动回复几次。QQ 官方机器人：群聊 5 次，单聊 4 次。"""
        platform = ""
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                platform = str(getter() or "")
            except Exception:
                platform = ""
        if not platform.startswith("qq_official"):
            return getattr(self, "reply_limit_other", DEFAULT_REPLY_SLOTS)
        is_private = getattr(event, "is_private_chat", None)
        try:
            private = bool(is_private()) if callable(is_private) else True
        except Exception:
            private = True
        # 判断不了会话类型时按更严格的单聊次数算
        if private:
            return getattr(self, "reply_limit_private", QQ_OFFICIAL_C2C_REPLY_SLOTS)
        return getattr(self, "reply_limit_group", QQ_OFFICIAL_GROUP_REPLY_SLOTS)

    def _split_questions(self, attachment_context: str, numbers: list[int], override_model: Optional[str] = None) -> Optional[dict[int, str]]:
        """让文本模型从 OCR/引用内容里找出指定题号的完整题干。

        返回 {题号: 题干}，找不到的题不在其中；拆题调用本身失败返回 None。
        用 [Q n] 标记而不是 JSON：题干满是 LaTeX 反斜杠，JSON 转义会随机解析失败。
        """
        if not (attachment_context or "").strip() or not self.text_api_url or not self.text_api_key:
            return None
        messages = [
            {"role": "system", "content": QUESTION_SPLIT_PROMPT},
            {"role": "user", "content": f"要找的题号：{'、'.join(str(number) for number in numbers)}\n\n{attachment_context[:20000]}"},
        ]
        for model_name in self._build_model_queue(override_model=override_model)[:2]:
            try:
                answer = self._call_text_model(model_name, messages)
            except Exception as exc:
                logger.warning("拆题调用失败 model=%s err=%s", model_name, exc)
                continue
            stems = self._parse_split_questions(answer, numbers)
            if stems is not None:
                return stems
        return None

    @staticmethod
    def _parse_split_questions(answer: str, numbers: list[int]) -> Optional[dict[int, str]]:
        text = re.sub(r"^[ \t]*`{3,}[A-Za-z]*[ \t]*$", "", answer or "", flags=re.MULTILINE)
        has_missing_line = QUESTION_SPLIT_MISSING_LINE.search(text) is not None
        body = QUESTION_SPLIT_MISSING_LINE.sub("", text)
        markers = list(QUESTION_SPLIT_MARKER.finditer(body))
        if not markers and not has_missing_line:
            return None
        wanted = set(numbers)
        stems: dict[int, str] = {}
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
            number = int(marker.group(1))
            stem = body[marker.end():end].strip()
            if number in wanted and stem and number not in stems:
                stems[number] = stem[:4000]
        return stems

    def _is_source_export_command(self, text: str) -> bool:
        command = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        return command in {"/ai_source", "ai_source", "/源文件", "源文件"}

    def _copy_to_export_dir(self, source_path: Path, base_name: Optional[str] = None) -> Optional[Path]:
        try:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            if base_name:
                name = self._safe_filename_component(base_name, default="report")
                export_path = self.export_dir / f"{name}{source_path.suffix}"
            else:
                export_path = self.export_dir / f"report_{time.time_ns()}{source_path.suffix}"
            shutil.copyfile(source_path, export_path)
            return export_path
        except Exception:
            return None

    def _extract_qq_number(self, event: Any) -> str:
        """从事件中提取发送者标识，作为文件命名前缀。

        优先使用纯数字 QQ 号；官方机器人等平台只有 openid 哈希，
        此时改用发送者昵称，避免文件名出现无意义的长哈希。
        """
        if event is None:
            return ""
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None) if message_obj is not None else None
        user_id = ""
        for source in (sender, event):
            if source is None:
                continue
            for attr in ("user_id", "qq", "sender_id"):
                value = getattr(source, attr, None)
                if value:
                    user_id = str(value)
                    break
            if user_id:
                break
        if user_id.isdigit():
            return user_id
        nickname = ""
        getter = getattr(event, "get_sender_name", None)
        if callable(getter):
            try:
                nickname = (getter() or "").strip()
            except Exception:
                nickname = ""
        if not nickname and sender is not None:
            nickname = str(getattr(sender, "nickname", "") or "").strip()
        if nickname:
            return nickname
        return user_id[:8] if user_id else ""

    def _extract_sender_subject(self, event: Any) -> str:
        """提取发送者原始渠道标识（QQ 号或官方机器人 openid），供题库身份匹配。

        与 _extract_qq_number 不同：这里不做昵称回退——身份匹配必须用
        稳定原始 id（openid/QQ 号），昵称会变且无法与绑定记录对应。
        """
        if event is None:
            return ""
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None) if message_obj is not None else None
        for source in (sender, event):
            if source is None:
                continue
            for attr in ("user_id", "qq", "sender_id"):
                value = getattr(source, attr, None)
                if value:
                    return str(value)
        return ""

    def _extract_message_id(self, event: Any) -> str:
        """从事件中提取消息 id（duck-typing 多属性探测，仿 _extract_qq_number）。

        AstrBot 各适配器的属性名不统一，逐一探测；取不到返回空串，
        由 _build_external_id 回落时间戳+哈希方案。
        """
        if event is None:
            return ""
        for source in (event, getattr(event, "message_obj", None)):
            if source is None:
                continue
            for attr in ("message_id", "msg_id", "id"):
                value = getattr(source, attr, None)
                if value:
                    return str(value)
        return ""

    def _build_external_id(self, event: Any, question_text: str) -> str:
        """构建回写幂等键：优先 qq:<消息id>（重发天然去重），
        取不到时回落 ts:<秒级时间戳>:<题干 sha256 前 16 位>。"""
        message_id = self._extract_message_id(event)
        if message_id:
            return f"qq:{message_id}"
        digest = hashlib.sha256((question_text or "").encode("utf-8")).hexdigest()[:16]
        return f"ts:{time.strftime('%Y%m%d%H%M%S')}:{digest}"

    def _build_archive_draft(self, structured: dict[str, Any], question_text: str, image_urls: list[str]) -> dict[str, Any]:
        """组装归档 draft（API 白名单字段）：stem 缺失回落原始提问，
        answer 缺失回落 [SUMMARY]，其余字段直接取解析结果。"""
        archive = structured.get("archive") if isinstance(structured.get("archive"), dict) else self._empty_archive_dict()
        summary = str(structured.get("summary", "") or "")
        analysis_text = str(structured.get("body", "") or "")
        answer_text = str(archive.get("answer", "") or "") or summary or "（模型未给出答案）"
        return {
            "stem": str(archive.get("stem", "") or "") or (question_text or "").strip(),
            "answer": answer_text,
            "analysis": analysis_text,
            "type": str(archive.get("type", "") or "answer"),
            "difficulty": int(archive.get("difficulty", 3) or 3),
            "images": list(image_urls or []),
            "tags": list(archive.get("tags") or []),
        }

    def _build_question_image_urls(self, parsed_message: ParsedMessage) -> list[str]:
        """题目图片公网 URL：仅取已本地化（落在受管目录内）的图片，
        经文件服务 asset 路由对外暴露；远程直链与不可达文件一律跳过。"""
        urls: list[str] = []
        for image in parsed_message.images:
            local_path = (image.path or "").replace("file://", "")
            if not local_path:
                continue
            try:
                if not self._is_managed_path(Path(local_path)):
                    continue
            except Exception:
                continue
            urls.append(self._public_file_url(Path(local_path)))
        return urls

    def _build_writeback_payload(
        self,
        event: Any,
        question_text: str,
        structured: dict[str, Any],
        parsed_message: ParsedMessage,
        session_id: str,
        base_name: str,
        used_model: str,
        response: CommandResponse,
    ) -> dict[str, Any]:
        """组装回写 payload：入队时刻冻结全部字段，worker 消费时不再依赖外部状态。"""
        image_urls = self._build_question_image_urls(parsed_message)
        summary = str(structured.get("summary", "") or "")
        archive = structured.get("archive") if isinstance(structured.get("archive"), dict) else self._empty_archive_dict()
        answer_text = str(archive.get("answer", "") or "") or summary or "（模型未给出答案）"
        analysis_text = str(structured.get("body", "") or "")
        pdf_url = ""
        if response.file_payload and response.file_payload.get("url"):
            pdf_url = str(response.file_payload["url"])
        return {
            "external_id": self._build_external_id(event, question_text),
            "question_text": question_text,
            "question_images": image_urls,
            # 回写身份匹配用原始 sender id（QQ 号/官方机器人 openid），
            # 与题库 user_identities 绑定记录的 # 后缀对应；文件命名另用昵称。
            "qq": self._extract_sender_subject(event),
            "meta": {
                "model": used_model,
                "base_name": base_name,
                "pdf_url": pdf_url,
                "session_id": session_id,
            },
            "answer_text": answer_text,
            "analysis_text": analysis_text,
            "draft": self._build_archive_draft(structured, question_text, image_urls),
        }

    def _enqueue_qa_writeback(self, payload: dict[str, Any]) -> None:
        """回写入队钩子：内部全兜底，任何异常都不允许上抛到调用方。"""
        try:
            scheduler = getattr(self, "writeback_scheduler", None)
            # 入队成功 = 这些题图 URL 即将写进题库，定期清理必须放过它们
            if scheduler is not None and scheduler.enqueue(payload):
                self._remember_protected_assets(list(payload.get("question_images") or []))
        except Exception as exc:
            logger.warning("%s 入队钩子异常（已忽略） err=%s", WRITEBACK_LOG_PREFIX, exc)

    def _extract_question_keyword(self, question_text: str) -> str:
        """从题目文本中提取简洁关键词，作为文件命名的可识别标识。"""
        # 先去掉 LaTeX 命令名（\geqslant、\frac…），否则多题题干会让文件名变成 xgeqslant0 这样
        normalized = re.sub(r"\\[A-Za-z]+", "", question_text or "")
        normalized = re.sub(r"\s+", "", normalized)
        normalized = re.sub(r"^(ai|ask|解答|解析|/)+", "", normalized, flags=re.IGNORECASE)
        cleaned = re.sub(r"[，。；：,.!?！？、()（）\[\]【】{}\"'`~!@#$%^&*+=|\\/<>]", "", normalized)
        return cleaned[:16]

    def _keyword_filename_component(self, value: str, default: str = "untitled") -> str:
        """文件名安全化：保留中文、字母、数字、下划线与连字符。"""
        normalized = re.sub(r"[^\w-]+", "_", str(value)).strip("_")
        return normalized[:32] or default

    def _build_base_name(self, qq_number: str, question_text: str) -> str:
        """构建统一命名基名：QQ号_题目关键词_时间戳。源码/日志/PDF 共用。"""
        qq_part = self._keyword_filename_component(qq_number, default="unknown")
        keyword_part = self._keyword_filename_component(self._extract_question_keyword(question_text), default="untitled")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        return f"{qq_part}_{keyword_part}_{timestamp}"

    def _persist_tex_source(self, base_name: str, tex_source: str) -> Path:
        """把源码文件按统一命名写入 sources/ 分类目录。"""
        self.source_dir.mkdir(parents=True, exist_ok=True)
        name = self._safe_filename_component(base_name, default="source")
        source_path = self.source_dir / f"{name}.tex"
        source_path.write_text(tex_source, encoding="utf-8")
        return source_path

    def _safe_filename_component(self, value: str, default: str = "file") -> str:
        normalized = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", str(value)).strip("._-")
        return normalized[:64] or default

    def _public_file_url(self, exported_path: Path) -> str:
        filename = exported_path.name
        configured_host = (getattr(self, "public_file_host", "") or "").strip()
        if configured_host:
            host = configured_host
        else:
            hostname = socket.gethostname()
            host = hostname if hostname and not hostname.startswith("localhost") else "127.0.0.1"
        # 按文件所在分类目录选择 URL 前缀，与文件服务路由保持一致
        try:
            parent = exported_path.resolve().parent
        except OSError:
            parent = exported_path.parent
        prefix = "pdf"
        for category_prefix, category_dir in (("source", self.source_dir), ("log", self.log_dir), ("asset", self.asset_dir)):
            if category_dir is None:
                continue
            try:
                if parent == Path(category_dir).resolve():
                    prefix = category_prefix
                    break
            except OSError:
                continue
        return f"http://{host}:{self.file_server_port}/{prefix}/{filename}"

    def _start_file_server(self) -> None:
        if self._file_server is not None:
            return

        handler_cls = self._build_file_handler()
        try:
            self._file_server = ThreadingHTTPServer((self.file_server_host, self.file_server_port), handler_cls)
        except OSError:
            self._file_server = None
            return

        self._file_server_thread = threading.Thread(target=self._file_server.serve_forever, daemon=True)
        self._file_server_thread.start()

    def _build_file_handler(self):
        # 按分类目录路由：/pdf/ → pdfs/，/source/ → sources/，/log/ → logs/，/asset/ → assets/
        category_dirs = {
            "pdf": self.pdf_dir,
            "source": self.source_dir,
            "log": self.log_dir,
            "asset": self.asset_dir,
        }

        class QuietHandler(SimpleHTTPRequestHandler):
            def do_GET(self) -> None:
                category = None
                for prefix, directory in category_dirs.items():
                    if self.path.startswith(f"/{prefix}/"):
                        category = (prefix, directory)
                        break
                if category is None:
                    self.send_error(404, "File not found")
                    return
                prefix, directory = category
                filename = urllib.parse.unquote(self.path.removeprefix(f"/{prefix}/").split("?", 1)[0])
                if not filename or "/" in filename or "\\" in filename:
                    self.send_error(404, "File not found")
                    return
                file_path = directory / filename
                if not file_path.is_file():
                    self.send_error(404, "File not found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", self.guess_type(str(file_path)))
                # 文件名可能含中文：HTTP 头仅支持 latin-1，直接塞会抛
                # UnicodeEncodeError 导致空响应。按 RFC 5987 用 filename* 编码，
                # 同时提供 ASCII 回退的 filename 以兼容旧客户端。
                ascii_fallback = filename.encode("ascii", errors="ignore").decode("ascii") or "download"
                encoded = urllib.parse.quote(filename)
                self.send_header(
                    "Content-Disposition",
                    f'inline; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}',
                )
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.end_headers()
                with file_path.open("rb") as file_obj:
                    shutil.copyfileobj(file_obj, self.wfile)

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=str(category_dirs["pdf"]), **kwargs)

            def log_message(self, *_args: Any) -> None:
                return

        return QuietHandler
