from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from hotword_suggestions import build_hotword_suggestions


SUGGESTIONS_FILE_NAME = "trusted-hotword-suggestions.json"
UNCLASSIFIED = {"", "未分类", "待分类", "unknown"}


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _category(payload: dict[str, Any], source: str) -> str:
    domain = payload.get("domain") if isinstance(payload.get("domain"), dict) else {}
    value = str(domain.get("name") or "").strip()
    if value.casefold() not in UNCLASSIFIED:
        return value
    parent = Path(source).parent.name.strip()
    parent = re.sub(r"^\d+[.、_\-\s]*", "", parent).strip()
    return parent or "待分类"


def _correction_id(path: Path, index: int, item: dict[str, Any]) -> str:
    raw = "|".join(
        (
            path.name,
            str(index),
            str(item.get("segment_id") or ""),
            str(item.get("original_span") or item.get("original") or ""),
            str(item.get("replacement") or item.get("corrected") or ""),
        )
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def load_review_corrections(output_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = output_root.expanduser().resolve()
    if not root.is_dir():
        return [], []
    files: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for log_path in sorted(root.glob("*.corrections.json"), key=lambda value: value.name.casefold()):
        log = _read_json(log_path)
        verified_path = log_path.with_name(log_path.name.replace(".corrections.json", ".verified.json"))
        verified = _read_json(verified_path)
        source = str(log.get("source") or verified.get("source") or "")
        segments = {
            str(item.get("id")): item
            for item in verified.get("segments", [])
            if isinstance(item, dict)
        }
        stats = log.get("stats") if isinstance(log.get("stats"), dict) else {}
        file_record = {
            "id": hashlib.sha1(str(log_path).casefold().encode("utf-8")).hexdigest()[:12],
            "name": Path(source).name or log_path.name.replace(".corrections.json", ""),
            "source_video": source,
            "corrections_file": str(log_path),
            "verified_file": str(verified_path) if verified_path.is_file() else "",
            "category": _category(verified, source),
            "stats": {
                "accepted": int(stats.get("accepted") or 0),
                "rejected": int(stats.get("rejected") or 0),
                "uncertain": int(stats.get("uncertain") or 0),
                "proposed": int(stats.get("proposed") or 0),
            },
        }
        files.append(file_record)
        for index, raw in enumerate(log.get("corrections", [])):
            if not isinstance(raw, dict):
                continue
            segment_id = str(raw.get("segment_id") or raw.get("id") or "")
            segment = segments.get(segment_id, {})
            status = str(raw.get("status") or ("applied" if raw.get("accepted") else "pending")).strip().lower()
            corrections.append(
                {
                    "id": _correction_id(log_path, index, raw),
                    "file_id": file_record["id"],
                    "file_name": file_record["name"],
                    "source_video": source,
                    "category": file_record["category"],
                    "segment_id": segment_id,
                    "start_seconds": float(segment.get("start") or 0.0),
                    "end_seconds": float(segment.get("end") or segment.get("start") or 0.0),
                    "original": str(raw.get("original_span") or raw.get("original") or "").strip(),
                    "corrected": str(raw.get("replacement") or raw.get("corrected") or "").strip(),
                    "reason": str(raw.get("reason") or "").strip(),
                    "confidence": str(raw.get("confidence") or "").strip(),
                    "status": status,
                    "status_reason": str(raw.get("status_reason") or "").strip(),
                    "raw_text": str(segment.get("raw_text") or "").strip(),
                    "final_text": str(segment.get("final_text") or "").strip(),
                }
            )
    return files, corrections


def _library_index(hotword_sets: Iterable[dict[str, Any]]) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    terms: dict[str, list[dict[str, str]]] = {}
    aliases: dict[str, list[dict[str, str]]] = {}
    for hotword_set in hotword_sets:
        if not isinstance(hotword_set, dict):
            continue
        set_id = str(hotword_set.get("id") or "")
        set_name = str(hotword_set.get("name") or "未命名热词集")
        for word in hotword_set.get("hotwords", []):
            if not isinstance(word, dict):
                continue
            term = str(word.get("term") or "").strip()
            if not term:
                continue
            match = {"set_id": set_id, "set_name": set_name, "term": term}
            terms.setdefault(term.casefold(), []).append(match)
            for alias in word.get("aliases", []):
                value = str(alias).strip()
                if value:
                    aliases.setdefault(value.casefold(), []).append(match)
    return terms, aliases


def build_hotword_comparison(
    corrections: Iterable[dict[str, Any]],
    hotword_sets: Iterable[dict[str, Any]],
    previous: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    applied = [item for item in corrections if str(item.get("status") or "") == "applied"]
    category_counts: dict[str, int] = {}
    for item in applied:
        category = str(item.get("category") or "待分类")
        category_counts[category] = category_counts.get(category, 0) + 1
    category = max(category_counts, key=category_counts.get) if category_counts else "待分类"
    hotword_sets = list(hotword_sets)
    terms, aliases = _library_index(hotword_sets)
    existing_terms = [match["term"] for matches in terms.values() for match in matches]
    suggestions = build_hotword_suggestions(applied, category=category, existing_terms=existing_terms)
    previous_status = {
        str(item.get("id")): str(item.get("status"))
        for item in (previous or {}).get("suggestions", [])
        if isinstance(item, dict)
    }
    for suggestion in suggestions:
        target = str(suggestion.get("target") or suggestion.get("term") or "").strip()
        alias = str(suggestion.get("alias") or "").strip()
        term_matches = terms.get(target.casefold(), [])
        alias_matches = aliases.get(alias.casefold(), []) if alias else []
        target_alias_matches = aliases.get(target.casefold(), [])
        matches = [*term_matches, *alias_matches, *target_alias_matches]
        unique_matches = list({(item["set_id"], item["term"]): item for item in matches}.values())
        suggestion["matches"] = unique_matches
        if term_matches and (not alias or any(item["term"].casefold() == target.casefold() for item in alias_matches)):
            suggestion["comparison"] = "synced"
            suggestion["action"] = "无需修改"
            suggestion["status"] = "synced"
        elif term_matches:
            suggestion["comparison"] = "add_alias"
            suggestion["action"] = "补充别名"
        elif target_alias_matches or alias_matches:
            suggestion["comparison"] = "possible_overlap"
            suggestion["action"] = "可能重复，需复核"
        else:
            suggestion["comparison"] = "new"
            suggestion["action"] = "新增热词"
        if previous_status.get(str(suggestion.get("id"))) == "applied":
            suggestion["status"] = "applied"
    return suggestions


def load_review_results(output_root: Path, hotword_sets: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    root = output_root.expanduser().resolve()
    files, corrections = load_review_corrections(root)
    suggestions_path = root / SUGGESTIONS_FILE_NAME
    previous = _read_json(suggestions_path)
    suggestions = build_hotword_comparison(corrections, hotword_sets, previous)
    summary = {
        "files": len(files),
        "applied": sum(item["status"] == "applied" for item in corrections),
        "pending": sum(item["status"] in {"pending", "rejected"} for item in corrections),
        "suggestions": sum(item.get("status") == "pending" for item in suggestions),
        "synced": sum(item.get("status") in {"synced", "applied"} for item in suggestions),
    }
    payload = {
        "schema_version": 1,
        "updated_at": _iso_now(),
        "summary": summary,
        "suggestions": suggestions,
    }
    if root.is_dir() and (suggestions or suggestions_path.is_file()):
        temporary = suggestions_path.with_suffix(suggestions_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(suggestions_path)
    return {
        "files": files,
        "corrections": corrections,
        "suggestions": suggestions,
        "summary": summary,
        "suggestions_file": str(suggestions_path) if suggestions_path.is_file() else "",
    }


def mark_suggestions_applied(output_root: Path, suggestion_ids: Iterable[str]) -> None:
    path = output_root.expanduser().resolve() / SUGGESTIONS_FILE_NAME
    payload = _read_json(path)
    selected = {str(value) for value in suggestion_ids}
    if not selected or not payload:
        return
    for item in payload.get("suggestions", []):
        if isinstance(item, dict) and str(item.get("id")) in selected:
            item["status"] = "applied"
    payload["updated_at"] = _iso_now()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
