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
    plugin.last_base_name_by_session = {}
    plugin.file_server_port = 8765
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


if __name__ == "__main__":
    unittest.main()
