import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import run_knowledge_batch as BATCH_RUNNER


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("localtranscriber_gui_test", str(ROOT / "gui.pyw"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
GUI = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(GUI)


class DesktopWorkflowTests(unittest.TestCase):
    def api_context(self, root: Path):
        stack = ExitStack()
        stack.enter_context(patch.object(GUI, "APP_SETTINGS_FILE", root / "state" / "knowledge-app.json"))
        stack.enter_context(patch.object(GUI, "MODEL_PROVIDER_SETTINGS_FILE", root / "state" / "model-providers.json"))
        stack.enter_context(patch.object(GUI, "LOG_FILE", root / "state" / "last-run.log"))
        return stack

    def test_ui_is_the_two_function_product_and_search_is_a_chat(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="knowledgeGenerationView"', html)
        self.assertIn('id="knowledgeChatView"', html)
        self.assertIn('id="chooseVideoFolderButton"', html)
        self.assertIn('id="appendVideosButton"', html)
        self.assertIn('id="appendFolderButton"', html)
        self.assertIn('id="taskStages"', html)
        self.assertIn('id="knowledgeComposer"', html)
        self.assertIn('id="evidencePlayer"', html)
        self.assertIn('id="aiBaseUrl"', html)
        self.assertIn('id="testAiButton"', html)
        self.assertIn('data-lucide="message-square-text"', html)
        self.assertIn('<script src="vendor/lucide.min.js"></script>', html)
        self.assertIn('<option value="medium">Medium</option>', html)
        self.assertIn('<option value="large-v3-turbo">Large-v3 Turbo</option>', html)
        self.assertIn('callApi("ask_knowledge"', script)
        self.assertIn('callApi("get_video_source"', script)
        self.assertIn('callApi("relink_missing_video"', script)
        self.assertIn('callApi("remove_recent_space"', script)
        self.assertIn('$("appendVideosButton").classList.toggle("hidden", !paused)', script)
        self.assertIn("waitForDesktopApi", script)
        self.assertIn("optionSignature", script)
        self.assertIn("window.confirm", script)
        self.assertIn("grid-template-columns: 244px", styles)
        self.assertNotIn('id="openObsidianButton"', html)
        self.assertNotIn('id="chatOpenObsidianButton"', html)
        self.assertNotIn('callApi("open_obsidian"', script)
        self.assertNotIn("trustedSearchForm", html)
        self.assertNotIn("workspaceView", html)

    def test_recent_space_can_be_removed_without_deleting_its_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first-space"
            second = root / "second-space"
            first.mkdir()
            second.mkdir()
            marker = first / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.api_context(root):
                api = GUI.KnowledgeApi()
                api.recent_spaces = [str(first), str(second)]
                api.save_app_settings()
                response = api.remove_recent_space(str(first))
                saved = json.loads(GUI.APP_SETTINGS_FILE.read_text(encoding="utf-8"))

            self.assertTrue(response["ok"])
            self.assertEqual(api.recent_spaces, [str(second)])
            self.assertEqual(saved["recent_spaces"], [str(second)])
            self.assertTrue(marker.is_file())

    def test_chat_passes_previous_messages_without_persisting_them_to_another_space(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            space = root / "knowledge"
            with self.api_context(root), patch.object(GUI.threading, "Thread") as thread_class:
                api = GUI.KnowledgeApi()
                api.open_space(str(space))
                api.api_base_url = "https://example.test/v1"
                api.api_model = "model"
                api.api_key = "key"
                api.api_verified_at = "verified"
                api.messages = [
                    {"role": "user", "content": "深蹲时膝盖方向有什么要求？"},
                    {"role": "assistant", "content": "膝盖应与脚尖方向一致。", "citations": []},
                ]

                response = api.ask_knowledge("它为什么重要？")

                self.assertTrue(response["ok"])
                thread_args = thread_class.call_args.kwargs["args"]
                self.assertEqual(thread_args[0], str(space.resolve()))
                self.assertEqual(thread_args[1], "它为什么重要？")
                self.assertEqual(len(thread_args[2]), 2)
                self.assertEqual(len(api.messages), 3)
                self.assertFalse(api.open_space(str(root / "other"))["ok"])
                self.assertFalse(api.clear_chat()["ok"])

    def test_snapshot_never_exposes_api_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = root / "state" / "model-providers.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({
                "corrector": {
                    "provider": "openai",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                    "api_key": "secret-value",
                    "verified_at": "2026-01-01T00:00:00+08:00",
                    "context_window": 64000,
                }
            }), encoding="utf-8")
            with self.api_context(root):
                snapshot = GUI.KnowledgeApi().snapshot()
            self.assertTrue(snapshot["ai"]["has_key"])
            self.assertNotIn("api_key", snapshot["ai"])
            self.assertNotIn("secret-value", json.dumps(snapshot, ensure_ascii=False))

    def test_startup_remembers_but_does_not_load_the_previous_space(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            space = root / "knowledge"
            space.mkdir()
            settings = root / "state" / "knowledge-app.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps({"space_root": str(space), "recent_spaces": [str(space)]}),
                encoding="utf-8",
            )
            with (
                self.api_context(root),
                patch.object(GUI, "initialize_space") as initialize,
                patch.object(GUI, "load_index") as load_index,
                patch.object(GUI, "load_latest_resumable_task") as load_task,
            ):
                api = GUI.KnowledgeApi()
                snapshot = api.snapshot()

            self.assertEqual(api.space_root, "")
            self.assertEqual(api._preferred_space_root, str(space))
            self.assertFalse(snapshot["space"]["ready"])
            initialize.assert_not_called()
            load_index.assert_not_called()
            load_task.assert_not_called()

    def test_opening_space_loads_the_index_once_and_snapshots_use_the_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            space = root / "knowledge"
            with self.api_context(root), patch.object(GUI, "load_index", return_value=[{}, {}]) as load_index:
                api = GUI.KnowledgeApi()
                response = api.open_space(str(space))
                first = api.snapshot()
                second = api.snapshot()

            self.assertTrue(response["ok"])
            self.assertEqual(first["space"]["knowledge_count"], 2)
            self.assertEqual(second["space"]["knowledge_count"], 2)
            load_index.assert_called_once_with(space.resolve())

    def test_selected_space_integrity_check_runs_in_the_background(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            space = root / "knowledge"
            with self.api_context(root), patch.object(GUI.threading, "Thread") as thread_class:
                api = GUI.KnowledgeApi()
                api.attach_window(object())
                response = api.open_space(str(space))

            self.assertTrue(response["ok"])
            self.assertEqual(response["snapshot"]["space"]["integrity"], "checking")
            self.assertEqual(thread_class.call_args.kwargs["target"], api._check_space_integrity)
            thread_class.return_value.start.assert_called_once()

    @unittest.skipUnless(GUI.sys.platform == "win32", "Windows named mutex")
    def test_named_mutex_rejects_a_second_desktop_instance(self):
        name = f"Local\\LocalTranscriber.Test.{uuid.uuid4()}"
        try:
            self.assertTrue(GUI.acquire_single_instance(name))
            self.assertFalse(GUI.acquire_single_instance(name))
        finally:
            GUI.release_single_instance()

    def test_batch_runner_uses_the_same_program_lock_and_releases_it(self):
        class FakeGui:
            acquired = True
            released = False

            @classmethod
            def acquire_single_instance(cls):
                return cls.acquired

            @classmethod
            def release_single_instance(cls):
                cls.released = True

        source = Path("D:/unused-in-unit-test")
        with (
            patch.object(BATCH_RUNNER, "load_gui_module", return_value=FakeGui),
            patch.object(BATCH_RUNNER, "_run_with_gui", return_value=7) as run_locked,
        ):
            self.assertEqual(BATCH_RUNNER.run(source), 7)
        run_locked.assert_called_once_with(FakeGui, source)
        self.assertTrue(FakeGui.released)

        FakeGui.acquired = False
        FakeGui.released = False
        with patch.object(BATCH_RUNNER, "load_gui_module", return_value=FakeGui):
            with self.assertRaisesRegex(RuntimeError, "已在运行"):
                BATCH_RUNNER.run(source)
        self.assertFalse(FakeGui.released)

    def test_folder_picker_does_not_pass_none_as_a_path(self):
        class FakeWindow:
            def __init__(self):
                self.calls = []

            def create_file_dialog(self, kind, **kwargs):
                self.calls.append((kind, kwargs))
                return None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.api_context(root):
                api = GUI.KnowledgeApi()
                window = FakeWindow()
                api.attach_window(window)
                response = api.choose_space()
            self.assertTrue(response["ok"])
            self.assertEqual(window.calls[0][1], {})

    def test_initial_folder_is_scanned_recursively_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / "input" / "nested"
            nested.mkdir(parents=True)
            video = nested / "lesson.mp4"
            video.write_bytes(b"video")
            (nested / "notes.txt").write_text("ignore", encoding="utf-8")
            with self.api_context(root):
                api = GUI.KnowledgeApi((str(root / "input"), str(video)))
            self.assertEqual(len(api.queue), 1)
            self.assertEqual(api.queue[0]["source"], str(video.resolve()))

    def test_running_task_requires_pause_before_adding_video(self):
        class FakeWindow:
            def __init__(self):
                self.calls = 0

            def create_file_dialog(self, *_args, **_kwargs):
                self.calls += 1
                return None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.api_context(root):
                api = GUI.KnowledgeApi()
                window = FakeWindow()
                api.attach_window(window)
                api.running = True
                api.paused = False
                response = api.choose_videos()

            self.assertFalse(response["ok"])
            self.assertIn("暂停", response["error"])
            self.assertEqual(window.calls, 0)

    def test_paused_task_appends_video_and_recovers_it_from_checkpoint(self):
        class FakeWindow:
            def __init__(self, selected):
                self.selected = selected

            def create_file_dialog(self, *_args, **_kwargs):
                return self.selected

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            space = root / "knowledge"
            first = root / "lesson-1.mp4"
            second = root / "lesson-2.mp4"
            first.write_bytes(b"video-1")
            second.write_bytes(b"video-2")
            with self.api_context(root):
                api = GUI.KnowledgeApi()
                api.open_space(str(space))
                task = GUI.create_task(space, [first])
                task.update(status="running", current_index=0, stages=[])
                task["videos"][0]["status"] = "processing"
                api.task = task
                api.running = True
                api.paused = True
                api.attach_window(FakeWindow((str(second),)))

                response = api.choose_videos()
                task_file = GUI.space_paths(space)["work"] / "tasks" / task["task_id"] / "task.json"
                saved = json.loads(task_file.read_text(encoding="utf-8"))
                restored = GUI.load_latest_resumable_task(space)
                checkpoint = json.loads(json.dumps(restored))
                api.task = restored
                api.running = False
                api.paused = False
                with patch.object(GUI.threading, "Thread") as thread_class:
                    continued = api.continue_knowledge_task()

            self.assertTrue(response["ok"])
            self.assertEqual(len(api.task["videos"]), 2)
            self.assertEqual(api.task["videos"][1]["status"], "waiting")
            self.assertEqual(saved["videos"][1]["source"], str(second.resolve()))
            self.assertEqual(checkpoint["status"], "interrupted")
            self.assertEqual(checkpoint["videos"][0]["status"], "interrupted")
            self.assertEqual(checkpoint["videos"][1]["status"], "waiting")
            self.assertTrue(continued["ok"])
            self.assertEqual(api.task["task_id"], task["task_id"])
            self.assertEqual(api.task["videos"][1]["status"], "waiting")
            thread_class.return_value.start.assert_called_once()

    def test_paused_task_folder_append_skips_existing_video(self):
        class FakeWindow:
            def __init__(self, selected):
                self.selected = selected

            def create_file_dialog(self, *_args, **_kwargs):
                return self.selected

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "input"
            folder.mkdir()
            first = folder / "lesson-1.mp4"
            second = folder / "lesson-2.mp4"
            first.write_bytes(b"video-1")
            second.write_bytes(b"video-2")
            space = root / "knowledge"
            with self.api_context(root):
                api = GUI.KnowledgeApi()
                api.open_space(str(space))
                api.task = GUI.create_task(space, [first])
                api.task.update(status="running", stages=[])
                api.running = True
                api.paused = True
                api.attach_window(FakeWindow((str(folder),)))

                response = api.choose_video_folder()

            self.assertTrue(response["ok"])
            self.assertEqual(len(api.task["videos"]), 2)
            self.assertEqual(api.task["videos"][1]["source"], str(second.resolve()))
            self.assertIn("跳过 1 个重复项", response["message"])

    def test_real_connection_test_saves_verified_provider(self):
        class FakeClient:
            def __init__(self, base_url, model, api_key, **_kwargs):
                self.base_url = base_url
                self.model = model
                self.api_key = api_key

            def test_chat_completion(self):
                return "ok"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.api_context(root), patch.object(GUI, "OpenAICompatibleClient", FakeClient):
                api = GUI.KnowledgeApi()
                response = api.test_and_save_ai("https://relay.example/v1", "model-a", "secret", 32000)
                snapshot = response["snapshot"]
            self.assertTrue(response["ok"])
            self.assertTrue(snapshot["ai"]["verified"])
            saved = json.loads((root / "state" / "model-providers.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["corrector"]["api_key"], "secret")
            self.assertEqual(saved["corrector"]["context_window"], 32000)

    def test_task_requires_space_queue_and_verified_ai(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.api_context(root):
                api = GUI.KnowledgeApi()
                self.assertFalse(api.start_knowledge_task()["ok"])
                space = root / "knowledge"
                api.open_space(str(space))
                self.assertFalse(api.start_knowledge_task()["ok"])
                source = root / "lesson.mp4"
                source.write_bytes(b"video")
                api._add_discovered(GUI.discover_videos([source]))
                self.assertFalse(api.start_knowledge_task()["ok"])

    def test_hotword_confirmation_is_saved_and_continues_the_same_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            space = root / "knowledge"
            source = root / "lesson.mp4"
            source.write_bytes(b"video")
            with self.api_context(root), patch.object(GUI.threading, "Thread") as thread_class:
                api = GUI.KnowledgeApi()
                api.open_space(str(space))
                task = GUI.create_task(space, [source])
                hotword_file = space / ".work" / "tasks" / task["task_id"] / "video-0001" / "hotwords.json"
                GUI.write_json(
                    hotword_file,
                    {
                        "status": "needs_confirmation",
                        "category": "AI",
                        "hotwords": [
                            {"term": "RAG", "aliases": ["阿格"], "evidence": "样本出现"},
                            {"term": "Agent", "aliases": [], "evidence": "样本出现"},
                        ],
                        "assessment": {"category": "AI", "selected": []},
                    },
                )
                task.update(
                    status="needs_attention",
                    current_stage="confirm",
                    current_index=0,
                    hotword_profile={"video_index": 0},
                )
                task["videos"][0].update(status="needs_confirmation", hotword_file=str(hotword_file))
                api.task = task
                response = api.confirm_hotwords("RAG，Agent, RAG", "AI 工程")
            self.assertTrue(response["ok"])
            self.assertEqual(api.task["task_id"], task["task_id"])
            self.assertTrue(api.running)
            saved = json.loads(hotword_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["confirmation"], "manual")
            self.assertEqual(saved["category"], "AI 工程")
            self.assertEqual(saved["assessment"]["category"], "AI 工程")
            self.assertEqual([item["term"] for item in saved["hotwords"]], ["RAG", "Agent"])
            thread_class.return_value.start.assert_called_once()

    def test_opening_space_recovers_a_running_task_as_interrupted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            space = root / "knowledge"
            source = root / "lesson.mp4"
            source.write_bytes(b"video")
            GUI.initialize_space(space)
            task = GUI.create_task(space, [source])
            task.update(status="running", status_text="processing", stages=[{"id": "copy", "status": "running"}])
            task["videos"][0]["status"] = "processing"
            GUI.write_task(space, task)

            with self.api_context(root):
                api = GUI.KnowledgeApi()
                response = api.open_space(str(space))

            self.assertTrue(response["ok"])
            self.assertEqual(api.task["task_id"], task["task_id"])
            self.assertEqual(api.task["status"], "interrupted")
            self.assertEqual(api.task["videos"][0]["status"], "interrupted")

    def test_cleanup_only_accepts_finished_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            space = root / "knowledge"
            with self.api_context(root):
                api = GUI.KnowledgeApi()
                api.open_space(str(space))
                api.task = {"task_id": "task-20260101-000000-aabbcc", "status": "running"}
                self.assertFalse(api.cleanup_task()["ok"])


if __name__ == "__main__":
    unittest.main()
