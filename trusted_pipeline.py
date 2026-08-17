from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from llm_client import DEFAULT_LOCAL_BASE_URL, DEFAULT_LOCAL_MODEL, OpenAICompatibleClient
from llm_repair import DEFAULT_MODEL as DEFAULT_DEEPSEEK_MODEL
from llm_repair import DeepSeekClient


EVENT_PREFIX = "@@LOCAL_TRANSCRIBER_EVENT@@"
PROFILE_NAME = "task-domain.json"
OUTPUT_FOLDER_NAME = "可信数据结果"
EXCLUDED_SUFFIXES = (
    ".llm.json",
    ".llm-corrections.json",
    ".source-context.json",
    ".verified.json",
    ".corrections.json",
)
NEGATIONS = {"不", "没", "没有", "无", "未", "非", "否", "不是", "不能", "不会"}
NUMBER_PATTERN = re.compile(r"(?:\$|¥|￥)?\d+(?:[.,]\d+)*(?:%|万|亿|k|m|b)?", re.IGNORECASE)


class JsonClient(Protocol):
    model: str

    def complete_json(
        self,
        system_prompt: str,
        user_payload: object,
        max_tokens: int = 4096,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...


@dataclass
class ProviderConfig:
    provider: str = "local"
    model: str = DEFAULT_LOCAL_MODEL
    base_url: str = DEFAULT_LOCAL_BASE_URL
    api_key: str = ""


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emit_event(enabled: bool, event_type: str, **payload: Any) -> None:
    if enabled:
        print(EVENT_PREFIX + json.dumps({"event": event_type, **payload}, ensure_ascii=False), flush=True)


def create_client(config: ProviderConfig) -> JsonClient:
    provider = config.provider.strip().lower()
    if provider == "local":
        client = OpenAICompatibleClient(base_url=config.base_url, model=config.model or DEFAULT_LOCAL_MODEL)
        client.ensure_model()
        return client
    if provider in {"openai", "compatible"}:
        return OpenAICompatibleClient(
            base_url=config.base_url,
            model=config.model or DEFAULT_LOCAL_MODEL,
            api_key=config.api_key,
            allow_remote=True,
        )
    if provider == "deepseek":
        if not config.api_key.strip():
            raise RuntimeError("已选择 DeepSeek，但没有提供 API Key")
        return DeepSeekClient(api_key=config.api_key.strip(), model=config.model or DEFAULT_DEEPSEEK_MODEL)
    raise ValueError(f"不支持的模型提供方：{config.provider}")


def load_transcript(path: Path) -> dict[str, Any] | None:
    if path.name.casefold() == PROFILE_NAME or path.name.lower().endswith(EXCLUDED_SUFFIXES):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    source = str(payload.get("source") or "").strip()
    segments = payload.get("segments")
    if not source or not isinstance(segments, list) or not segments:
        return None
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(segments):
        if not isinstance(item, dict):
            return None
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
        except (TypeError, ValueError):
            return None
        if start < 0 or end < start:
            return None
        normalized.append(
            {
                "id": str(item.get("id", index)),
                "start": start,
                "end": end,
                "text": text,
                "review_reasons": [str(value) for value in item.get("review_reasons", [])],
            }
        )
    if not normalized:
        return None
    return {"path": path.resolve(), "source": source, "segments": normalized, "metadata": payload}


def verified_output_path(raw_path: Path, source_root: Path, output_root: Path) -> Path:
    relative = str(raw_path.resolve().relative_to(source_root.resolve())).casefold()
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:10]
    return output_root / f"{digest}-{raw_path.stem}.verified.json"


def estimate_transcript_tokens(segments: list[dict[str, Any]]) -> int:
    text = "\n".join(str(item.get("text") or "") for item in segments)
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    other = max(0, len(text) - cjk)
    return max(1, round(cjk * 1.2 + other / 4 + len(segments) * 10 + 800))


def discover_transcripts(source_root: Path, output_root: Path | None = None) -> list[dict[str, Any]]:
    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"转写目录不存在：{root}")
    target = (output_root or root / OUTPUT_FOLDER_NAME).expanduser().resolve()
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json"), key=lambda value: str(value).casefold()):
        try:
            if path.resolve().is_relative_to(target):
                continue
        except ValueError:
            pass
        transcript = load_transcript(path)
        if transcript is None:
            continue
        output = verified_output_path(path, root, target)
        correction_log = output.with_name(output.name.replace(".verified.json", ".corrections.json"))
        complete = output.is_file() and correction_log.is_file()
        records.append(
            {
                "path": str(path.resolve()),
                "name": path.name,
                "source": transcript["source"],
                "segment_count": len(transcript["segments"]),
                "estimated_tokens": estimate_transcript_tokens(transcript["segments"]),
                "output": str(output),
                "outputs": [str(output), str(correction_log)] if complete else [],
                "complete": complete,
                "status": "整文件校对完成" if complete else "等待校对",
                "progress": 100.0 if complete else 0.0,
            }
        )
    return records


def sample_text(records: list[dict[str, Any]], max_files: int = 6, max_chars: int = 2800) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    if not records:
        return samples
    selected: list[dict[str, Any]] = []
    count = min(max_files, len(records))
    for offset in range(count):
        index = round(offset * (len(records) - 1) / max(count - 1, 1))
        record = records[index]
        if record not in selected:
            selected.append(record)
    per_file = max(420, max_chars // max(len(selected), 1))
    for record in selected:
        transcript = load_transcript(Path(record["path"]))
        if transcript is None:
            continue
        segments = transcript["segments"]
        thirds = [
            segments[: max(1, len(segments) // 6)],
            segments[max(0, len(segments) // 2 - len(segments) // 12) : len(segments) // 2 + len(segments) // 12 + 1],
            segments[-max(1, len(segments) // 6) :],
        ]
        text = " ".join(item["text"] for group in thirds for item in group)
        samples.append({"file": record["name"], "text": text[:per_file]})
    return samples


def normalize_string_list(value: object, minimum: int, maximum: int, item_length: int = 40) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("模型结果缺少列表字段")
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if 1 < len(text) <= item_length and text not in result:
            result.append(text)
    if len(result) < minimum:
        raise ValueError(f"模型只返回了 {len(result)} 个有效项")
    return result[:maximum]


def detect_domain(records: list[dict[str, Any]], client: JsonClient) -> dict[str, Any]:
    samples = sample_text(records)
    if not samples:
        raise RuntimeError("没有可用于领域探测的转写样本")
    payload, usage = client.complete_json(
        """你是语音转写的领域配置分析器。根据多个转写样本判断一个主领域、内容方向，并提取能够降低 ASR 专业词误识别的热词。
热词必须是样本所属领域内可能真实出现的术语、人物、指标、药物、缩写或专业表达；不要生成口号和抽象标签。
只返回 JSON：{"domain":"主领域","topics":["方向"],"hotwords":["热词"],"summary":"一句话判断依据"}。
topics 返回 3-8 个，hotwords 返回 20-60 个。""",
        {
            "course_titles": [str(item.get("name", ""))[:80] for item in records[:120]],
            "samples": samples,
        },
        max_tokens=1400,
    )
    domain = str(payload.get("domain") or "").strip()
    if len(domain) < 2 or len(domain) > 60:
        raise ValueError("模型未返回有效的领域")
    return {
        "schema_version": 1,
        "created_at": iso_now(),
        "domain": domain,
        "topics": normalize_string_list(payload.get("topics"), 3, 8),
        "hotwords": normalize_string_list(payload.get("hotwords"), 10, 60),
        "summary": str(payload.get("summary") or "").strip(),
        "sample_files": [item["file"] for item in samples],
        "model": getattr(client, "model", ""),
        "usage": usage,
    }


def load_or_create_profile(
    records: list[dict[str, Any]],
    output_root: Path,
    client: JsonClient,
    refresh: bool = False,
) -> dict[str, Any]:
    path = output_root / PROFILE_NAME
    if path.is_file() and not refresh:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("domain") and payload.get("hotwords"):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    profile = detect_domain(records, client)
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return profile


def chunks(segments: list[dict[str, Any]], max_segments: int = 12, max_chars: int = 1800) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    start = 0
    while start < len(segments):
        end = start
        size = 0
        while end < len(segments) and end - start < max_segments:
            next_size = len(str(segments[end].get("text", "")))
            if end > start and size + next_size > max_chars:
                break
            size += next_size
            end += 1
        result.append((start, max(start + 1, end)))
        start = max(start + 1, end)
    return result


def guard_correction(original: str, proposed: str) -> tuple[bool, list[str]]:
    risks: list[str] = []
    if not proposed.strip():
        return False, ["empty_output"]
    ratio = len(proposed) / max(len(original), 1)
    if ratio < 0.65 or ratio > 1.45:
        risks.append("length_changed_too_much")
    if set(NUMBER_PATTERN.findall(original)) != set(NUMBER_PATTERN.findall(proposed)):
        risks.append("numbers_changed")
    original_negations = {word for word in NEGATIONS if word in original}
    proposed_negations = {word for word in NEGATIONS if word in proposed}
    if original_negations != proposed_negations:
        risks.append("negation_changed")
    return not risks, risks


def propose_chunk(
    client: JsonClient,
    profile: dict[str, Any],
    segments: list[dict[str, Any]],
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    target = [
        {"id": index, "start": item["start"], "end": item["end"], "text": item["text"]}
        for index, item in enumerate(segments[start:end], start=start)
    ]
    payload, _usage = client.complete_json(
        """你是保守的语音转写纠错模型 A。只修正结合上下文和领域热词可以高度确定的 ASR 同音词或专有名词错误。
不要润色、总结、改写或补充事实；不得修改数字、单位、否定关系和说话顺序。
只返回真正需要替换的短词；不要返回完整句子，不要返回“原文正确”或“无需修改”的项目。original_span 必须逐字存在于原文，replacement 是要替换成的短词。
只返回 JSON：{"corrections":[{"id":0,"original_span":"误识别短词","replacement":"正确短词","reason":""}]}。""",
        {
            "domain": profile.get("domain"),
            "topics": profile.get("topics", []),
            "hotwords": profile.get("hotwords", []),
            "previous_context": " ".join(item["text"] for item in segments[max(0, start - 2) : start]),
            "segments": target,
            "next_context": " ".join(item["text"] for item in segments[end : min(len(segments), end + 2)]),
        },
        max_tokens=1800,
    )
    values = payload.get("corrections")
    if not isinstance(values, list):
        raise ValueError("纠错模型未返回 corrections 数组")
    allowed = set(range(start, end))
    proposals: list[dict[str, Any]] = []
    replacements_by_id: dict[int, list[dict[str, str]]] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            segment_id = int(value.get("id"))
        except (TypeError, ValueError):
            continue
        if segment_id not in allowed:
            continue
        original = segments[segment_id]["text"]
        original_span = str(value.get("original_span") or "").strip()
        replacement = str(value.get("replacement") or "").strip()
        if (
            not original_span
            or not replacement
            or original_span == replacement
            or original_span not in original
            or len(original_span) > 40
            or len(replacement) > 50
        ):
            continue
        replacements_by_id.setdefault(segment_id, []).append(
            {
                "original_span": original_span,
                "replacement": replacement,
                "reason": str(value.get("reason") or "").strip(),
            }
        )
    for segment_id, replacements in replacements_by_id.items():
        original = segments[segment_id]["text"]
        proposed = original
        applied: list[dict[str, str]] = []
        for replacement in sorted(replacements, key=lambda item: len(item["original_span"]), reverse=True):
            original_span = replacement["original_span"]
            if original_span not in proposed:
                continue
            proposed = proposed.replace(original_span, replacement["replacement"], 1)
            applied.append(replacement)
        if proposed == original or not applied:
            continue
        proposals.append(
            {
                "id": segment_id,
                "original": original,
                "proposed": proposed,
                "reason": "；".join(item["reason"] for item in applied if item["reason"]),
                "replacements": applied,
            }
        )
    return proposals


def verify_proposals(
    client: JsonClient,
    profile: dict[str, Any],
    segments: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> dict[int, dict[str, str]]:
    if not proposals:
        return {}
    guarded: list[dict[str, Any]] = []
    decisions: dict[int, dict[str, str]] = {}
    for proposal in proposals:
        accepted, risks = guard_correction(proposal["original"], proposal["proposed"])
        if not accepted:
            decisions[proposal["id"]] = {
                "decision": "reject",
                "reason": "程序保护拦截：" + ", ".join(risks),
            }
            continue
        index = int(proposal["id"])
        guarded.append(
            {
                **proposal,
                "previous_context": " ".join(item["text"] for item in segments[max(0, index - 2) : index]),
                "next_context": " ".join(item["text"] for item in segments[index + 1 : min(len(segments), index + 3)]),
            }
        )
    if not guarded:
        return decisions
    payload, _usage = client.complete_json(
        """你是独立的语音转写验证模型 B。你不能再次改写文本，只能判断模型 A 的修改是否被领域热词和上下文充分支持。
对每一项返回 accept、reject 或 uncertain。只有高度确定是 ASR 误识别时才 accept；需要听原音才能确定时必须 uncertain。
只返回 JSON：{"verifications":[{"id":0,"decision":"accept|reject|uncertain","reason":""}]}。""",
        {
            "domain": profile.get("domain"),
            "topics": profile.get("topics", []),
            "hotwords": profile.get("hotwords", []),
            "proposals": guarded,
        },
        max_tokens=3500,
    )
    values = payload.get("verifications")
    if not isinstance(values, list):
        raise ValueError("验证模型未返回 verifications 数组")
    expected = {int(item["id"]) for item in guarded}
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            segment_id = int(value.get("id"))
        except (TypeError, ValueError):
            continue
        decision = str(value.get("decision") or "").strip().lower()
        if segment_id in expected and decision in {"accept", "reject", "uncertain"}:
            decisions[segment_id] = {"decision": decision, "reason": str(value.get("reason") or "").strip()}
    for segment_id in expected:
        decisions.setdefault(segment_id, {"decision": "uncertain", "reason": "验证模型未返回该项判断"})
    return decisions


def process_transcript(
    raw_path: Path,
    source_root: Path,
    output_root: Path,
    profile: dict[str, Any],
    corrector: JsonClient,
    verifier: JsonClient,
    progress: Callable[[int, int, str], None] | None = None,
    pause: Callable[[], None] | None = None,
) -> tuple[Path, dict[str, int]]:
    transcript = load_transcript(raw_path)
    if transcript is None:
        raise ValueError("不是有效的结构化转写 JSON")
    segments = transcript["segments"]
    ranges = chunks(segments)
    proposals: list[dict[str, Any]] = []
    for chunk_index, (start, end) in enumerate(ranges, start=1):
        if pause:
            pause()
        proposals.extend(propose_chunk(corrector, profile, segments, start, end))
        if progress:
            progress(chunk_index, max(1, len(ranges) * 2), "correct")

    decisions: dict[int, dict[str, str]] = {}
    # Verification is deliberately one proposal per request. Small local models
    # can otherwise attach a valid reason to the wrong segment id, which is more
    # dangerous than leaving a correction unaccepted.
    proposal_chunks = [[proposal] for proposal in proposals]
    for verify_index, proposal_group in enumerate(proposal_chunks, start=1):
        if pause:
            pause()
        decisions.update(verify_proposals(verifier, profile, segments, proposal_group))
        if progress:
            mapped = len(ranges) + round(verify_index / max(len(proposal_chunks), 1) * len(ranges))
            progress(mapped, max(1, len(ranges) * 2), "verify")
    if not proposal_chunks and progress:
        progress(len(ranges) * 2, max(1, len(ranges) * 2), "verify")

    by_id = {int(item["id"]): item for item in proposals}
    final_segments: list[dict[str, Any]] = []
    accepted_count = 0
    rejected_count = 0
    uncertain_count = 0
    for index, item in enumerate(segments):
        proposal = by_id.get(index)
        if proposal is None:
            final_segments.append(
                {
                    "id": item["id"],
                    "start": item["start"],
                    "end": item["end"],
                    "raw_text": item["text"],
                    "final_text": item["text"],
                    "verification": "unchanged",
                    "knowledge_ready": True,
                }
            )
            continue
        verification = decisions.get(index, {"decision": "uncertain", "reason": "缺少验证判断"})
        decision = verification["decision"]
        accepted = decision == "accept"
        if accepted:
            accepted_count += 1
        elif decision == "reject":
            rejected_count += 1
        else:
            uncertain_count += 1
        final_segments.append(
            {
                "id": item["id"],
                "start": item["start"],
                "end": item["end"],
                "raw_text": item["text"],
                "final_text": proposal["proposed"] if accepted else item["text"],
                "proposed_text": proposal["proposed"],
                "verification": decision,
                "knowledge_ready": accepted,
                "correction_reason": proposal["reason"],
                "verification_reason": verification["reason"],
            }
        )
    same_model = (
        type(corrector) is type(verifier)
        and getattr(corrector, "model", "") == getattr(verifier, "model", "")
        and getattr(corrector, "base_url", "") == getattr(verifier, "base_url", "")
    )
    stats = {
        "segments": len(final_segments),
        "proposed": len(proposals),
        "accepted": accepted_count,
        "rejected": rejected_count,
        "uncertain": uncertain_count,
        "knowledge_ready": sum(bool(item["knowledge_ready"]) for item in final_segments),
    }
    result = {
        "schema_version": 1,
        "created_at": iso_now(),
        "status": "verified" if rejected_count + uncertain_count == 0 else "verified_with_exclusions",
        "source": transcript["source"],
        "raw_transcript": str(raw_path.resolve()),
        "domain": {
            "name": profile.get("domain", ""),
            "topics": profile.get("topics", []),
            "profile_file": str((output_root / PROFILE_NAME).resolve()),
        },
        "models": {
            "corrector": getattr(corrector, "model", ""),
            "verifier": getattr(verifier, "model", ""),
            "verification_mode": "same_model_independent_roles" if same_model else "dual_model",
        },
        "stats": stats,
        "segments": final_segments,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    output = verified_output_path(raw_path, source_root, output_root)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return output, stats


def run_pipeline(
    source_root: Path,
    output_root: Path,
    corrector_config: ProviderConfig,
    verifier_config: ProviderConfig,
    skip_existing: bool = True,
    refresh_profile: bool = False,
    limit: int = 0,
    match: str = "",
    gui_events: bool = False,
    pause_file: Path | None = None,
) -> dict[str, Any]:
    all_records = discover_transcripts(source_root, output_root)
    if not all_records:
        raise RuntimeError("所选目录中没有有效的结构化转写 JSON")
    matched_records = [
        item for item in all_records
        if not match.strip()
        or match.strip().casefold() in str(item.get("path", "")).casefold()
        or match.strip().casefold() in str(item.get("source", "")).casefold()
    ]
    records = matched_records[:limit] if limit > 0 else matched_records
    if not records:
        raise RuntimeError(f"没有找到匹配的结构化转写：{match}")
    corrector = create_client(corrector_config)
    verifier = create_client(verifier_config)
    output_root.mkdir(parents=True, exist_ok=True)
    emit_event(gui_events, "domain_profile_start", sample_count=min(6, len(all_records)))
    profile = load_or_create_profile(all_records, output_root, corrector, refresh_profile)
    emit_event(
        gui_events,
        "domain_profile_ready",
        domain=profile.get("domain", ""),
        topics=profile.get("topics", []),
        hotwords=profile.get("hotwords", []),
        profile_path=str((output_root / PROFILE_NAME).resolve()),
    )
    summary: dict[str, Any] = {
        "total": len(records),
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "accepted": 0,
        "rejected": 0,
        "uncertain": 0,
        "output_dir": str(output_root.resolve()),
    }
    emit_event(gui_events, "clean_batch_start", **summary)

    def wait_if_paused() -> None:
        while pause_file is not None and pause_file.is_file():
            time.sleep(0.2)

    for file_index, record in enumerate(records, start=1):
        wait_if_paused()
        raw_path = Path(record["path"])
        output = verified_output_path(raw_path, source_root, output_root)
        if skip_existing and output.is_file():
            summary["skipped"] += 1
            emit_event(
                gui_events,
                "clean_file_skipped",
                path=str(raw_path),
                output=str(output),
                file_index=file_index,
                file_total=len(records),
            )
            continue
        emit_event(
            gui_events,
            "clean_file_start",
            path=str(raw_path),
            file_index=file_index,
            file_total=len(records),
            segment_count=record["segment_count"],
        )

        def progress(current: int, total: int, stage: str) -> None:
            emit_event(
                gui_events,
                "clean_file_progress",
                path=str(raw_path),
                file_index=file_index,
                file_total=len(records),
                current=current,
                total=total,
                stage=stage,
                progress=round(current / max(total, 1) * 100, 2),
            )
            wait_if_paused()

        try:
            output, stats = process_transcript(
                raw_path,
                source_root,
                output_root,
                profile,
                corrector,
                verifier,
                progress,
                wait_if_paused,
            )
            summary["completed"] += 1
            for key in ("accepted", "rejected", "uncertain"):
                summary[key] += int(stats[key])
            emit_event(
                gui_events,
                "clean_file_done",
                path=str(raw_path),
                output=str(output),
                outputs=[str(output)],
                stats=stats,
                file_index=file_index,
                file_total=len(records),
            )
        except Exception as exc:
            summary["failed"] += 1
            print(f"可信处理失败：{raw_path}\n{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            emit_event(
                gui_events,
                "clean_file_error",
                path=str(raw_path),
                error=f"{type(exc).__name__}: {exc}",
                file_index=file_index,
                file_total=len(records),
            )
    emit_event(gui_events, "clean_batch_done", **summary)
    return summary


def search_verified(output_root: Path, query: str, limit: int = 50) -> list[dict[str, Any]]:
    root = output_root.expanduser().resolve()
    query = query.strip()
    if not query:
        raise ValueError("请输入需要验收的关键词")
    if not root.is_dir():
        raise FileNotFoundError(f"可信数据目录不存在：{root}")
    tokens = [item for item in re.split(r"\s+", query.casefold()) if item]
    hits: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.verified.json"), key=lambda value: value.name.casefold()):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        source = str(payload.get("source") or "")
        domain = str((payload.get("domain") or {}).get("name") or "")
        for item in payload.get("segments", []):
            if not isinstance(item, dict) or not item.get("knowledge_ready"):
                continue
            text = str(item.get("final_text") or "")
            lowered = text.casefold()
            score = sum(lowered.count(token) for token in tokens)
            if score <= 0:
                continue
            hits.append(
                {
                    "source_video": source,
                    "verified_file": str(path),
                    "domain": domain,
                    "start_seconds": float(item.get("start") or 0.0),
                    "end_seconds": float(item.get("end") or item.get("start") or 0.0),
                    "text": text,
                    "verification": str(item.get("verification") or ""),
                    "score": score,
                }
            )
    hits.sort(key=lambda item: (-int(item["score"]), str(item["source_video"]).casefold(), item["start_seconds"]))
    return hits[: max(1, min(limit, 200))]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LocalTranscriber 可信数据处理")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("source_dir", type=Path)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--corrector-provider", choices=["local", "openai", "deepseek"], default="local")
    run.add_argument("--corrector-model", default=DEFAULT_LOCAL_MODEL)
    run.add_argument("--corrector-base-url", default=DEFAULT_LOCAL_BASE_URL)
    run.add_argument("--verifier-provider", choices=["local", "openai", "deepseek"], default="local")
    run.add_argument("--verifier-model", default=DEFAULT_LOCAL_MODEL)
    run.add_argument("--verifier-base-url", default=DEFAULT_LOCAL_BASE_URL)
    run.add_argument("--skip-existing", action="store_true")
    run.add_argument("--refresh-profile", action="store_true")
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--match", default="")
    run.add_argument("--pause-file", type=Path)
    run.add_argument("--gui-events", action="store_true", help=argparse.SUPPRESS)
    search = subparsers.add_parser("search")
    search.add_argument("output_dir", type=Path)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=50)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "search":
        print(json.dumps(search_verified(args.output_dir, args.query, args.limit), ensure_ascii=False, indent=2))
        return 0
    output_root = args.output_dir or args.source_dir.expanduser().resolve() / OUTPUT_FOLDER_NAME
    legacy_key = os.environ.get("DEEPSEEK_API_KEY", "")
    corrector_key = os.environ.get("CORRECTOR_API_KEY", legacy_key)
    verifier_key = os.environ.get("VERIFIER_API_KEY", legacy_key)
    summary = run_pipeline(
        args.source_dir,
        output_root,
        ProviderConfig(args.corrector_provider, args.corrector_model, args.corrector_base_url, corrector_key),
        ProviderConfig(args.verifier_provider, args.verifier_model, args.verifier_base_url, verifier_key),
        skip_existing=args.skip_existing,
        refresh_profile=args.refresh_profile,
        limit=max(0, args.limit),
        match=args.match,
        gui_events=args.gui_events,
        pause_file=args.pause_file,
    )
    if not args.gui_events:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
