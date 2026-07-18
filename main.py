from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import base64
import hashlib
import json
import os
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


PLUGIN_VERSION = "0.3.0"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_OCR_IMAGE_BYTES = 15 * 1024 * 1024
MAX_OCR_PAGES = 12
NETWORK_TIMEOUT_SECONDS = 30
MODEL_TIMEOUT_SECONDS = 120
VISION_TIMEOUT_SECONDS = 300
PROCESS_TIMEOUT_SECONDS = 120
STALE_WORKDIR_MAX_AGE_SECONDS = 24 * 60 * 60

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
        def __init__(self, name: str = "", url: str = "") -> None:
            self.name = name
            self.url = url

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
        self.asset_dir = self.data_dir
        self.export_dir = self.data_dir
        self.history_file = self.data_dir / "chat_history.json"
        self.max_history_rounds = 5
        self.chat_history = self._load_history()
        self.last_tex_source_by_session: dict[str, str] = {}
        self.last_pdf_by_session: dict[str, Path] = {}
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
        self.file_server_port = 8765
        self.file_server_host = "0.0.0.0"
        self._file_server: ThreadingHTTPServer | None = None
        self._file_server_thread: threading.Thread | None = None
        self._start_file_server()

    async def initialize(self) -> None:
        """插件启动钩子。"""
        self.initialized = True
        await asyncio.to_thread(self._cleanup_stale_workdirs)
        self._start_file_server()
        return None

    async def terminate(self) -> None:
        """插件卸载时关闭内置文件服务。"""
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
        response = await self.handle_event(event)
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
            fallback_message = response.message
            if file_payload.get("url"):
                fallback_message = f"{response.message}\n下载链接：{file_payload['url']}"
            if hasattr(event, "plain_result") and fallback_message:
                yield event.plain_result(fallback_message)
            if hasattr(event, "chain_result"):
                yield event.chain_result([File(name=file_payload["name"], url=file_payload["url"])])
                return
            return
        if hasattr(event, "plain_result"):
            yield event.plain_result(response.message)
        else:
            yield response.message

    async def handle_event(self, event: Any) -> CommandResponse:
        session_id = self.get_session_id(event)
        parsed_message = await self.parse_message(event)
        command_text = self._extract_command_text(parsed_message)
        if not command_text:
            fallback_text = (parsed_message.text or parsed_message.reply_text or "").strip()
            if fallback_text:
                command_text = f"/ai {fallback_text}"
            else:
                model_switch = self._extract_model_switch(parsed_message.text or parsed_message.reply_text)
                if model_switch:
                    self._set_active_model(model_switch)
                    return CommandResponse(success=True, message=f"已切换模型：{model_switch}")
                return CommandResponse(success=False, message="消息中没有可处理的命令")

        model_switch = self._extract_model_switch(self._extract_user_question(command_text))
        if model_switch:
            self._set_active_model(model_switch)
            return CommandResponse(success=True, message=f"已切换模型：{model_switch}")

        if self._is_source_export_command(command_text):
            return await asyncio.to_thread(
                self.handle_command,
                command_text,
                session_id=session_id,
            )

        request = self.parse_command(command_text)
        if request is not None and (request.args[:1] == ["clear"] or "-m" in request.args):
            return await asyncio.to_thread(
                self.handle_command,
                command_text,
                session_id=session_id,
            )

        parsed_message = await asyncio.to_thread(self.localize_attachments, parsed_message)

        answer_text, used_model = await asyncio.to_thread(
            self.generate_answer,
            command_text,
            parsed_message,
            session_id,
        )
        self._set_active_model(used_model)
        question_text = self._extract_user_question(command_text)
        tex_source = await asyncio.to_thread(
            self.build_tex_source,
            parsed_message,
            body_override=answer_text,
            question_text=question_text,
        )
        self.last_tex_source_by_session[session_id] = tex_source
        response = await asyncio.to_thread(
            self.handle_command,
            command_text,
            tex_source=tex_source,
            session_id=session_id,
        )

        if response.success and response.compile_result and response.compile_result.success:
            self.append_history(session_id, question_text, answer_text)
            if response.file_payload and response.file_payload.get("path"):
                self.last_pdf_by_session[session_id] = Path(response.file_payload["path"])

        return response

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

    def handle_command(self, text: str, tex_source: Optional[str] = None, session_id: str = "default") -> CommandResponse:
        request = self.parse_command(text)
        if request is None:
            return CommandResponse(success=False, message="未识别的命令")

        if request.command in {"/查模型", "查模型"}:
            return CommandResponse(success=True, message=self._format_model_list())

        if request.command in {"/ai_source", "ai_source", "/源文件", "源文件"}:
            source = self.last_tex_source_by_session.get(session_id)
            if not source:
                return CommandResponse(success=False, message="当前会话没有可导出的源文件")
            source_path = self._write_export_file(session_id, source, ".tex")
            return CommandResponse(
                success=True,
                message="已导出源文件",
                file_payload=self.build_file_payload(source_path),
            )

        if request.args[:1] == ["clear"]:
            self.clear_session_history(session_id)
            return CommandResponse(success=True, message="会话已清空")

        if "-m" in request.args:
            model_index = request.args.index("-m")
            model_name = request.args[model_index + 1] if model_index + 1 < len(request.args) else ""
            if model_name:
                self._set_active_model(model_name)
            return CommandResponse(success=True, message=f"已切换模型：{model_name or '未指定'}")

        if tex_source is None:
            return CommandResponse(success=True, message="命令已接收，等待 TeX 内容")

        compile_result = self.compile_tex(tex_source)
        if compile_result.success:
            file_payload = self.build_file_payload(compile_result.pdf_path)
            if file_payload is None or not file_payload.get("path"):
                compile_result.success = False
                compile_result.stderr = "PDF 已编译，但无法复制到导出目录"
                message = self.build_compile_error_message(compile_result)
                self._cleanup_compile_result(compile_result)
                return CommandResponse(
                    success=False,
                    message=message,
                    compile_result=compile_result,
                )
            exported_path = Path(file_payload["path"])
            self._cleanup_compile_result(compile_result, exported_path=exported_path)
            return CommandResponse(
                success=True,
                message="编译成功",
                compile_result=compile_result,
                file_payload=file_payload,
            )
        message = self.build_compile_error_message(compile_result)
        self._cleanup_compile_result(compile_result)
        return CommandResponse(
            success=False,
            message=message,
            compile_result=compile_result,
        )

    def _cleanup_compile_result(self, compile_result: CompileResult, exported_path: Optional[Path] = None) -> None:
        workdir_path = compile_result.workdir_path
        if workdir_path is not None:
            shutil.rmtree(workdir_path, ignore_errors=True)
        compile_result.workdir_path = None
        compile_result.log_path = None
        compile_result.pdf_path = exported_path

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

    def build_file_payload(self, pdf_path: Optional[Path]) -> Optional[dict[str, Any]]:
        if pdf_path is None:
            return None
        try:
            already_exported = pdf_path.resolve().parent == self.export_dir.resolve()
        except OSError:
            already_exported = False
        exported_path = pdf_path if already_exported else self._copy_to_export_dir(pdf_path)
        if exported_path is None:
            return None
        return {
            "type": "file",
            "name": exported_path.name,
            "path": str(exported_path),
            "url": self._public_file_url(exported_path),
        }

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

    def _build_user_render_prompt(self, question: str, parsed_message: ParsedMessage) -> str:
        parts = [f"用户原文：\n{question.strip()}"]
        if parsed_message.reply_text:
            parts.append(f"引用消息：\n{parsed_message.reply_text.strip()}")
        if parsed_message.text and parsed_message.text.strip() and parsed_message.text.strip() != question.strip():
            parts.append(f"补充文本：\n{parsed_message.text.strip()}")
        attachment_context = self._build_attachment_context(parsed_message)
        if attachment_context:
            parts.append(attachment_context)
        parts.append(self._build_render_instruction(question, parsed_message))
        parts.append("输出要求：不要输出完整 LaTeX 文档，不要寒暄，不要说无法识别；直接输出可插入模板正文的内容。")
        return "\n\n".join(parts)

    def generate_answer(self, command_text: str, parsed_message: ParsedMessage, session_id: str) -> tuple[str, str]:
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
                    "结构要求：使用清晰标题、短段落、项目列表和必要公式；标题使用 Markdown 的 #、##、### 等层级即可。"
                    "ElegantBook 环境要求：遇到定义、定理、性质、例题、注、笔记、证明、解答时，优先直接使用对应环境："
                    "\\begin{definition}...\\end{definition}、\\begin{theorem}...\\end{theorem}、\\begin{property}...\\end{property}、"
                    "\\begin{example}...\\end{example}、\\begin{remark}...\\end{remark}、\\begin{note}...\\end{note}、"
                    "\\begin{proof}...\\end{proof}、\\begin{solution}...\\end{solution}。"
                    "也可以用 Markdown 标题写“## 定义”“#### 例 1”“#### 注”等，外部程序会自动转换为 ElegantBook 环境。"
                    "数学要求：行内公式优先使用 \\(...\\) 或 $...$；行间公式使用 \\[...\\] 或 $$...$$；可使用 amsmath/amsfonts/amssymb 常见命令，如 \\mathbb、\\varepsilon、\\delta、\\Rightarrow。"
                    "内容要求：直接回答用户问题，不要寒暄，不要说“下面是”。"
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

        messages.append({"role": "user", "content": self._build_user_render_prompt(question, parsed_message)})

        model_queue = self._build_model_queue()
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
        data = self._post_chat_completion(
            api_url=self.text_api_url,
            api_key=self.text_api_key,
            payload={"model": model_name, "messages": messages},
            timeout=MODEL_TIMEOUT_SECONDS,
        )
        return self._extract_chat_content(data)

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
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
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

    def _build_model_queue(self) -> list[str]:
        models = [self.active_model_name]
        models.extend(self.available_models)
        result: list[str] = []
        for model in models:
            model = (model or "").strip()
            if model and model not in result:
                result.append(model)
        return result or ["default"]

    def _extract_user_question(self, command_text: str) -> str:
        request = self.parse_command(command_text)
        if request is None:
            return command_text.strip()
        args = list(request.args)
        if "-m" in args:
            model_index = args.index("-m")
            del args[model_index:model_index + 2]
        return " ".join(args).strip()

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

    def build_tex_source(self, parsed_message: ParsedMessage, body_override: Optional[str] = None, question_text: str = "") -> str:
        if body_override is None:
            body_parts = [parsed_message.reply_text, parsed_message.text]
            body = "\n".join(part for part in body_parts if part).strip() or "请输入要编译的内容。"
        else:
            body = body_override.strip() or "模型未返回有效内容。"
        image_block = self._build_image_block(parsed_message.images)
        pdf_block = self._build_pdf_block(parsed_message.pdfs)
        answer_model = self._escape_tex_metadata(self.active_model_name or "default")
        answer_time = self._escape_tex_metadata(time.strftime("%Y-%m-%d %H:%M:%S"))
        title_text = self._escape_tex_metadata(self._build_document_title(question_text, body))
        subtitle_text = self._escape_tex_metadata(self._build_document_subtitle(question_text))
        summary_text = self._tex_inline(self._build_document_summary(question_text, body))
        question_card = self._build_question_card(question_text)
        insight_block = self._build_insight_block(question_text, body)
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
            "\\begin{answerabstractbox}\n"
            "\\textbf{内容提要}\\quad " + summary_text + "\n"
            "\\end{answerabstractbox}\n"
            f"{question_card}"
            f"{insight_block}"
            "\\vspace{0.5em}\n"
            f"{self._markdown_to_tex(body)}\n"
            f"{image_block}"
            f"{pdf_block}"
            "\\end{document}\n"
        )

    def _build_document_title(self, question_text: str, body: str) -> str:
        seed = (question_text or body or "智能回答").strip()
        normalized = re.sub(r"\s+", " ", seed)
        if any(keyword in normalized for keyword in ("哈斯图", "偏序集", "整除关系")):
            return "偏序集分析与哈斯图绘制"
        if any(keyword in normalized for keyword in ("证明", "定理", "命题")):
            return "数学证明与结构化解答"
        if any(keyword in normalized for keyword in ("图", "TikZ", "tikzpicture", "Graphviz")):
            return "图形生成与排版说明"
        title = normalized[:28].strip("，。；：,.!?！？ ")
        return title or "智能回答"

    def _build_document_subtitle(self, question_text: str) -> str:
        question = re.sub(r"\s+", " ", (question_text or "").strip())
        if not question:
            return "ElegantBook 智能解答"
        snippet = question[:36].strip("，。；：,.!?！？ ")
        return f"围绕问题：{snippet}"

    def _build_document_summary(self, question_text: str, body: str) -> str:
        has_graphics = any(keyword in (question_text + "\n" + body) for keyword in ("tikzpicture", "哈斯图", "Graphviz", "图"))
        summary_parts = ["本页整理本次问题的核心思路、结果与必要说明"]
        if question_text.strip():
            summary_parts.append("标题根据问题语义自动生成")
        if has_graphics:
            summary_parts.append("包含图形或图论相关内容时会优先保留原生图形渲染")
        summary_parts.append("正文优先使用 ElegantBook 的解答、注记与示例风格")
        return "；".join(summary_parts) + "。"

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

    def _build_insight_block(self, question_text: str, body: str) -> str:
        combined = (question_text + "\n" + body).strip()
        notes: list[str] = []
        if any(keyword in combined for keyword in ("哈斯图", "偏序集", "图论", "tikzpicture", "Graphviz")):
            notes.append("\\begin{note}图形题：优先保留原生 TikZ/图环境，避免转义破坏布局。\\end{note}")
        if any(keyword in combined for keyword in ("证明", "推导", "结论")):
            notes.append("\\begin{note}证明题：建议先写结论，再展开关键推导。\\end{note}")
        return "\n".join(notes) + ("\n" if notes else "")

    def _markdown_to_tex(self, text: str) -> str:
        text = self._normalize_latex_blocks(text)
        lines = text.splitlines()
        output: list[str] = []
        in_itemize = False
        in_enumerate = False
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
                        emit(ml)
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
                emit(ml)
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

    def _tex_inline(self, text: str) -> str:
        math_chunks: list[str] = []
        code_chunks: list[str] = []

        def stash_math(match: re.Match[str]) -> str:
            math_chunks.append(match.group(0))
            return f"@@MATH{len(math_chunks) - 1}@@"

        def stash_code(match: re.Match[str]) -> str:
            code_chunks.append(match.group(1))
            return f"@@CODE{len(code_chunks) - 1}@@"

        masked = re.sub(r"\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$\$[\s\S]*?\$\$|\$[^$\n]+\$", stash_math, text)
        masked = re.sub(r"`([^`]+)`", stash_code, masked)
        masked = self._escape_tex(masked)
        masked = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", masked)
        masked = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\\emph{\1}", masked)
        for index, value in enumerate(code_chunks):
            masked = masked.replace(f"@@CODE{index}@@", f"\\texttt{{{self._escape_tex(value)}}}")
        for index, value in enumerate(math_chunks):
            masked = masked.replace(f"@@MATH{index}@@", value)
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

    def _format_model_list(self) -> str:
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
        return parsed_message

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

    def _extract_model_switch(self, text: str) -> Optional[str]:
        stripped = text.strip()
        patterns = [
            r"^使用\s+(.+?)\s+模型回答$",
            r"^切换到\s+(.+?)\s*模型$",
            r"^用\s+(.+?)\s+模型回答$",
        ]
        for pattern in patterns:
            match = re.match(pattern, stripped)
            if match:
                return match.group(1).strip()
        return None

    def _is_source_export_command(self, text: str) -> bool:
        command = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        return command in {"/ai_source", "ai_source", "/源文件", "源文件"}

    def _copy_to_export_dir(self, source_path: Path) -> Optional[Path]:
        try:
            export_path = self.export_dir / f"report_{time.time_ns()}{source_path.suffix}"
            shutil.copyfile(source_path, export_path)
            return export_path
        except Exception:
            return None

    def _write_export_file(self, session_id: str, content: str, suffix: str) -> Path:
        safe_session_id = self._safe_filename_component(session_id, default="session")
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]
        export_path = self.export_dir / f"source_{safe_session_id}_{content_digest}{suffix}"
        export_path.write_text(content, encoding="utf-8")
        return export_path

    def _safe_filename_component(self, value: str, default: str = "file") -> str:
        normalized = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value)).strip("._-")
        return normalized[:64] or default

    def _public_file_url(self, exported_path: Path) -> str:
        filename = exported_path.name
        hostname = socket.gethostname()
        if hostname and not hostname.startswith("localhost"):
            return f"http://{hostname}:{self.file_server_port}/pdf/{filename}"
        return f"http://127.0.0.1:{self.file_server_port}/pdf/{filename}"

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
        data_dir = self.data_dir

        class QuietHandler(SimpleHTTPRequestHandler):
            def do_GET(self) -> None:
                if not self.path.startswith("/pdf/"):
                    self.send_error(404, "File not found")
                    return
                filename = urllib.parse.unquote(self.path.removeprefix("/pdf/").split("?", 1)[0])
                if not filename or "/" in filename or "\\" in filename:
                    self.send_error(404, "File not found")
                    return
                file_path = data_dir / filename
                if not file_path.is_file():
                    self.send_error(404, "File not found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", self.guess_type(str(file_path)))
                self.send_header("Content-Disposition", f'inline; filename="{filename}"')
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.end_headers()
                with file_path.open("rb") as file_obj:
                    shutil.copyfileobj(file_obj, self.wfile)

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=str(data_dir), **kwargs)

            def log_message(self, *_args: Any) -> None:
                return

        return QuietHandler
