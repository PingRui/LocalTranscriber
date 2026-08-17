from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_pipeline import run_resumable_task
from knowledge_space import create_task, initialize_space, load_index, write_index, write_task


class FakeApi:
    def __init__(self, space: Path, task: dict):
        self.space_root = str(space)
        self.task = task
        self.app_config = {"default_model": "medium", "default_device": "cpu"}
        self.language = "zh"
        self.api_base_url = "https://example.test/v1"
        self.api_model = "test-model"
        self.api_key = "secret"
        self.context_window = 128000
        self.running = True
        self.paused = False
        self.cancel_requested = False
        self.activities = []
        self.subprocess_phases = []
        self.analysis_profiles = {}

    def _new_stage_state(self):
        return [
            {"id": key, "label": key, "status": "waiting", "progress": 0.0, "message": ""}
            for key in ("copy", "analyze", "confirm", "transcribe", "verify", "publish", "write")
        ]

    def _set_stage(self, stage_id, status, progress=0.0, message=""):
        for stage in self.task["stages"]:
            if stage["id"] == stage_id:
                stage.update(status=status, progress=progress, message=message)
        self.task["current_stage"] = stage_id
        self.task["status_text"] = message
        self._persist_task()

    def _persist_task(self):
        write_task(Path(self.space_root), self.task)

    def activity(self, text, level="info"):
        self.activities.append((level, text))

    def notify(self, *_args, **_kwargs):
        return None

    def _client(self):
        return object()

    def _run_subprocess(self, command, _env, phase):
        self.subprocess_phases.append(phase)
        if phase == "analyze":
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            source_name = Path(command[3]).name
            profile = self.analysis_profiles.get(source_name) or {
                "status": "ready",
                "category": "AI",
                "confidence": 0.92,
                "sample_text": "本节讲解 RAG 检索增强生成。",
                "hotwords": [{"term": "RAG", "aliases": [], "evidence": "讲解 RAG"}],
            }
            output.write_text(
                json.dumps(profile, ensure_ascii=False),
                encoding="utf-8",
            )
        elif phase == "transcribe":
            output = Path(command[command.index("--output") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "lesson.json").write_text(
                json.dumps(
                    {
                        "source": command[2],
                        "segments": [{"id": "0", "start": 0, "end": 4, "text": "RAG 检索增强生成"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        elif phase == "verify":
            output = Path(command[command.index("--output-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "lesson.verified.json").write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "id": "0",
                                "start": 0,
                                "end": 4,
                                "raw_text": "RAG 检索增强生成",
                                "final_text": "RAG 检索增强生成",
                                "knowledge_ready": True,
                                "corrections": [],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )


class KnowledgePipelineTests(unittest.TestCase):
    @staticmethod
    def publish_current(api, target_root, copied_video):
        video = api.task["videos"][api.task["current_index"]]
        entries = load_index(target_root)
        entries.append(
            {
                "knowledge_id": f"knowledge-{video['video_id']}",
                "video_id": video["video_id"],
                "domain": "AI",
                "title": "RAG",
                "content": "检索增强生成",
                "video_relative_path": copied_video.relative_to(target_root).as_posix(),
                "evidence_start": 0,
                "evidence_end": 4,
                "evidence_text": "RAG 检索增强生成",
                "obsidian_relative_path": "Obsidian知识库/AI/RAG.md",
            }
        )
        write_index(target_root, entries)
        wiki = target_root / "Obsidian知识库" / "AI" / f"{video['video_id']}.md"
        wiki.parent.mkdir(parents=True, exist_ok=True)
        wiki.write_text("# RAG\n", encoding="utf-8")
        return {"video_id": video["video_id"], "wiki": str(wiki), "wiki_title": "RAG", "knowledge_count": 1}

    def test_auto_confirms_and_resume_skips_completed_expensive_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "knowledge"
            source = root / "lesson.mp4"
            source.write_bytes(b"video-content")
            initialize_space(space)
            task = create_task(space, [source])
            task.update(status="running", stages=[])
            api = FakeApi(space, task)

            def fake_publish(target_root, copied_video, _verified, _client, domain_hint="", raw_transcript_path=None):
                self.assertEqual(domain_hint, "AI")
                self.assertTrue(raw_transcript_path.is_file())
                video = api.task["videos"][0]
                write_index(
                    target_root,
                    [
                        {
                            "knowledge_id": "knowledge-test",
                            "video_id": video["video_id"],
                            "domain": "AI",
                            "title": "RAG",
                            "content": "检索增强生成",
                            "video_relative_path": copied_video.relative_to(target_root).as_posix(),
                            "evidence_start": 0,
                            "evidence_end": 4,
                            "evidence_text": "RAG 检索增强生成",
                            "obsidian_relative_path": "Obsidian知识库/AI/RAG.md",
                        }
                    ],
                )
                wiki = target_root / "Obsidian知识库" / "AI" / "RAG.md"
                wiki.parent.mkdir(parents=True, exist_ok=True)
                wiki.write_text("# RAG\n", encoding="utf-8")
                return {"video_id": video["video_id"], "wiki": str(wiki), "wiki_title": "RAG", "knowledge_count": 1}

            with patch("knowledge_pipeline.publish_verified", side_effect=fake_publish) as publish:
                run_resumable_task(
                    api,
                    python=Path("python.exe"),
                    knowledge_worker=Path("knowledge_worker.py"),
                    transcriber=Path("transcribe.py"),
                    reviewer=Path("whole_file_review.py"),
                )
                self.assertEqual(api.task["status"], "completed")
                self.assertEqual(api.task["videos"][0]["hotword_status"], "auto_confirmed")
                self.assertEqual(api.subprocess_phases, ["analyze", "transcribe", "verify"])
                self.assertEqual(publish.call_count, 1)

                api.task["status"] = "interrupted"
                api.task["videos"][0]["status"] = "interrupted"
                api.running = True
                api.subprocess_phases.clear()
                run_resumable_task(
                    api,
                    python=Path("python.exe"),
                    knowledge_worker=Path("knowledge_worker.py"),
                    transcriber=Path("transcribe.py"),
                    reviewer=Path("whole_file_review.py"),
                )

            self.assertEqual(api.task["status"], "completed")
            self.assertEqual(api.subprocess_phases, [])
            self.assertEqual(publish.call_count, 1)
            self.assertTrue(all(stage["status"] == "skipped" for stage in api.task["stages"]))

    def test_uncertain_domain_does_not_require_user_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "knowledge"
            unclear = root / "unclear.mp4"
            clear = root / "clear.mp4"
            unclear.write_bytes(b"unclear-video")
            clear.write_bytes(b"clear-video")
            initialize_space(space)
            task = create_task(space, [unclear, clear])
            task.update(status="running", stages=[])
            api = FakeApi(space, task)
            api.analysis_profiles = {
                "unclear.mp4": {
                    "status": "ready",
                    "category": "未分类",
                    "confidence": 0.31,
                    "sample_text": "样本里出现胰岛素。",
                    "hotwords": [{"term": "胰岛素", "aliases": [], "evidence": "样本出现"}],
                }
            }

            published_domains = []

            def fake_publish(target_root, copied_video, _verified, _client, domain_hint="", raw_transcript_path=None):
                published_domains.append(domain_hint)
                return self.publish_current(api, target_root, copied_video)

            with patch("knowledge_pipeline.publish_verified", side_effect=fake_publish) as publish:
                run_resumable_task(
                    api,
                    python=Path("python.exe"),
                    knowledge_worker=Path("knowledge_worker.py"),
                    transcriber=Path("transcribe.py"),
                    reviewer=Path("whole_file_review.py"),
                )

            states = {item["name"]: item["status"] for item in api.task["videos"]}
            self.assertEqual(states["unclear.mp4"], "completed")
            self.assertEqual(states["clear.mp4"], "completed")
            self.assertEqual(api.task["status"], "completed")
            self.assertIsNone(api.task["hotword_profile"])
            self.assertEqual(api.subprocess_phases.count("analyze"), 2)
            self.assertEqual(api.subprocess_phases.count("transcribe"), 2)
            self.assertEqual(api.subprocess_phases.count("verify"), 2)
            self.assertEqual(publish.call_count, 2)
            self.assertCountEqual(published_domains, ["未分类", "AI"])


if __name__ == "__main__":
    unittest.main()
