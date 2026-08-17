from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any, Iterable


PUNCTUATION = re.compile(r"[，。！？；：、,.!?;:()（）\[\]【】/\\\n\r]")


def _compact_change(original: str, corrected: str) -> tuple[str, str]:
    old_words = original.split()
    new_words = corrected.split()
    if len(old_words) > 1 or len(new_words) > 1:
        word_matcher = SequenceMatcher(None, old_words, new_words)
        old_changed: list[str] = []
        new_changed: list[str] = []
        for tag, i1, i2, j1, j2 in word_matcher.get_opcodes():
            if tag == "equal":
                continue
            old_changed.extend(old_words[i1:i2])
            new_changed.extend(new_words[j1:j2])
        old_value, new_value = " ".join(old_changed).strip(".,!?;:，。！？；："), " ".join(new_changed).strip(".,!?;:，。！？；：")
        if old_value and new_value:
            return old_value, new_value
    matcher = SequenceMatcher(None, original, corrected)
    old_parts: list[str] = []
    new_parts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_parts.append(original[i1:i2])
        new_parts.append(corrected[j1:j2])
    return "".join(old_parts).strip(), "".join(new_parts).strip()


def _valid_term(value: str) -> bool:
    if not 2 <= len(value) <= 40 or PUNCTUATION.search(value) or value.isspace():
        return False
    if len(value) <= 3 and value[-1:] in {"的", "了", "着", "地", "得", "呢", "吗", "吧"}:
        return False
    midpoint = len(value) // 2
    if len(value) >= 4 and len(value) % 2 == 0 and value[:midpoint] == value[midpoint:]:
        return False
    return True


def build_hotword_suggestions(
    corrections: Iterable[dict[str, Any]],
    category: str = "",
    existing_terms: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Turn accepted proofreading changes into conservative, user-reviewable suggestions."""
    existing = {str(value).strip().casefold() for value in existing_terms if str(value).strip()}
    suggestions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    tags = [category.strip()] if category.strip() else ["待分类"]
    for correction in corrections:
        if not isinstance(correction, dict) or correction.get("accepted") is False:
            continue
        status = str(correction.get("status") or "").strip().lower()
        if status and status not in {"applied", "accepted"}:
            continue
        original = str(correction.get("original") or correction.get("original_span") or "").strip()
        corrected = str(correction.get("corrected") or correction.get("replacement") or "").strip()
        if not original or not corrected or original == corrected:
            continue
        alias, target = _compact_change(original, corrected)
        if not _valid_term(target):
            continue
        if alias == target:
            continue
        kind = "alias" if _valid_term(alias) else "new"
        if target.casefold() in existing and kind == "new":
            continue
        key = (kind, target.casefold(), alias.casefold())
        if key in seen:
            continue
        seen.add(key)
        identifier = hashlib.sha1("|".join(key).encode("utf-8")).hexdigest()[:12]
        suggestions.append(
            {
                "id": identifier,
                "type": kind,
                "term": target,
                "target": target,
                "alias": alias if kind == "alias" else "",
                "tags": tags,
                "reason": f"全量校对发现：{alias or original[:20]} → {target}",
                "status": "pending",
            }
        )
        if len(suggestions) >= 80:
            break
    return suggestions
