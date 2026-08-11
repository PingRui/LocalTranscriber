import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("localtranscriber_gui_test", str(ROOT / "gui.pyw"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
GUI = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(GUI)


class DesktopWorkflowTests(unittest.TestCase):
    def test_local_ui_keeps_approved_icon_and_spacing_contract(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertTrue((ROOT / "ui" / "vendor" / "lucide.min.js").is_file())
        self.assertTrue((ROOT / "ui" / "vendor" / "LUCIDE-LICENSE").is_file())
        self.assertIn('<script src="vendor/lucide.min.js"></script>', html)
        self.assertIn('data-lucide="circle-plus"', html)
        self.assertIn('data-lucide="trash-2"', script)
        self.assertIn('flex: 0 0 236px', styles)
        self.assertIn('flex: 0 0 210px', styles)
        self.assertIn('flex: 0 0 44px', styles)
        self.assertIn('min-height: 58px', styles)
        self.assertIn('min-width: 250px', styles)
        self.assertNotIn('font-weight: 550', styles)
        self.assertNotIn('font-weight: 650', styles)
        self.assertIn('let frontmatter = lines[0]?.trim() === "---";', script)
        self.assertNotIn('line.trim() === "---" && index < 12', script)
        self.assertIn('response?.pending', script)

    def test_batch_sources_are_written_as_per_file_map(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "one.mp4"
            second = root / "two.wav"
            first.write_bytes(b"1")
            second.write_bytes(b"2")
            with patch.object(GUI, "STATE_DIR", root / "state"), patch.object(GUI, "HISTORY_FILE", root / "state" / "history.json"):
                api = GUI.TranscriberApi((str(first), str(second)))
                settings = {
                    "model": "medium",
                    "language": "auto",
                    "device": "cpu",
                    "output_mode": "source",
                    "output_path": "",
                    "skip_existing": True,
                    "prompt": "",
                    "source_urls": {
                        str(first): "https://www.youtube.com/watch?v=one",
                        str(second): "https://www.bilibili.com/video/two",
                    },
                    "context_mode": "isolated",
                    "llm_repair": False,
                    "deepseek_api_key": "",
                }
                with patch.object(GUI.threading, "Thread") as thread_class:
                    response = api.start_transcription(settings)
                    self.assertTrue(response["ok"])
                    command = thread_class.call_args.kwargs["args"][0]
                    map_path = Path(command[command.index("--source-url-map") + 1])
                    source_map = json.loads(map_path.read_text(encoding="utf-8"))
                    self.assertEqual(source_map[str(first.resolve())], settings["source_urls"][str(first)])
                    self.assertEqual(source_map[str(second.resolve())], settings["source_urls"][str(second)])
                    map_path.unlink(missing_ok=True)
                    api.source_map_file = None

    def test_markdown_viewer_prefers_repaired_copy_and_reads_corrections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "lesson.mp4"
            source.write_bytes(b"x")
            result_dir = root / "转写结果"
            result_dir.mkdir()
            raw = result_dir / "lesson.md"
            accurate = result_dir / "lesson.llm.md"
            corrections = result_dir / "lesson.llm-corrections.json"
            raw.write_text("# 原始稿\n\nCloud。", encoding="utf-8")
            accurate.write_text("# 准确稿\n\nClaude。", encoding="utf-8")
            corrections.write_text(
                json.dumps(
                    {
                        "corrections": [
                            {
                                "start": 12,
                                "original": "Cloud",
                                "corrected": "Claude",
                                "reason": "名称修复",
                                "accepted": True,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch.object(GUI, "STATE_DIR", root / "state"), patch.object(GUI, "HISTORY_FILE", root / "state" / "history.json"):
                api = GUI.TranscriberApi((str(source),))
                item = api.files[0]
                item["status"] = "已完成"
                api.sync_history(
                    item,
                    output_dir=result_dir,
                    outputs=[str(raw), str(accurate), str(corrections)],
                )
                settings = {"model": "medium", "output_mode": "source"}
                self.assertIn("Claude", api.read_result(str(source), settings, "accurate")["content"])
                self.assertIn("Cloud", api.read_result(str(source), settings, "raw")["content"])
                self.assertIn("名称修复", api.read_result(str(source), settings, "corrections")["content"])

    def test_markdown_viewer_treats_missing_processing_output_as_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "lesson.mp4"
            source.write_bytes(b"x")
            with patch.object(GUI, "STATE_DIR", root / "state"), patch.object(GUI, "HISTORY_FILE", root / "state" / "history.json"):
                api = GUI.TranscriberApi((str(source),))
                api.files[0]["status"] = "转写中"
                response = api.read_result(
                    str(source),
                    {"model": "medium", "output_mode": "source"},
                    "accurate",
                )
                self.assertFalse(response["ok"])
                self.assertTrue(response["pending"])
                self.assertNotIn("error", response)

    def test_markdown_viewer_treats_optional_corrections_as_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "lesson.mp4"
            source.write_bytes(b"x")
            with patch.object(GUI, "STATE_DIR", root / "state"), patch.object(GUI, "HISTORY_FILE", root / "state" / "history.json"):
                api = GUI.TranscriberApi((str(source),))
                api.files[0]["status"] = "已完成"
                response = api.read_result(
                    str(source),
                    {"model": "medium", "output_mode": "source"},
                    "corrections",
                )
                self.assertFalse(response["ok"])
                self.assertTrue(response["unavailable"])
                self.assertNotIn("error", response)


if __name__ == "__main__":
    unittest.main()
