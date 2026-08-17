from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import transcribe
from task_hotwords import TaskHotwordDiscovery
from transcribe import prepare_task_hotwords, representative_hotword_sources, transcribe_one


class FakeHotwordClient:
    model = "remote-hotword-model"
    base_url = "https://relay.example.com/v1"

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, _system_prompt, _user_payload, max_tokens=4096):
        self.calls += 1
        return (
            {
                "category": "营养学 / 激素",
                "hotwords": [
                    {"term": "胰岛素", "aliases": ["移导素"], "evidence": "课程讨论激素"},
                    {"term": "五羟色胺", "aliases": ["五腔四胺"], "evidence": "神经递质"},
                ],
            },
            {},
        )


class FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.avg_logprob = -0.1
        self.compression_ratio = 1.0
        self.no_speech_prob = 0.0


class FakeWhisperModel:
    def __init__(self) -> None:
        self.calls = []

    def transcribe(self, _source: str, **kwargs):
        self.calls.append(kwargs)
        info = SimpleNamespace(duration=300.0, language="zh", language_probability=0.99)
        if kwargs.get("clip_timestamps") in ([0.0, 180.0], [0.0, 90.0]):
            text = "这节课讨论胰岛素、血糖、五羟色胺和营养代谢。" * 40
            return iter([FakeSegment(0.0, 170.0, text)]), info
        return iter(
            [
                FakeSegment(0.0, 170.0, "完整课程从胰岛素开始。"),
                FakeSegment(180.0, 240.0, "后续继续讨论胰岛素的释放机制。"),
            ]
        ), info


class TaskHotwordTests(unittest.TestCase):
    def test_ready_profile_may_explicitly_have_no_hotwords(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "no-hotwords.json"
            state_file.write_text(
                json.dumps({"status": "ready", "category": "日常记录", "confidence": 0.9, "hotwords": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            discovery = TaskHotwordDiscovery.from_file(state_file)
            self.assertTrue(discovery.ready)
            self.assertEqual(discovery.hotwords, [])

    def test_hotwords_are_prepared_before_full_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "lesson.mp4"
            source.write_bytes(b"fixture")
            state_file = root / "task-hotwords.json"
            hotword_client = FakeHotwordClient()
            discovery = TaskHotwordDiscovery(hotword_client, state_file, min_chars=300)
            model = FakeWhisperModel()
            prepare_task_hotwords([source], model, "zh", discovery)
            transcribe_one(
                source,
                model,
                "medium",
                "cpu",
                "int8",
                str(root / "results"),
                "zh",
                "",
                task_hotword_discovery=discovery,
            )
            self.assertEqual(model.calls[0]["clip_timestamps"], [0.0, 180.0])
            self.assertEqual(model.calls[1]["clip_timestamps"], "0")
            self.assertIn("胰岛素", model.calls[1]["hotwords"])
            self.assertEqual(hotword_client.calls, 1)
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["category"], "营养学 / 激素")
            self.assertEqual(saved["status"], "ready")
            self.assertNotIn("api_key", json.dumps(saved, ensure_ascii=False))
            transcript = json.loads((root / "results" / "lesson.json").read_text(encoding="utf-8"))
            self.assertEqual(transcript["task_hotwords"]["status"], "ready")
            self.assertEqual(len(transcript["segments"]), 2)

            next_source = root / "lesson-2.mp4"
            next_source.write_bytes(b"fixture")
            next_model = FakeWhisperModel()
            transcribe_one(
                next_source,
                next_model,
                "medium",
                "cpu",
                "int8",
                str(root / "results"),
                "zh",
                "",
                task_hotword_discovery=discovery,
            )
            self.assertEqual(len(next_model.calls), 1)
            self.assertEqual(next_model.calls[0]["clip_timestamps"], "0")
            self.assertIn("胰岛素", next_model.calls[0]["hotwords"])
            self.assertEqual(hotword_client.calls, 1)

    def test_batch_sampling_is_bounded_and_representative(self) -> None:
        sources = [Path(f"lesson-{index}.mp4") for index in range(10)]
        self.assertEqual(
            representative_hotword_sources(sources),
            [sources[0], sources[5], sources[9]],
        )

    def test_raw_phase_can_finish_without_publishing_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "lesson.mp4"
            source.write_bytes(b"fixture")
            events: list[str] = []
            with patch.object(transcribe, "emit_event", side_effect=lambda _enabled, event, **_payload: events.append(event)):
                transcribe_one(
                    source, FakeWhisperModel(), "medium", "cpu", "int8",
                    str(root / "results"), "zh", "", emit_completion=False,
                )
            self.assertIn("file_transcribed", events)
            self.assertNotIn("file_done", events)


if __name__ == "__main__":
    unittest.main()
