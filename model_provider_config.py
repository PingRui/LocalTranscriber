from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_provider_settings(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for role in ("corrector", "verifier"):
        raw = payload.get(role)
        if not isinstance(raw, dict):
            continue
        result[role] = {
            "provider": str(raw.get("provider") or "local"),
            "base_url": str(raw.get("base_url") or ""),
            "model": str(raw.get("model") or ""),
            "api_key": str(raw.get("api_key") or ""),
            "verified_at": str(raw.get("verified_at") or ""),
            "context_window": int(raw.get("context_window") or 128_000),
        }
    return result


def save_provider_settings(path: Path, settings: dict[str, dict[str, Any]]) -> None:
    payload: dict[str, Any] = {"version": 1}
    for role in ("corrector", "verifier"):
        raw = settings.get(role) or {}
        payload[role] = {
            "provider": str(raw.get("provider") or "local"),
            "base_url": str(raw.get("base_url") or ""),
            "model": str(raw.get("model") or ""),
            "api_key": str(raw.get("api_key") or ""),
            "verified_at": str(raw.get("verified_at") or ""),
            "context_window": int(raw.get("context_window") or 128_000),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
