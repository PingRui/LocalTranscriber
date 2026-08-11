from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


APP_NAME = "LocalTranscriber"
CONFIG_SCHEMA_VERSION = 1
SUPPORTED_MODELS = ("medium", "large-v3-turbo")
MODEL_LABELS = {
    "medium": "Medium",
    "large-v3-turbo": "Large-v3 Turbo",
}
MODEL_REPOSITORIES = {
    "medium": "Systran/faster-whisper-medium",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def state_dir() -> Path:
    override = os.environ.get("LOCALTRANSCRIBER_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).expanduser().resolve() / APP_NAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).expanduser().resolve() / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def config_file() -> Path:
    return state_dir() / "config.json"


def default_config() -> dict[str, Any]:
    root = state_dir()
    model_root_override = os.environ.get("LOCALTRANSCRIBER_MODEL_DIR", "").strip()
    model_root = Path(model_root_override).expanduser().resolve() if model_root_override else root / "models"
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "default_model": "medium",
        "default_device": "auto",
        "model_root": str(model_root),
        "hf_cache_dir": str(root / "huggingface"),
    }


def normalize_config(payload: object) -> dict[str, Any]:
    config = default_config()
    if isinstance(payload, dict):
        model = str(payload.get("default_model", "")).strip().lower()
        device = str(payload.get("default_device", "")).strip().lower()
        model_root = str(payload.get("model_root", "")).strip()
        hf_cache_dir = str(payload.get("hf_cache_dir", "")).strip()
        if model in SUPPORTED_MODELS:
            config["default_model"] = model
        if device in {"auto", "cuda", "cpu"}:
            config["default_device"] = device
        if model_root:
            config["model_root"] = str(Path(model_root).expanduser().resolve())
        if hf_cache_dir:
            config["hf_cache_dir"] = str(Path(hf_cache_dir).expanduser().resolve())
    return config


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_file()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        payload = {}
    return normalize_config(payload)


def save_config(config: dict[str, Any], path: Path | None = None) -> Path:
    target = path or config_file()
    normalized = normalize_config(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def model_paths(config: dict[str, Any] | None = None) -> dict[str, Path]:
    current = normalize_config(config) if config is not None else load_config()
    root = Path(str(current["model_root"]))
    return {name: root / name for name in SUPPORTED_MODELS}


def installed_models(config: dict[str, Any] | None = None) -> list[str]:
    return [
        name
        for name, path in model_paths(config).items()
        if (path / "model.bin").is_file() and (path / "config.json").is_file()
    ]
