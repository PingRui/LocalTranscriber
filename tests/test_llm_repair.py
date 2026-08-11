from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm_repair import repair_segments, write_repair_outputs


class FakeClient:
    def complete_json(self, _system_prompt: str, payload: dict[str, object]):
        returned = []
        for item in payload["segments"]:
            text = item["text"]
            corrected = text.replace("Cloud Code", "Claude Code")
            corrected = corrected.replace("It's not an opportunity", "Is that an opportunity")
            returned.append({"id": item["id"], "corrected_text": corrected, "reason": "context check"})
        return {"segments": returned}, {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


class LlmRepairTests(unittest.TestCase):
    def test_repairs_context_errors_and_marks_negation_change(self) -> None:
        raw = [
            {"start": 0.0, "end": 2.0, "text": "Cloud Code is useful."},
            {"start": 2.0, "end": 4.0, "text": "It's not an opportunity for us."},
        ]
        repaired, corrections, usage = repair_segments(
            raw,
            {"title": "Claude Code interview", "terms": ["Claude Code"]},
            FakeClient(),
        )

        self.assertEqual(raw[0]["text"], "Cloud Code is useful.")
        self.assertEqual(repaired[0]["text"], "Claude Code is useful.")
        self.assertEqual(repaired[1]["text"], "Is that an opportunity for us.")
        self.assertFalse(corrections[0]["review_required"])
        self.assertTrue(corrections[1]["review_required"])
        self.assertIn("negation_changed", corrections[1]["risks"])
        self.assertEqual(usage["total_tokens"], 120)

    def test_writes_separate_outputs_without_secret(self) -> None:
        segments = [{"start": 0.0, "end": 1.0, "text": "Claude Code", "llm_repaired": True}]
        metadata = {"source": "video.mp4", "segments": [{"start": 0.0, "end": 1.0, "text": "Cloud Code"}]}
        corrections = [
            {
                "accepted": True,
                "review_required": False,
                "original": "Cloud Code",
                "corrected": "Claude Code",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            paths = write_repair_outputs(metadata, segments, corrections, {"total_tokens": 10}, Path(directory), "video", "deepseek-v4-flash")
            self.assertEqual(len(paths), 5)
            self.assertTrue(all(path.is_file() for path in paths))
            combined = "".join(path.read_text(encoding="utf-8-sig") for path in paths)
            self.assertNotIn("sk-", combined)
            data = json.loads((Path(directory) / "video.llm.json").read_text(encoding="utf-8"))
            self.assertEqual(data["segments"][0]["text"], "Claude Code")


if __name__ == "__main__":
    unittest.main()
