from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trusted_pipeline import load_transcript, search_verified
from whole_file_review import process_file, review_full_file


class FakeReviewClient:
    model = "long-context-model"
    base_url = "https://relay.example.com/v1"

    def __init__(self, fail_on_call: int | None = None, incomplete: bool = False) -> None:
        self.fail_on_call = fail_on_call
        self.incomplete = incomplete
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system: str, payload: object, max_tokens: int = 0):
        call = {"system": system, "payload": payload, "max_tokens": max_tokens}
        self.calls.append(call)
        if self.fail_on_call == len(self.calls):
            raise TimeoutError("simulated read timeout")
        assert isinstance(payload, dict)
        if "candidate_corrections" in payload:
            candidate_ids = [item["correction_id"] for item in payload["candidate_corrections"]]
            return {
                "review_complete": not self.incomplete,
                "approved_correction_ids": candidate_ids,
                "rejected_corrections": [],
            }, {"total_tokens": 200}
        segments = payload.get("target_segments", [])
        corrections = []
        for item in segments:
            if item["id"] == "a" and "姨岛素" in item["text"]:
                corrections.append(
                    {
                        "segment_id": "a",
                        "original_span": "姨岛素",
                        "replacement": "胰岛素",
                        "reason": "全文术语一致",
                        "confidence": "high",
                    }
                )
        return {"review_complete": not self.incomplete, "corrections": corrections}, {"total_tokens": 1200}


def write_transcript(path: Path, video: Path, long: bool = False) -> Path:
    segments: list[dict[str, Any]] = [
        {"id": "a", "start": 0, "end": 5, "text": "姨岛素影响血糖。"},
        {"id": "b", "start": 5, "end": 10, "text": "每天使用10毫克。"},
        {"id": "c", "start": 10, "end": 15, "text": "背景噪声内容。", "review_reasons": ["low_confidence"]},
    ]
    if long:
        for index in range(3, 620):
            segments.append(
                {
                    "id": str(index),
                    "start": index * 5,
                    "end": index * 5 + 5,
                    "text": f"这是第{index}个连续转录片段，用来验证长文件可以分段保存断点并继续处理。",
                }
            )
    path.write_text(
        json.dumps(
            {
                "source": str(video),
                "hotwords": "胰岛素, 胰高血糖素",
                "task_hotwords": {"category": "营养健康"},
                "segments": segments,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class WholeFileReviewTests(unittest.TestCase):
    def test_small_file_uses_one_request_and_applies_only_safe_local_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "lesson.mp4"
            video.write_bytes(b"video")
            raw = write_transcript(root / "lesson.json", video)
            raw_before = raw.read_text(encoding="utf-8")
            client = FakeReviewClient()

            outputs, stats = process_file(raw, root, root / "可信数据结果", client, 128000)

            self.assertEqual(len(client.calls), 1)
            sent = client.calls[0]["payload"]
            self.assertEqual(len(sent["target_segments"]), 3)
            self.assertIn("胰岛素", sent["hotwords"])
            self.assertEqual(client.calls[0]["max_tokens"], 1024)
            self.assertEqual(sent["file"], "lesson.json")
            self.assertEqual(sent["source_video"], "lesson.mp4")
            verified = json.loads(outputs[0].read_text(encoding="utf-8"))
            correction_log = json.loads(outputs[1].read_text(encoding="utf-8"))
            self.assertEqual(verified["segments"][0]["final_text"], "胰岛素影响血糖。")
            self.assertTrue(verified["segments"][0]["knowledge_ready"])
            self.assertFalse(verified["segments"][2]["knowledge_ready"])
            self.assertEqual(stats["accepted"], 1)
            self.assertEqual(stats["chunk_count"], 1)
            self.assertFalse(stats["global_consistency"])
            self.assertTrue(correction_log["review_complete"])
            self.assertEqual(raw.read_text(encoding="utf-8"), raw_before)
            hits = search_verified(root / "可信数据结果", "胰岛素")
            self.assertEqual(hits[0]["start_seconds"], 0.0)

    def test_long_file_resumes_from_last_completed_chunk_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "long.mp4"
            video.write_bytes(b"video")
            raw = write_transcript(root / "long.json", video, long=True)
            output_root = root / "可信数据结果"

            first = FakeReviewClient(fail_on_call=2)
            with self.assertRaisesRegex(TimeoutError, "read timeout"):
                process_file(raw, root, output_root, first, 128000)

            self.assertFalse(any(output_root.glob("*.verified.json")))
            chunk_files = list((output_root / ".review-checkpoints").rglob("chunk-*.json"))
            self.assertEqual(len(chunk_files), 1)

            second = FakeReviewClient()
            outputs, stats = process_file(raw, root, output_root, second, 128000)

            self.assertGreater(stats["chunk_count"], 1)
            self.assertEqual(stats["chunks_reused"], 1)
            self.assertEqual(len(second.calls), stats["chunk_count"])
            self.assertIn("candidate_corrections", second.calls[-1]["payload"])
            verified = json.loads(outputs[0].read_text(encoding="utf-8"))
            self.assertEqual(
                verified["models"]["verification_mode"],
                "resumable_chunked_with_global_consistency",
            )
            self.assertEqual(verified["segments"][0]["final_text"], "胰岛素影响血糖。")

    def test_changed_transcript_does_not_reuse_stale_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = write_transcript(root / "long.json", root / "long.mp4", long=True)
            output_root = root / "可信数据结果"
            first = FakeReviewClient(fail_on_call=2)
            with self.assertRaises(TimeoutError):
                process_file(raw, root, output_root, first, 128000)

            payload = json.loads(raw.read_text(encoding="utf-8"))
            payload["segments"][10]["text"] += "内容发生变化。"
            raw.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            second = FakeReviewClient()
            _outputs, stats = process_file(raw, root, output_root, second, 128000)
            self.assertEqual(stats["chunks_reused"], 0)
            self.assertEqual(len(second.calls), stats["chunk_count"] + 1)

    def test_file_that_does_not_fit_small_context_is_split_instead_of_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = write_transcript(root / "lesson.json", root / "lesson.mp4")
            payload = json.loads(raw.read_text(encoding="utf-8"))
            payload["segments"] = [
                {
                    "id": str(index),
                    "start": index * 5,
                    "end": index * 5 + 5,
                    "text": "胰岛素参与血糖调节，这是一段需要在小上下文模型中分批校验的连续转录。",
                }
                for index in range(220)
            ]
            raw.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            client = FakeReviewClient()

            _outputs, stats = process_file(raw, root, root / "可信数据结果", client, 8_000)

            self.assertGreater(stats["chunk_count"], 1)
            self.assertTrue(stats["global_consistency"])
            self.assertEqual(len(client.calls), stats["chunk_count"] + 1)

    def test_incomplete_model_output_is_never_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = write_transcript(root / "lesson.json", root / "lesson.mp4")
            transcript = load_transcript(raw)
            self.assertIsNotNone(transcript)
            client = FakeReviewClient(incomplete=True)
            with self.assertRaisesRegex(RuntimeError, "没有确认完成"):
                review_full_file(client, transcript, 128000)


if __name__ == "__main__":
    unittest.main()
