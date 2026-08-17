from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from model_provider_config import load_provider_settings, save_provider_settings


class ModelProviderConfigTests(unittest.TestCase):
    def test_plaintext_provider_config_round_trips_two_independent_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "model-providers.json"
            settings = {
                "corrector": {
                    "provider": "openai",
                    "base_url": "https://relay.example.com/v1",
                    "model": "model-a",
                    "api_key": "corrector-secret",
                    "verified_at": "2026-08-12T15:00:00+08:00",
                    "context_window": 128_000,
                },
                "verifier": {
                    "provider": "openai",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "api_key": "verifier-secret",
                    "verified_at": "",
                    "context_window": 128_000,
                },
            }
            save_provider_settings(path, settings)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["corrector"]["api_key"], "corrector-secret")
            self.assertEqual(raw["verifier"]["api_key"], "verifier-secret")
            self.assertEqual(load_provider_settings(path), settings)


if __name__ == "__main__":
    unittest.main()
