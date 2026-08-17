import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_config
import model_manager


class AppConfigTests(unittest.TestCase):
    def test_console_output_survives_legacy_windows_encoding(self):
        class LegacyStream:
            encoding = "cp1252"

            def __init__(self):
                self.parts = []

            def write(self, value):
                safe = value.encode(self.encoding, errors="strict").decode(self.encoding)
                self.parts.append(safe)
                return len(value)

            def flush(self):
                return None

        stream = LegacyStream()
        with patch.object(model_manager.sys, "stdout", stream):
            model_manager.print_console("正在下载模型…")
        self.assertTrue(stream.parts)

    def test_config_uses_user_state_directory_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            with patch.dict(os.environ, {"LOCALTRANSCRIBER_STATE_DIR": str(root)}, clear=False):
                default = app_config.default_config()
                self.assertEqual(Path(default["model_root"]).resolve(), (root / "models").resolve())
                default["default_model"] = "large-v3-turbo"
                saved = app_config.save_config(default, config_path)
                loaded = app_config.load_config(saved)
                self.assertEqual(loaded["default_model"], "large-v3-turbo")
                self.assertEqual(Path(loaded["model_root"]).resolve(), (root / "models").resolve())

    def test_invalid_values_fall_back_to_safe_defaults(self):
        config = app_config.normalize_config(
            {"default_model": "unknown", "default_device": "dangerous"}
        )
        self.assertEqual(config["default_model"], "medium")
        self.assertEqual(config["default_device"], "auto")

    def test_installed_models_requires_model_and_config_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = app_config.normalize_config({"model_root": str(root)})
            medium = root / "medium"
            medium.mkdir()
            (medium / "model.bin").write_bytes(b"model")
            self.assertEqual(app_config.installed_models(config), [])
            (medium / "config.json").write_text(json.dumps({}), encoding="utf-8")
            self.assertEqual(app_config.installed_models(config), ["medium"])

    def test_model_install_downloads_to_user_directory_and_saves_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = app_config.normalize_config(
                {
                    "model_root": str(root / "models"),
                    "hf_cache_dir": str(root / "cache"),
                }
            )
            destination = root / "models" / "large-v3-turbo"
            with patch.object(
                model_manager, "load_config", return_value=config
            ), patch.object(model_manager, "save_config") as save_config, patch(
                "huggingface_hub.snapshot_download"
            ) as snapshot_download:
                result = model_manager.install_model("large-v3-turbo")

            self.assertEqual(result.resolve(), destination.resolve())
            snapshot_download.assert_called_once()
            self.assertEqual(
                snapshot_download.call_args.kwargs["repo_id"],
                app_config.MODEL_REPOSITORIES["large-v3-turbo"],
            )
            self.assertEqual(
                Path(snapshot_download.call_args.kwargs["local_dir"]).resolve(),
                destination.resolve(),
            )
            self.assertEqual(save_config.call_args.args[0]["default_model"], "large-v3-turbo")


if __name__ == "__main__":
    unittest.main()
