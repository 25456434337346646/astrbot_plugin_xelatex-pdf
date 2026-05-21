from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - 运行在 AstrBot 环境时会走真实导入
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


@register("astrbot_plugin_xelatex_pdf", "CodeBuddy", "使用 XeLaTeX 生成 PDF 的 AstrBot 插件", "0.1.0")
class Plugin(Star):
    """AstrBot XeLaTeX PDF 插件骨架。"""

    command_aliases = {"/ai", "/ask", "/解答", "/解析"}

    def __init__(self, context: Context | None = None) -> None:
        super().__init__(context)
        self.name = "astrbot_plugin_xelatex_pdf"
        self.initialized = False
        self.data_dir = Path(tempfile.gettempdir()) / self.name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.asset_dir = self.data_dir / "assets"
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.data_dir / "chat_history.json"
        self.max_history_rounds = 5
        self.chat_history = self._load_history()
        self.tex_environment = TexEnvironment(
            xelatex=shutil.which("xelatex"),
            latexmk=shutil.which("latexmk"),
            pdflatex=shutil.which("pdflatex"),
        )

    async def initialize(self) -> None:
        """插件启动钩子。"""
        self.initialized = True
        return None

    def get_environment_status(self) -> dict[str, object]:
        return {
            "initialized": self.initialized,
            "xelatex": self.tex_environment.xelatex,
            "latexmk": self.tex_environment.latexmk,
            "pdflatex": self.tex_environment.pdflatex,
            "available": self.tex_environment.available,
        }

    @filter.command("ai", alias={"ask", "解答", "解析"})
    async def handle_ai(self, event: AstrMessageEvent):
        response = self.handle_event(event)
        if response.file_payload:
            file_payload = response.file_payload
            if hasattr(event, "chain_result"):
                yield event.chain_result([File(name=file_payload["name"], url=file_payload["url"])])
                return
        if hasattr(event, "plain_result"):
            yield event.plain_result(response.message)
        else:
            yield response.message

    def handle_event(self, event: Any) -> CommandResponse:
        parsed_message = self.parse_message(event)
        command_text = self._extract_command_text(parsed_message)
        if not command_text:
            return CommandResponse(success=False, message="消息中没有可处理的命令")

        parsed_message = self.localize_attachments(parsed_message)
        tex_source = self.build_tex_source(parsed_message)
        response = self.handle_command(command_text, tex_source=tex_source)

        session_id = self.get_session_id(event)
        if response.success and response.compile_result and response.compile_result.success:
            self.append_history(session_id, parsed_message.text or command_text, response.message)

        return response

    def parse_message(self, message: Any) -> ParsedMessage:
        parsed = ParsedMessage()
        components = self._extract_components(message)
        parsed.raw_components.extend(components)

        for component in components:
            component_type = component.__class__.__name__.lower()
            text = self._get_component_text(component)
            if text:
                parsed.text = f"{parsed.text}\n{text}".strip()
                continue

            if "reply" in component_type:
                parsed.reply_text = self._extract_reply_text(component)
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

    def handle_command(self, text: str, tex_source: Optional[str] = None) -> CommandResponse:
        request = self.parse_command(text)
        if request is None:
            return CommandResponse(success=False, message="未识别的命令")

        if request.args[:1] == ["clear"]:
            self.clear_session_history(self.get_session_id(None))
            return CommandResponse(success=True, message="会话已清空")

        if "-m" in request.args:
            model_index = request.args.index("-m")
            model_name = request.args[model_index + 1] if model_index + 1 < len(request.args) else ""
            return CommandResponse(success=True, message=f"已切换模型：{model_name or '未指定'}")

        if tex_source is None:
            return CommandResponse(success=True, message="命令已接收，等待 TeX 内容")

        compile_result = self.compile_tex(tex_source)
        if compile_result.success:
            file_payload = self.build_file_payload(compile_result.pdf_path)
            return CommandResponse(success=True, message="编译成功", compile_result=compile_result, file_payload=file_payload)
        return CommandResponse(success=False, message=self.build_compile_error_message(compile_result), compile_result=compile_result)

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
        tex_path.write_text(tex_source, encoding="utf-8")

        process = subprocess.run(
            [
                self.tex_environment.xelatex,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-output-directory",
                str(workdir_path),
                str(tex_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        pdf_exists = pdf_path.exists()
        return CompileResult(
            success=process.returncode == 0 and pdf_exists,
            workdir_path=workdir_path,
            pdf_path=pdf_path if pdf_exists else None,
            log_path=log_path if log_path.exists() else None,
            stdout=process.stdout,
            stderr=process.stderr,
        )

    def build_file_payload(self, pdf_path: Optional[Path]) -> Optional[dict[str, Any]]:
        if pdf_path is None:
            return None
        return {
            "type": "file",
            "name": pdf_path.name,
            "path": str(pdf_path),
            "url": pdf_path.as_uri(),
        }

    def build_tex_source(self, parsed_message: ParsedMessage) -> str:
        body_parts = [parsed_message.reply_text, parsed_message.text]
        body = "\n".join(part for part in body_parts if part).strip() or "请输入要编译的内容。"
        image_block = self._build_image_block(parsed_message.images)
        pdf_block = self._build_pdf_block(parsed_message.pdfs)
        return (
            "\\documentclass{article}\n"
            "\\usepackage[UTF8]{ctex}\n"
            "\\usepackage{geometry}\n"
            "\\usepackage{graphicx}\n"
            "\\geometry{a4paper,margin=2.5cm}\n"
            "\\begin{document}\n"
            f"{self._escape_tex(body)}\n"
            f"{image_block}"
            f"{pdf_block}"
            "\\end{document}\n"
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
    def _build_image_block(self, images: list[MessageAttachment]) -> str:
        if not images:
            return ""
        lines = ["\\section*{图片附件}"]
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
        lines = ["\\section*{PDF 附件}"]
        for pdf in pdfs:
            reference = pdf.path or pdf.url or pdf.name
            if not reference:
                continue
            lines.append(f"\\noindent 附件：{self._escape_tex(reference)}\\\\")
        return "\n".join(lines) + "\n"

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

        target_suffix = Path(attachment.name or source).suffix or (".pdf" if attachment.kind == "pdf" else ".png")
        target_name = f"{attachment.kind}_{abs(hash(source))}{target_suffix}"
        target_path = self.asset_dir / target_name

        try:
            if source.startswith(("http://", "https://")):
                urllib.request.urlretrieve(source, target_path)
            else:
                local_source = source.replace("file://", "")
                if os.path.exists(local_source):
                    shutil.copyfile(local_source, target_path)
                else:
                    return attachment
        except Exception:
            return attachment

        return MessageAttachment(
            kind=attachment.kind,
            name=attachment.name or target_path.name,
            url=target_path.as_uri(),
            path=str(target_path),
            raw=attachment.raw,
        )

    def _escape_tex(self, text: str) -> str:
        return (
            text.replace("\\", r"\\textbackslash{}").replace("&", r"\\&").replace("%", r"\\%").replace("$", r"\\$")
            .replace("#", r"\\#").replace("_", r"\\_").replace("{", r"\\{").replace("}", r"\\}")
            .replace("~", r"\\textasciitilde{}").replace("^", r"\\textasciicircum{}")
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

    def _extract_components(self, message: Any) -> list[Any]:
        if isinstance(message, str):
            return []
        if isinstance(message, list):
            return message
        for attr in ("message", "chain", "message_chain"):
            value = getattr(message, attr, None)
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

    def _extract_reply_text(self, component: Any) -> str:
        for attr in ("text", "content", "message"):
            value = getattr(component, attr, None)
            if isinstance(value, str):
                return value.strip()
        chain = getattr(component, "chain", None)
        if isinstance(chain, list):
            texts = [self._get_component_text(item) for item in chain]
            attachments = [self._component_to_attachment(item) for item in chain]
            attachment_refs = [
                attachment.url or attachment.path or attachment.name
                for attachment in attachments
                if attachment is not None
            ]
            parts = [text for text in texts if text]
            parts.extend(f"[引用附件] {ref}" for ref in attachment_refs if ref)
            return "\n".join(parts)
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
            if stripped.startswith(tuple(self.command_aliases)):
                return stripped
        if parsed_message.reply_text:
            for line in parsed_message.reply_text.splitlines():
                stripped = line.strip()
                if stripped.startswith(tuple(self.command_aliases)):
                    return stripped
        return ""
