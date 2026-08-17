from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
TAG_SPLIT = re.compile(r"\s*(?:[/|、,，;；>]+)\s*")


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_tags(value: object, category: str = "") -> list[str]:
    raw_values = value if isinstance(value, list) else []
    if not raw_values and category:
        raw_values = TAG_SPLIT.split(category)
    tags: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        tag = str(raw).strip()[:24]
        if not tag or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        tags.append(tag)
        if len(tags) >= 8:
            break
    return tags or ([category[:24]] if category else ["未分类"])


def normalize_words(raw_words: object) -> list[dict[str, Any]]:
    if not isinstance(raw_words, list):
        return []
    words: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_words:
        if isinstance(raw, str):
            term, aliases, evidence = raw, [], ""
        elif isinstance(raw, dict):
            term = str(raw.get("term") or "")
            aliases = raw.get("aliases") if isinstance(raw.get("aliases"), list) else []
            evidence = str(raw.get("evidence") or "")
        else:
            continue
        term = term.strip()
        if not 2 <= len(term) <= 40 or term.casefold() in seen or any(char in term for char in "。！？\n"):
            continue
        seen.add(term.casefold())
        alias_values: list[str] = []
        alias_seen: set[str] = set()
        for alias in aliases:
            value = str(alias).strip()
            if not value or value.casefold() in alias_seen or value.casefold() == term.casefold():
                continue
            alias_seen.add(value.casefold())
            alias_values.append(value[:40])
            if len(alias_values) >= 8:
                break
        words.append({"term": term, "aliases": alias_values, "evidence": evidence.strip()[:160]})
        if len(words) >= 120:
            break
    return words


def _path_for(root: Path, set_id: str) -> Path:
    identifier = str(set_id).strip()
    if not identifier or not SAFE_ID.fullmatch(identifier):
        raise ValueError("热词集标识无效")
    filename = identifier if identifier.lower().endswith(".json") else f"{identifier}.json"
    path = (root.expanduser().resolve() / filename).resolve()
    if path.parent != root.expanduser().resolve():
        raise ValueError("热词集标识无效")
    return path


def _normalize(path: Path, payload: object) -> dict[str, Any] | None:
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ready"
        or payload.get("library_visible") is False
    ):
        return None
    words = normalize_words(payload.get("hotwords"))
    if not words:
        return None
    identifier = path.stem
    category = str(payload.get("category") or "未分类").strip() or "未分类"
    tags = normalize_tags(payload.get("tags"), category)
    api = dict(payload.get("api") or {})
    return {
        "id": identifier,
        "name": str(payload.get("name") or category).strip() or category,
        "category": category,
        "tags": tags,
        "count": len(words),
        "hotwords": words,
        "preview": [item["term"] for item in words[:8]],
        "sources": [str(item) for item in payload.get("sources", []) if str(item).strip()],
        "created_at": str(payload.get("created_at") or payload.get("updated_at") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
        "last_used_at": str(payload.get("last_used_at") or ""),
        "use_count": int(payload.get("use_count") or 0),
        "source_type": str(payload.get("source_type") or ("自动生成" if api else "手动")),
        "api": api,
        "file": str(path.resolve()),
    }


def load_hotword_set(root: Path, set_id: str) -> dict[str, Any]:
    path = _path_for(root, set_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError("所选热词集不存在或已被删除") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("热词集文件无法读取") from exc
    normalized = _normalize(path, payload)
    if normalized is None:
        raise ValueError("热词集尚未生成完成或没有有效热词")
    return normalized


def list_hotword_sets(root: Path) -> list[dict[str, Any]]:
    folder = root.expanduser().resolve()
    if not folder.is_dir():
        return []
    values: list[dict[str, Any]] = []
    for path in folder.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        item = _normalize(path, payload)
        if item is not None:
            values.append(item)
    values.sort(
        key=lambda item: str(item.get("last_used_at") or item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return values


def _update(root: Path, set_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    path = _path_for(root, set_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError("所选热词集不存在或已被删除") from exc
    if not isinstance(payload, dict):
        raise ValueError("热词集文件格式无效")
    payload.update(changes)
    payload["updated_at"] = iso_now()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return load_hotword_set(root, set_id)


def rename_hotword_set(root: Path, set_id: str, name: str) -> dict[str, Any]:
    value = str(name).strip()
    if not 1 <= len(value) <= 80:
        raise ValueError("热词集名称需要保持在 1 到 80 个字符之间")
    return _update(root, set_id, {"name": value})


def update_hotword_set(root: Path, set_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    current = load_hotword_set(root, set_id)
    normalized: dict[str, Any] = {}
    if "name" in changes:
        name = str(changes.get("name") or "").strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("热词集名称需要保持在 1 到 80 个字符之间")
        normalized["name"] = name
    if "tags" in changes:
        normalized["tags"] = normalize_tags(changes.get("tags"), current["category"])
    if "hotwords" in changes:
        incoming = changes.get("hotwords")
        if isinstance(incoming, str):
            incoming = [line.strip() for line in incoming.splitlines() if line.strip()]
        words = normalize_words(incoming)
        if not words:
            raise ValueError("热词集至少需要保留一个有效热词")
        previous = {item["term"].casefold(): item for item in current["hotwords"]}
        for item in words:
            old = previous.get(item["term"].casefold())
            if old and not item["aliases"] and not item["evidence"]:
                item["aliases"] = list(old.get("aliases", []))
                item["evidence"] = str(old.get("evidence") or "")
        normalized["hotwords"] = words
    if not normalized:
        return current
    return _update(root, set_id, normalized)


def touch_hotword_set(root: Path, set_id: str) -> dict[str, Any]:
    current = load_hotword_set(root, set_id)
    return _update(
        root,
        set_id,
        {"last_used_at": iso_now(), "use_count": int(current.get("use_count") or 0) + 1},
    )


def combine_hotword_sets(root: Path, set_ids: list[str], output_path: Path) -> dict[str, Any]:
    identifiers = list(dict.fromkeys(str(item).strip() for item in set_ids if str(item).strip()))
    if not identifiers:
        raise ValueError("请至少选择一个热词集")
    selected = [touch_hotword_set(root, set_id) for set_id in identifiers]
    words: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for item in selected:
        for raw in item["hotwords"]:
            key = raw["term"].casefold()
            if key not in seen:
                copied = {
                    "term": raw["term"],
                    "aliases": list(raw.get("aliases", [])),
                    "evidence": str(raw.get("evidence") or ""),
                }
                seen[key] = copied
                words.append(copied)
                continue
            existing = seen[key]
            existing["aliases"] = list(
                dict.fromkeys([*existing.get("aliases", []), *raw.get("aliases", [])])
            )[:8]
    categories = list(dict.fromkeys(item["category"] for item in selected if item.get("category")))
    tags = list(dict.fromkeys(tag for item in selected for tag in item.get("tags", [])))[:8]
    now = iso_now()
    payload = {
        "schema_version": 2,
        "status": "ready",
        "library_visible": False,
        "source_type": "组合复用",
        "name": f"已选 {len(selected)} 组热词",
        "category": " / ".join(categories[:3]) or "组合热词",
        "tags": tags,
        "hotwords": words,
        "source_set_ids": identifiers,
        "created_at": now,
        "updated_at": now,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return {**payload, "count": len(words), "file": str(output_path.resolve())}


def apply_hotword_suggestions(
    root: Path,
    set_id: str,
    suggestions: list[dict[str, Any]],
) -> dict[str, Any]:
    current = load_hotword_set(root, set_id)
    words = [dict(item) for item in current["hotwords"]]
    by_term = {item["term"].casefold(): item for item in words}
    tags = list(current.get("tags", []))
    applied = 0
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        kind = str(suggestion.get("type") or "new").strip().lower()
        term = str(suggestion.get("term") or suggestion.get("target") or "").strip()
        alias = str(suggestion.get("alias") or "").strip()
        target = str(suggestion.get("target") or term).strip()
        if kind == "category":
            before = len(tags)
            tags = normalize_tags([*tags, *suggestion.get("tags", [])], current["category"])
            applied += int(len(tags) > before)
            continue
        if not 2 <= len(target) <= 40:
            continue
        target_item = by_term.get(target.casefold())
        if target_item is None:
            target_item = {
                "term": target,
                "aliases": [],
                "evidence": str(suggestion.get("reason") or "全量校对发现")[:160],
            }
            words.append(target_item)
            by_term[target.casefold()] = target_item
            applied += 1
        if alias and alias.casefold() != target.casefold() and alias not in target_item["aliases"]:
            target_item["aliases"] = [*target_item["aliases"], alias[:40]][:8]
            applied += 1
        if kind == "merge" and term and term.casefold() != target.casefold():
            duplicate = by_term.get(term.casefold())
            if duplicate and duplicate is not target_item:
                target_item["aliases"] = list(
                    dict.fromkeys([*target_item["aliases"], term, *duplicate.get("aliases", [])])
                )[:8]
                words.remove(duplicate)
                by_term.pop(term.casefold(), None)
                applied += 1
    if not applied:
        return current
    return _update(root, set_id, {"hotwords": words, "tags": tags})


def delete_hotword_set(root: Path, set_id: str) -> None:
    path = _path_for(root, set_id)
    if not path.is_file():
        raise FileNotFoundError("所选热词集不存在或已被删除")
    path.unlink()
