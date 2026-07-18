from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from main import CompileResult, MessageAttachment, ParsedMessage, PLUGIN_VERSION, Plugin, SessionMemory


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
    plugin.file_server_port = 8765
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
    def test_source_export_stays_inside_export_directory(self) -> None:
        plugin = bare_plugin()
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin.export_dir = Path(temp_dir)

            exported = plugin._write_export_file("../../outside/session", "content", ".tex")

            self.assertEqual(exported.parent, Path(temp_dir))
            self.assertNotIn("..", exported.name)
            self.assertEqual(exported.read_text(encoding="utf-8"), "content")

    def test_compile_source_copies_only_approved_images(self) -> None:
        plugin = bare_plugin()
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as work_dir:
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
        plugin = bare_plugin()
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as work_dir:
            plugin.asset_dir = Path(data_dir)
            plugin.data_dir = Path(data_dir)
            plugin.export_dir = Path(data_dir)

            prepared = plugin._prepare_compile_source(
                r"\includegraphics{/etc/passwd}",
                Path(work_dir),
            )

            self.assertEqual(prepared, r"\fbox{\textit{图片附件不可用}}")

    def test_successful_command_cleans_compile_directory(self) -> None:
        plugin = bare_plugin()
        with tempfile.TemporaryDirectory() as export_dir, tempfile.TemporaryDirectory() as work_dir:
            plugin.export_dir = Path(export_dir)
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
                response = plugin.handle_command("/ai test", tex_source="source")

            self.assertTrue(response.success)
            self.assertFalse(work_path.exists())
            self.assertIsNone(compile_result.workdir_path)
            self.assertIsNotNone(compile_result.pdf_path)
            self.assertEqual(compile_result.pdf_path.parent, Path(export_dir))
            self.assertTrue(compile_result.pdf_path.exists())


if __name__ == "__main__":
    unittest.main()
