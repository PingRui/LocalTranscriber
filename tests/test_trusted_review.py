from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trusted_review import SUGGESTIONS_FILE_NAME, load_review_results, mark_suggestions_applied


class TrustedReviewTests(unittest.TestCase):
    def test_loads_visible_diffs_and_compares_hotwords(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "4.激素分类与胰岛素" / "lesson.mp4"
            verified = root / "abc.verified.json"
            corrections = root / "abc.corrections.json"
            verified.write_text(
                json.dumps(
                    {
                        "source": str(source),
                        "domain": {"name": "未分类"},
                        "segments": [
                            {"id": "1", "start": 12.5, "end": 18.0, "raw_text": "基数", "final_text": "激素"},
                            {"id": "2", "start": 19.0, "end": 23.0, "raw_text": "移导术", "final_text": "胰岛素"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            corrections.write_text(
                json.dumps(
                    {
                        "source": str(source),
                        "stats": {"accepted": 2, "rejected": 1, "uncertain": 0, "proposed": 3},
                        "corrections": [
                            {"segment_id": "1", "original_span": "基数", "replacement": "激素", "status": "applied", "reason": "术语纠错"},
                            {"segment_id": "2", "original_span": "移导术", "replacement": "胰岛素", "status": "applied", "reason": "术语纠错"},
                            {"segment_id": "3", "original_span": "不在原文", "replacement": "候选", "status": "rejected", "status_reason": "原错误文字不在指定片段中"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            library = [
                {
                    "id": "set-1",
                    "name": "营养学",
                    "hotwords": [{"term": "激素", "aliases": [], "evidence": ""}],
                }
            ]

            review = load_review_results(root, library)

            self.assertEqual(review["summary"]["applied"], 2)
            self.assertEqual(review["summary"]["pending"], 1)
            self.assertEqual(review["corrections"][0]["start_seconds"], 12.5)
            self.assertEqual(review["corrections"][0]["category"], "激素分类与胰岛素")
            by_target = {item["target"]: item for item in review["suggestions"]}
            self.assertEqual(by_target["激素"]["action"], "补充别名")
            self.assertEqual(by_target["胰岛素"]["action"], "新增热词")
            self.assertTrue((root / SUGGESTIONS_FILE_NAME).is_file())

            mark_suggestions_applied(root, [by_target["胰岛素"]["id"]])
            refreshed = load_review_results(root, library)
            refreshed_by_target = {item["target"]: item for item in refreshed["suggestions"]}
            self.assertEqual(refreshed_by_target["胰岛素"]["status"], "applied")


if __name__ == "__main__":
    unittest.main()
