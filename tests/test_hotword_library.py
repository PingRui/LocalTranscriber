from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hotword_library import (
    apply_hotword_suggestions,
    combine_hotword_sets,
    delete_hotword_set,
    list_hotword_sets,
    rename_hotword_set,
    touch_hotword_set,
    update_hotword_set,
)


class HotwordLibraryTests(unittest.TestCase):
    def test_ready_task_file_can_be_reused_renamed_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "nutrition.json"
            path.write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "category": "营养健康",
                        "updated_at": "2026-08-12T10:00:00+08:00",
                        "hotwords": [
                            {"term": "胰岛素", "aliases": ["姨岛素"], "evidence": "样本出现"},
                            {"term": "胰高血糖素"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            items = list_hotword_sets(root)
            self.assertEqual(items[0]["id"], "nutrition")
            self.assertEqual(items[0]["count"], 2)
            self.assertEqual(items[0]["preview"][0], "胰岛素")

            renamed = rename_hotword_set(root, "nutrition", "内分泌课程")
            self.assertEqual(renamed["name"], "内分泌课程")
            touched = touch_hotword_set(root, "nutrition")
            self.assertEqual(touched["use_count"], 1)
            self.assertTrue(touched["last_used_at"])

            delete_hotword_set(root, "nutrition")
            self.assertEqual(list_hotword_sets(root), [])

    def test_incomplete_or_invalid_task_files_are_not_listed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "collecting.json").write_text(
                json.dumps({"status": "collecting", "hotwords": [{"term": "胰岛素"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "broken.json").write_text("not-json", encoding="utf-8")
            self.assertEqual(list_hotword_sets(root), [])

    def test_multiple_sets_can_be_combined_and_managed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for identifier, category, term in (("a", "营养", "胰岛素"), ("b", "康复", "步态训练")):
                (root / f"{identifier}.json").write_text(
                    json.dumps({"status": "ready", "category": category, "tags": [category], "hotwords": [{"term": term}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
            runtime = combine_hotword_sets(root, ["a", "b"], root / "runtime" / "task.json")
            self.assertEqual(runtime["count"], 2)
            self.assertEqual(runtime["source_set_ids"], ["a", "b"])
            updated = update_hotword_set(root, "a", {"name": "营养课程", "tags": ["医学", "营养"], "hotwords": "胰岛素\n血糖"})
            self.assertEqual(updated["tags"], ["医学", "营养"])
            self.assertEqual(updated["count"], 2)
            applied = apply_hotword_suggestions(root, "a", [{"type": "alias", "target": "胰岛素", "alias": "姨岛素"}])
            self.assertIn("姨岛素", applied["hotwords"][0]["aliases"])


if __name__ == "__main__":
    unittest.main()
