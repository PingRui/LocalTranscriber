from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from domain_hotwords import (
    assess_hotword_profile,
    hotword_store_path,
    learn_from_verified,
    load_hotword_store,
    save_hotword_store,
)


def write_verified(path: Path, raw: str, final: str, corrections=None) -> Path:
    path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "id": "0",
                        "raw_text": raw,
                        "final_text": final,
                        "knowledge_ready": True,
                        "corrections": corrections or [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class DomainHotwordTests(unittest.TestCase):
    def test_only_evidence_backed_candidates_are_auto_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = {
                "category": "AI",
                "confidence": 0.91,
                "sample_text": "这一节介绍 RAG，也有人把它错听成阿格。",
                "hotwords": [
                    {"term": "RAG", "aliases": ["阿格"], "evidence": "介绍 RAG"},
                    {"term": "Transformer", "aliases": [], "evidence": "模型结构"},
                ],
            }

            result = assess_hotword_profile(root, profile)

            self.assertTrue(result["auto_confirmed"])
            self.assertEqual(result["selected_terms"], ["RAG"])
            self.assertEqual(result["rejected_candidates"][0]["term"], "Transformer")

    def test_unknown_or_low_confidence_domain_does_not_block_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = assess_hotword_profile(
                Path(directory),
                {
                    "category": "未分类",
                    "confidence": 0.4,
                    "sample_text": "出现胰岛素",
                    "hotwords": [{"term": "胰岛素", "aliases": [], "evidence": "出现胰岛素"}],
                },
            )
            self.assertTrue(result["auto_confirmed"])
            self.assertEqual(result["category"], "未分类")
            self.assertEqual(result["manual_reasons"], [])

    def test_confident_domain_without_professional_terms_can_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            result = assess_hotword_profile(
                Path(directory),
                {
                    "category": "日常记录",
                    "confidence": 0.91,
                    "sample_text": "今天简单记录一下生活安排。",
                    "hotwords": [],
                },
            )
            self.assertTrue(result["auto_confirmed"])
            self.assertEqual(result["selected_terms"], [])

    def test_saturated_domain_reuses_stable_terms_without_new_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_hotword_store(
                root,
                {
                    "domains": {
                        "ai": {
                            "name": "AI",
                            "saturated": True,
                            "terms": [
                                {
                                    "term": "RAG",
                                    "aliases": ["阿格"],
                                    "status": "stable",
                                    "video_count": 5,
                                    "correct_occurrences": 20,
                                }
                            ],
                        }
                    }
                },
            )
            result = assess_hotword_profile(
                root,
                {"category": "AI", "confidence": 0.9, "sample_text": "今天继续讲系统设计", "hotwords": []},
            )
            self.assertTrue(result["auto_confirmed"])
            self.assertTrue(result["saturated_domain"])
            self.assertEqual(result["analysis_mode"], "incremental")
            self.assertEqual(result["selected_terms"], ["RAG"])

    def test_verified_feedback_promotes_terms_and_marks_domain_saturated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = {
                "category": "AI",
                "hotwords": [{"term": "RAG", "aliases": ["阿格"], "evidence": "RAG"}],
            }
            assessment = {
                "category": "AI",
                "selected": [{"term": "RAG", "aliases": ["阿格"], "source": "sample"}],
            }
            for index in range(3):
                verified = write_verified(
                    root / f"lesson-{index}.verified.json",
                    "RAG RAG RAG RAG",
                    "RAG RAG RAG RAG",
                )
                learn_from_verified(root, f"video-{index}", profile, assessment, verified)

            store = load_hotword_store(root)
            domain = store["domains"]["ai"]
            term = domain["terms"][0]
            self.assertTrue(domain["saturated"])
            self.assertEqual(term["status"], "stable")
            self.assertEqual(term["video_count"], 3)
            self.assertEqual(term["correct_occurrences"], 12)

    def test_learning_is_idempotent_and_accepted_correction_becomes_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = {
                "category": "营养",
                "hotwords": [{"term": "胰岛素", "aliases": [], "evidence": "胰岛素"}],
            }
            assessment = {
                "category": "营养",
                "selected": [{"term": "胰岛素", "aliases": [], "source": "sample"}],
            }
            verified = write_verified(
                root / "lesson.verified.json",
                "移到素",
                "胰岛素",
                [{"status": "applied", "original_span": "移到素", "replacement": "胰岛素"}],
            )
            learn_from_verified(root, "video-one", profile, assessment, verified)
            learn_from_verified(root, "video-one", profile, assessment, verified)

            term = load_hotword_store(root)["domains"]["营养"]["terms"][0]
            self.assertEqual(term["corrected_to"], 1)
            self.assertEqual(term["correct_occurrences"], 1)
            self.assertIn("移到素", term["aliases"])
            self.assertTrue(hotword_store_path(root).is_file())


if __name__ == "__main__":
    unittest.main()
