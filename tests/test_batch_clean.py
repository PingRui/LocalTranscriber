from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from batch_clean import clean_batch, discover_transcripts, expected_outputs, load_term_aliases


def write_transcript(path: Path, text: str = "菠菜的甲梨子含量很丰富。") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source": str(path.with_suffix(".mp4")),
                "segments": [{"start": 0.0, "end": 3.0, "text": text}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class BatchCleanTests(unittest.TestCase):
    def test_term_aliases_require_a_non_empty_json_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "aliases.json"
            valid.write_text(json.dumps({"甲梨子": "钾离子", "缺甲": "缺钾"}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_term_aliases(valid), {"甲梨子": "钾离子", "缺甲": "缺钾"})

            invalid = Path(directory) / "invalid.json"
            invalid.write_text(json.dumps(["甲梨子", "钾离子"], ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON 对象"):
                load_term_aliases(invalid)

    def test_recursive_scan_excludes_non_transcripts_and_repaired_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_transcript(root / "a" / "one.json")
            second = write_transcript(root / "b" / "two.json")
            (root / "config.json").write_text(json.dumps({"model": "medium"}), encoding="utf-8")
            (root / "b" / "two.llm.json").write_text(
                json.dumps({"source": "video.mp4", "segments": [{"start": 0, "end": 1, "text": "校订稿"}]}),
                encoding="utf-8",
            )

            records = discover_transcripts(root)

            self.assertEqual([Path(item["path"]) for item in records], [first.resolve(), second.resolve()])
            self.assertTrue(all(item["status"] == "等待清洗" for item in records))

    def test_failure_isolated_and_complete_outputs_resume_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = write_transcript(root / "good.json")
            bad = write_transcript(root / "nested" / "bad.json")
            calls: list[Path] = []

            def fake_repair(path: Path, *_args, **_kwargs) -> list[Path]:
                calls.append(path)
                if path.name == "bad.json":
                    raise RuntimeError("fixture failure")
                outputs = expected_outputs(path)
                for output in outputs:
                    output.write_text("ok", encoding="utf-8")
                return outputs

            first = clean_batch(
                root,
                "local",
                "qwen3-4b-proofreader",
                "http://127.0.0.1:1234/v1",
                skip_existing=True,
                repair=fake_repair,
            )
            self.assertEqual(first, {"total": 2, "completed": 1, "failed": 1, "skipped": 0})
            self.assertTrue(all(path.is_file() for path in expected_outputs(good)))
            self.assertFalse(all(path.is_file() for path in expected_outputs(bad)))

            calls.clear()
            second = clean_batch(
                root,
                "local",
                "qwen3-4b-proofreader",
                "http://127.0.0.1:1234/v1",
                skip_existing=True,
                repair=fake_repair,
            )
            self.assertEqual(second, {"total": 2, "completed": 0, "failed": 1, "skipped": 1})
            self.assertEqual(calls, [bad.resolve()])

    def test_complete_output_set_marks_record_as_existing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_transcript(Path(directory) / "lesson.json")
            for output in expected_outputs(path):
                output.write_text("ok", encoding="utf-8")

            record = discover_transcripts(Path(directory))[0]

            self.assertTrue(record["complete"])
            self.assertEqual(record["status"], "已有完整结果")
            self.assertEqual(len(record["outputs"]), 5)


if __name__ == "__main__":
    unittest.main()
