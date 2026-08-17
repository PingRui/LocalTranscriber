from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


HOTWORD_STORE_FOLDER = ".knowledge"
HOTWORD_STORE_NAME = "domain-hotwords.json"
UNKNOWN_CATEGORIES = {"", "未分类", "待分类", "unknown", "uncategorized"}
MAX_ACTIVE_HOTWORDS = 48


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def hotword_store_path(root: Path) -> Path:
    return root.expanduser().resolve() / HOTWORD_STORE_FOLDER / HOTWORD_STORE_NAME


def load_hotword_store(root: Path) -> dict[str, Any]:
    path = hotword_store_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    domains = payload.get("domains")
    if not isinstance(domains, dict):
        domains = {}
    return {"schema_version": 1, "updated_at": str(payload.get("updated_at") or ""), "domains": domains}


def save_hotword_store(root: Path, payload: dict[str, Any]) -> Path:
    path = hotword_store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = dict(payload)
    value["schema_version"] = 1
    value["updated_at"] = iso_now()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def normalize_category(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())[:80]
    return "未分类" if text.casefold() in UNKNOWN_CATEGORIES else text


def category_key(value: Any) -> str:
    return normalize_category(value).casefold()


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").casefold())


def _term_present(text: str, term: str, aliases: Iterable[str] = ()) -> bool:
    normalized = _normalized_text(text)
    values = [term, *aliases]
    return any(_normalized_text(value) in normalized for value in values if _normalized_text(value))


def _profile_records(profile: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in profile.get("hotwords", []):
        if isinstance(raw, str):
            raw = {"term": raw}
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("term") or "").strip()
        if not 2 <= len(term) <= 40:
            continue
        aliases = list(dict.fromkeys(str(item).strip() for item in raw.get("aliases", []) if str(item).strip()))[:8]
        records.append(
            {
                "term": term,
                "aliases": aliases,
                "evidence": str(raw.get("evidence") or "").strip()[:240],
            }
        )
    return records


def known_domains_for_prompt(root: Path, max_terms_per_domain: int = 30) -> list[dict[str, Any]]:
    store = load_hotword_store(root)
    result = []
    for domain in store["domains"].values():
        if not isinstance(domain, dict):
            continue
        terms = [
            {
                "term": str(item.get("term") or ""),
                "aliases": list(item.get("aliases") or [])[:4],
                "status": str(item.get("status") or "candidate"),
            }
            for item in domain.get("terms", [])
            if isinstance(item, dict) and item.get("term") and item.get("status") in {"active", "stable"}
        ][:max_terms_per_domain]
        result.append(
            {
                "name": str(domain.get("name") or "未分类"),
                "saturated": bool(domain.get("saturated")),
                "terms": terms,
            }
        )
    return result


def assess_hotword_profile(root: Path, profile: dict[str, Any]) -> dict[str, Any]:
    """Select a bounded, evidence-backed hotword set and decide whether it is safe to auto-confirm."""
    store = load_hotword_store(root)
    category = normalize_category(profile.get("category"))
    domain = store["domains"].get(category_key(category), {})
    sample_text = str(profile.get("sample_text") or "")
    try:
        confidence = max(0.0, min(float(profile.get("confidence") or 0.0), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    saturated = bool(domain.get("saturated"))

    known_records = [
        item for item in domain.get("terms", [])
        if isinstance(item, dict) and item.get("term") and item.get("status") in {"active", "stable"}
    ]
    known_records.sort(
        key=lambda item: (
            item.get("status") != "stable",
            -int(item.get("video_count") or 0),
            -int(item.get("correct_occurrences") or 0),
            str(item.get("term") or ""),
        )
    )
    relevant_known = [
        item for item in known_records
        if _term_present(sample_text, str(item.get("term") or ""), item.get("aliases") or [])
    ]
    if saturated and not relevant_known:
        relevant_known = known_records[:18]

    candidates = _profile_records(profile)
    credible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    known_names = {str(item.get("term") or "").casefold() for item in known_records}
    for record in candidates:
        has_occurrence = _term_present(sample_text, record["term"], record["aliases"])
        has_evidence = len(record["evidence"]) >= 2
        if record["term"].casefold() in known_names:
            continue
        if has_occurrence and has_evidence:
            credible.append(record)
        else:
            rejected.append({**record, "reason": "样本中缺少可核对的词形或依据"})

    selected_records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in [*relevant_known, *credible]:
        term = str(record.get("term") or "").strip()
        if not term or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        selected_records.append(
            {
                "term": term,
                "aliases": list(record.get("aliases") or [])[:8],
                "source": "domain" if record in relevant_known else "sample",
            }
        )
        if len(selected_records) >= MAX_ACTIVE_HOTWORDS:
            break

    reasons = []
    if candidates and len(rejected) / len(candidates) > 0.5 and not saturated:
        reasons.append("多数候选词缺少样本依据")

    return {
        "category": category,
        "confidence": round(confidence, 3),
        "saturated_domain": saturated,
        "analysis_mode": "incremental" if saturated else "discovery",
        "auto_confirmed": not reasons,
        "manual_reasons": reasons,
        "selected": selected_records,
        "selected_terms": [item["term"] for item in selected_records],
        "new_candidates": credible,
        "rejected_candidates": rejected,
    }


def _count_occurrences(text: str, value: str) -> int:
    needle = _normalized_text(value)
    return _normalized_text(text).count(needle) if needle else 0


def _applied_corrections(verified: dict[str, Any]) -> list[dict[str, str]]:
    values = []
    for segment in verified.get("segments", []):
        if not isinstance(segment, dict):
            continue
        for item in segment.get("corrections", []):
            if not isinstance(item, dict) or str(item.get("status") or "") != "applied":
                continue
            values.append(
                {
                    "original": str(item.get("original_span") or "").strip(),
                    "replacement": str(item.get("replacement") or "").strip(),
                }
            )
    return values


def learn_from_verified(
    root: Path,
    video_id: str,
    profile: dict[str, Any],
    assessment: dict[str, Any],
    verified_path: Path,
) -> dict[str, Any]:
    """Use only locally accepted corrections to promote, enrich, or demote domain hotwords."""
    verified = json.loads(verified_path.read_text(encoding="utf-8-sig"))
    segments = [item for item in verified.get("segments", []) if isinstance(item, dict)]
    raw_text = " ".join(str(item.get("raw_text") or "") for item in segments)
    final_text = " ".join(str(item.get("final_text") or "") for item in segments)
    corrections = _applied_corrections(verified)
    category = normalize_category(assessment.get("category") or profile.get("category"))
    if category == "未分类":
        return {
            "category": category,
            "saturated": False,
            "videos_reviewed": 0,
            "term_occurrences": 0,
            "professional_corrections": len(_applied_corrections(verified)),
            "correction_rate": 0.0,
            "new_terms": 0,
            "skipped": True,
        }
    key = category_key(category)
    store = load_hotword_store(root)
    domain = store["domains"].setdefault(
        key,
        {
            "name": category,
            "status": "learning",
            "saturated": False,
            "videos_reviewed": 0,
            "terms": [],
            "history": [],
        },
    )
    terms = {str(item.get("term") or "").casefold(): item for item in domain.get("terms", []) if isinstance(item, dict)}
    profile_records = {item["term"].casefold(): item for item in _profile_records(profile)}
    selected = list(assessment.get("selected") or [])
    new_term_count = 0
    professional_corrections = 0
    term_occurrences = 0

    for selected_item in selected:
        term = str(selected_item.get("term") or "").strip()
        if not term:
            continue
        lookup = term.casefold()
        source_record = profile_records.get(lookup, {})
        record = terms.get(lookup)
        if record is None:
            record = {
                "term": term,
                "aliases": list(source_record.get("aliases") or selected_item.get("aliases") or [])[:8],
                "status": "candidate",
                "video_count": 0,
                "correct_occurrences": 0,
                "corrected_to": 0,
                "corrected_away": 0,
                "last_seen_at": "",
            }
            domain.setdefault("terms", []).append(record)
            terms[lookup] = record
            new_term_count += 1
        aliases = list(dict.fromkeys(str(item).strip() for item in record.get("aliases", []) if str(item).strip()))
        raw_hits = _count_occurrences(raw_text, term) + sum(_count_occurrences(raw_text, alias) for alias in aliases)
        final_hits = _count_occurrences(final_text, term)
        corrected_to = [item for item in corrections if item["replacement"].casefold() == lookup]
        corrected_away = [item for item in corrections if item["original"].casefold() == lookup and item["replacement"].casefold() != lookup]
        if raw_hits or final_hits or corrected_to or corrected_away:
            evidence_videos = set(str(item) for item in record.get("evidence_videos", []) if str(item))
            already_learned = video_id in evidence_videos
            evidence_videos.add(video_id)
            record["evidence_videos"] = sorted(evidence_videos)[-30:]
            record["video_count"] = len(evidence_videos)
            if not already_learned:
                record["correct_occurrences"] = int(record.get("correct_occurrences") or 0) + final_hits
                record["corrected_to"] = int(record.get("corrected_to") or 0) + len(corrected_to)
                record["corrected_away"] = int(record.get("corrected_away") or 0) + len(corrected_away)
            record["last_seen_at"] = iso_now()
            for correction in corrected_to:
                alias = correction["original"]
                if 2 <= len(alias) <= 40 and alias.casefold() != lookup and alias not in aliases:
                    aliases.append(alias)
            record["aliases"] = aliases[:8]
            term_occurrences += final_hits
            professional_corrections += len(corrected_to) + len(corrected_away)

        if int(record.get("corrected_away") or 0) >= 2 and int(record.get("corrected_away") or 0) > int(record.get("corrected_to") or 0):
            record["status"] = "disabled"
        elif int(record.get("video_count") or 0) >= 3 and int(record.get("correct_occurrences") or 0) >= 10 and int(record.get("corrected_away") or 0) == 0:
            record["status"] = "stable"
        elif int(record.get("video_count") or 0) >= 2 or int(record.get("corrected_to") or 0) >= 1:
            record["status"] = "active"

    correction_rate = professional_corrections / max(1, term_occurrences + professional_corrections)
    history = [item for item in domain.get("history", []) if isinstance(item, dict) and item.get("video_id") != video_id]
    history.append(
        {
            "video_id": video_id,
            "reviewed_at": iso_now(),
            "term_occurrences": term_occurrences,
            "professional_corrections": professional_corrections,
            "correction_rate": round(correction_rate, 4),
            "new_terms": new_term_count,
        }
    )
    history = history[-20:]
    domain["history"] = history
    domain["videos_reviewed"] = len({str(item.get("video_id")) for item in history})
    recent = history[-3:]
    recent_occurrences = sum(int(item.get("term_occurrences") or 0) for item in recent)
    recent_errors = sum(int(item.get("professional_corrections") or 0) for item in recent)
    recent_new = sum(int(item.get("new_terms") or 0) for item in recent)
    domain["saturated"] = bool(
        len(recent) >= 3
        and recent_occurrences >= 10
        and recent_errors / max(1, recent_occurrences + recent_errors) <= 0.05
        and recent_new <= 1
    )
    domain["status"] = "stable" if domain["saturated"] else "learning"
    domain["updated_at"] = iso_now()
    save_hotword_store(root, store)
    return {
        "category": category,
        "saturated": domain["saturated"],
        "videos_reviewed": domain["videos_reviewed"],
        "term_occurrences": term_occurrences,
        "professional_corrections": professional_corrections,
        "correction_rate": round(correction_rate, 4),
        "new_terms": new_term_count,
    }
