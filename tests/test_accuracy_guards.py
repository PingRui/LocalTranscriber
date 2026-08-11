from __future__ import annotations

import unittest

from transcribe import canonicalize_entities, repetition_risk


class AccuracyGuardTests(unittest.TestCase):
    def test_entity_normalization_changes_only_context_backed_variants(self) -> None:
        segments = [
            {
                "start": 1.0,
                "end": 2.0,
                "text": "Like Diane said, Gary Tan uses Cloud Code.",
                "avg_logprob": -0.1,
                "compression_ratio": 1.2,
                "no_speech_prob": 0.0,
            }
        ]
        context = {
            "people": ["Dianne Penn", "Garry Tan", "Mike Krieger"],
            "primary_people": ["Dianne Penn"],
            "terms": ["Claude Code"],
        }

        corrections = canonicalize_entities(segments, context)

        self.assertEqual(segments[0]["text"], "Like Dianne said, Garry Tan uses Claude Code.")
        self.assertEqual(len(corrections), 1)
        self.assertNotIn("Mike", segments[0]["text"])

    def test_repetition_guard_detects_prompt_loop(self) -> None:
        segments = [
            {
                "start": 0.0,
                "end": 30.0,
                "text": ", ".join(["AGI"] * 30),
                "avg_logprob": -0.2,
                "compression_ratio": 4.2,
                "no_speech_prob": 0.0,
            }
        ]
        self.assertTrue(repetition_risk(segments))


if __name__ == "__main__":
    unittest.main()
