from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trusted_pipeline import (
    ProviderConfig,
    detect_domain,
    discover_transcripts,
    chunks,
    guard_correction,
    process_transcript,
    sample_text,
    search_verified,
)


class FakeClient:
    def __init__(self, model: str, responses: list[dict[str, object]]) -> None:
        self.model = model
        self.responses = list(responses)

    def complete_json(self, _system: str, _payload: object, max_tokens: int = 0):
        return self.responses.pop(0), {}


def write_transcript(path: Path, video: Path, texts: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source": str(video),
                "segments": [
                    {"start": index * 5.0, "end": index * 5.0 + 4.0, "text": text}
                    for index, text in enumerate(texts)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class TrustedPipelineTests(unittest.TestCase):
    def test_discovery_uses_one_verified_output_and_excludes_derived_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "trusted"
            raw = write_transcript(root / "lesson.json", root / "lesson.mp4", ["胰岛素影响血糖。"])
            (root / "lesson.llm.json").write_text(raw.read_text(encoding="utf-8"), encoding="utf-8")

            records = discover_transcripts(root, output)

            self.assertEqual(len(records), 1)
            self.assertEqual(Path(records[0]["path"]), raw.resolve())
            self.assertTrue(str(records[0]["output"]).endswith(".verified.json"))

    def test_domain_profile_is_restricted_to_structured_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = write_transcript(root / "lesson.json", root / "lesson.mp4", ["胰岛素抵抗与血糖调节。"] * 20)
            records = discover_transcripts(root, root / "trusted")
            hotwords = [f"专业词{i}" for i in range(20)]
            client = FakeClient(
                "domain-model",
                [{"domain": "营养与内分泌", "topics": ["胰岛素", "血糖", "代谢"], "hotwords": hotwords, "summary": "样本集中讨论代谢。"}],
            )

            profile = detect_domain(records, client)

            self.assertEqual(profile["domain"], "营养与内分泌")
            self.assertEqual(len(profile["hotwords"]), 20)
            self.assertIn(raw.name, profile["sample_files"])

    def test_domain_samples_fit_small_local_model_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(8):
                write_transcript(root / f"lesson-{index}.json", root / f"lesson-{index}.mp4", ["胰岛素与代谢。" * 80])
            records = discover_transcripts(root, root / "trusted")
            samples = sample_text(records)
            self.assertLessEqual(sum(len(item["text"]) for item in samples), 2800)
            self.assertLessEqual(len(samples), 6)

    def test_cross_validation_writes_one_file_and_excludes_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "lesson.mp4"
            video.write_bytes(b"video")
            raw = write_transcript(root / "lesson.json", video, ["一岛素影响血糖。", "剂量是10毫克。", "普通内容。"])
            profile = {"domain": "营养与内分泌", "topics": ["胰岛素"], "hotwords": ["胰岛素"]}
            corrector = FakeClient(
                "corrector",
                [{"corrections": [
                    {"id": 0, "original_span": "一岛素", "replacement": "胰岛素", "reason": "专业词纠正"},
                    {"id": 1, "original_span": "10", "replacement": "20", "reason": "数字修改"},
                ]}],
            )
            verifier = FakeClient(
                "verifier",
                [{"verifications": [{"id": 0, "decision": "accept", "reason": "领域与上下文支持"}]}],
            )
            output, stats = process_transcript(raw, root, root / "trusted", profile, corrector, verifier)
            payload = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(len(list((root / "trusted").glob("*.verified.json"))), 1)
            self.assertEqual(payload["segments"][0]["final_text"], "胰岛素影响血糖。")
            self.assertTrue(payload["segments"][0]["knowledge_ready"])
            self.assertEqual(payload["segments"][1]["verification"], "reject")
            self.assertFalse(payload["segments"][1]["knowledge_ready"])
            self.assertEqual(stats["accepted"], 1)
            self.assertEqual(stats["rejected"], 1)
            self.assertEqual(payload["models"]["verification_mode"], "dual_model")

            hits = search_verified(root / "trusted", "胰岛素")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["source_video"], str(video))
            self.assertEqual(hits[0]["start_seconds"], 0.0)

    def test_program_guard_rejects_number_and_negation_changes(self) -> None:
        self.assertEqual(guard_correction("每天10毫克", "每天20毫克"), (False, ["numbers_changed"]))
        accepted, risks = guard_correction("这个方法有效", "这个方法没有效")
        self.assertFalse(accepted)
        self.assertIn("negation_changed", risks)

    def test_correction_chunks_fit_four_thousand_token_models(self) -> None:
        segments = [{"text": "胰岛素与血糖调节。" * 30} for _ in range(30)]
        ranges = chunks(segments)
        self.assertTrue(all(end - start <= 12 for start, end in ranges))
        self.assertTrue(all(sum(len(segments[index]["text"]) for index in range(start, end)) <= 1800 for start, end in ranges))

    def test_provider_config_keeps_two_model_roles_independent(self) -> None:
        corrector = ProviderConfig(model="model-a")
        verifier = ProviderConfig(model="model-b")
        self.assertNotEqual(corrector.model, verifier.model)


if __name__ == "__main__":
    unittest.main()
