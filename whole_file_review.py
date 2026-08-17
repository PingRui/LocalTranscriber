from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from trusted_pipeline import (
    EVENT_PREFIX,
    ProviderConfig,
    create_client,
    discover_transcripts,
    estimate_transcript_tokens,
    guard_correction,
    load_transcript,
    verified_output_path,
)


DEFAULT_CONTEXT_WINDOW = 128_000
REVIEW_ALGORITHM_VERSION = "resumable-chunks-v1"
CHUNK_TRIGGER_TOKENS = 12_000
CHUNK_TARGET_TOKENS = 6_000
CHUNK_MAX_SEGMENTS = 240
BOUNDARY_CONTEXT_SEGMENTS = 3


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emit_event(enabled: bool, event_type: str, **payload: Any) -> None:
    if enabled:
        print(EVENT_PREFIX + json.dumps({"event": event_type, **payload}, ensure_ascii=False), flush=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def corrections_output_path(verified_path: Path) -> Path:
    suffix = ".verified.json"
    name = verified_path.name
    return verified_path.with_name(
        name[: -len(suffix)] + ".corrections.json" if name.endswith(suffix) else name + ".corrections.json"
    )


def hotword_context(metadata: dict[str, Any]) -> dict[str, Any]:
    raw = str(metadata.get("hotwords") or "")
    terms = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
    task = metadata.get("task_hotwords") if isinstance(metadata.get("task_hotwords"), dict) else {}
    return {
        "category": str(task.get("category") or "未分类"),
        "hotwords": list(dict.fromkeys(terms))[:80],
    }


def _segment_token_cost(segment: dict[str, Any]) -> int:
    text = str(segment.get("text") or "")
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    other = max(0, len(text) - cjk)
    return max(12, round(cjk * 1.2 + other / 4 + 10))


def _partition_segments(
    segments: list[dict[str, Any]],
    context_window: int,
) -> tuple[list[tuple[int, int]], bool]:
    estimated_tokens = estimate_transcript_tokens(segments)
    single_output_tokens = min(4_096, max(1_024, estimated_tokens // 3))
    fits_single_request = estimated_tokens + single_output_tokens + 1_500 <= max(8_000, int(context_window))
    if (
        estimated_tokens <= CHUNK_TRIGGER_TOKENS
        and len(segments) <= CHUNK_MAX_SEGMENTS
        and fits_single_request
    ):
        return [(0, len(segments))], False

    target_tokens = max(1_500, min(CHUNK_TARGET_TOKENS, max(1_500, int(context_window) // 2)))
    chunks: list[tuple[int, int]] = []
    start = 0
    while start < len(segments):
        end = start
        tokens = 800
        while end < len(segments) and end - start < CHUNK_MAX_SEGMENTS:
            cost = _segment_token_cost(segments[end])
            if end > start and tokens + cost > target_tokens:
                break
            tokens += cost
            end += 1
        end = max(start + 1, end)
        chunks.append((start, end))
        start = end
    return chunks, len(chunks) > 1


def _review_fingerprint(transcript: dict[str, Any], client: Any, chunks: list[tuple[int, int]]) -> str:
    context = hotword_context(transcript.get("metadata") or {})
    material = {
        "algorithm": REVIEW_ALGORITHM_VERSION,
        "model": str(getattr(client, "model", "")),
        "source": str(transcript["source"]),
        "category": context["category"],
        "hotwords": context["hotwords"],
        "chunks": chunks,
        "segments": [
            {"id": item["id"], "start": item["start"], "end": item["end"], "text": item["text"]}
            for item in transcript["segments"]
        ],
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _review_chunk(
    client: Any,
    transcript: dict[str, Any],
    start: int,
    end: int,
    chunk_index: int,
    chunk_total: int,
    context_window: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    segments = transcript["segments"]
    context = hotword_context(transcript.get("metadata") or {})
    target = segments[start:end]
    chunk_tokens = estimate_transcript_tokens(target)
    max_output_tokens = min(4_096, max(1_024, chunk_tokens // 3))
    if chunk_tokens + max_output_tokens + 1_500 > max(8_000, int(context_window)):
        raise ValueError(
            f"第 {chunk_index + 1} 个校验分段仍过大（约 {chunk_tokens:,} Token），请降低分段大小"
        )
    payload, usage = client.complete_json(
        """你是保守的语音转写分段校对模型。你正在校对一个长文件中的连续片段。
只修正结合目标片段、相邻上下文和任务热词可以高度确定的 ASR 同音词或专有名词错误。
不要润色、总结、改写或补充事实；不得修改数字、单位、否定关系和说话顺序。
previous_context 和 next_context 仅用于理解，不得对其中内容提出修改；corrections 只能引用 target_segments 中的 segment_id。
original_span 必须逐字存在于对应目标片段，replacement 只能包含局部替换文字。
必须检查完本分段。只返回 JSON：
{"review_complete":true,"corrections":[{"segment_id":"0","original_span":"误识别短词","replacement":"正确短词","reason":"依据","confidence":"high|medium|low"}]}。
没有错误时 corrections 返回空数组。""",
        {
            "file": Path(transcript["path"]).name,
            "source_video": Path(transcript["source"]).name,
            "category": context["category"],
            "hotwords": context["hotwords"],
            "chunk_index": chunk_index + 1,
            "chunk_total": chunk_total,
            "previous_context": [
                {"id": item["id"], "text": item["text"]}
                for item in segments[max(0, start - BOUNDARY_CONTEXT_SEGMENTS) : start]
            ],
            "target_segments": [
                {"id": item["id"], "start": item["start"], "end": item["end"], "text": item["text"]}
                for item in target
            ],
            "next_context": [
                {"id": item["id"], "text": item["text"]}
                for item in segments[end : end + BOUNDARY_CONTEXT_SEGMENTS]
            ],
        },
        max_tokens=max_output_tokens,
    )
    if payload.get("review_complete") is not True:
        raise RuntimeError(f"模型没有确认完成第 {chunk_index + 1}/{chunk_total} 个校验分段")
    if not isinstance(payload.get("corrections"), list):
        raise RuntimeError(f"第 {chunk_index + 1}/{chunk_total} 个分段没有返回有效 corrections 数组")
    return payload, usage


def _valid_chunk_checkpoint(
    payload: dict[str, Any] | None,
    review_id: str,
    chunk_index: int,
    segment_ids: list[str],
) -> bool:
    return bool(
        payload
        and payload.get("schema_version") == 1
        and payload.get("review_id") == review_id
        and payload.get("chunk_index") == chunk_index
        and payload.get("segment_ids") == segment_ids
        and isinstance(payload.get("response"), dict)
        and payload["response"].get("review_complete") is True
        and isinstance(payload["response"].get("corrections"), list)
    )


def _candidate_id(chunk_index: int, item_index: int, raw: dict[str, Any]) -> str:
    material = "|".join(
        [
            str(chunk_index),
            str(item_index),
            str(raw.get("segment_id") or raw.get("id") or ""),
            str(raw.get("original_span") or ""),
            str(raw.get("replacement") or ""),
        ]
    )
    return f"c{chunk_index + 1}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:10]}"


def _global_consistency_check(
    client: Any,
    transcript: dict[str, Any],
    chunks: list[tuple[int, int]],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    segments = transcript["segments"]
    by_id = {str(item["id"]): item for item in segments}
    chunk_summaries: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(chunks):
        first = segments[start]
        last = segments[end - 1]
        chunk_summaries.append(
            {
                "chunk_index": index + 1,
                "start": first["start"],
                "end": last["end"],
                "opening": first["text"][:160],
                "closing": last["text"][-160:],
                "candidate_ids": [item["correction_id"] for item in candidates if item["chunk_index"] == index],
            }
        )
    compact_candidates = []
    for item in candidates:
        segment = by_id.get(str(item.get("segment_id") or ""), {})
        compact_candidates.append(
            {
                "correction_id": item["correction_id"],
                "chunk_index": item["chunk_index"] + 1,
                "segment_id": item.get("segment_id"),
                "segment_text": str(segment.get("text") or "")[:320],
                "original_span": item.get("original_span"),
                "replacement": item.get("replacement"),
                "reason": item.get("reason"),
                "confidence": item.get("confidence"),
            }
        )
    payload, usage = client.complete_json(
        """你是长文件校对结果的全局一致性检查器。输入只包含各分段边界摘要和分段校对提出的候选修正。
你的任务是检查同一术语跨分段是否冲突、候选修正是否互相矛盾，以及候选是否仍符合其原始片段。
不得新增修正，不得改写候选；只能批准或拒绝已有 correction_id。每个候选 ID 必须且只能出现一次。
只返回 JSON：{"review_complete":true,"approved_correction_ids":["c1-..."],"rejected_corrections":[{"correction_id":"c2-...","reason":"冲突原因"}]}。
没有候选时，两个数组都返回空数组。""",
        {
            "file": Path(transcript["path"]).name,
            "source_video": Path(transcript["source"]).name,
            "chunk_summaries": chunk_summaries,
            "candidate_corrections": compact_candidates,
        },
        max_tokens=min(3_072, max(1_024, len(candidates) * 120 + 512)),
    )
    if payload.get("review_complete") is not True:
        raise RuntimeError("模型没有确认完成全局一致性检查")
    approved = payload.get("approved_correction_ids")
    rejected = payload.get("rejected_corrections")
    if not isinstance(approved, list) or not isinstance(rejected, list):
        raise RuntimeError("全局一致性检查没有返回有效的批准与拒绝列表")
    approved_ids = [str(item) for item in approved]
    rejected_ids = [
        str(item.get("correction_id") or "") for item in rejected if isinstance(item, dict)
    ]
    expected_ids = [item["correction_id"] for item in candidates]
    resolved = approved_ids + rejected_ids
    if len(resolved) != len(set(resolved)) or set(resolved) != set(expected_ids):
        raise RuntimeError("全局一致性检查没有逐项覆盖全部候选修正")
    return payload, usage


def _valid_global_checkpoint(
    payload: dict[str, Any] | None,
    review_id: str,
    candidate_ids: list[str],
) -> bool:
    if not payload or payload.get("schema_version") != 1 or payload.get("review_id") != review_id:
        return False
    if payload.get("candidate_ids") != candidate_ids or not isinstance(payload.get("response"), dict):
        return False
    response = payload["response"]
    approved = response.get("approved_correction_ids")
    rejected = response.get("rejected_corrections")
    if response.get("review_complete") is not True or not isinstance(approved, list) or not isinstance(rejected, list):
        return False
    resolved = [str(item) for item in approved] + [
        str(item.get("correction_id") or "") for item in rejected if isinstance(item, dict)
    ]
    return len(resolved) == len(set(resolved)) and set(resolved) == set(candidate_ids)


def _apply_corrections(
    client: Any,
    transcript: dict[str, Any],
    raw_corrections: list[dict[str, Any]],
    globally_rejected: dict[str, str],
    estimated_tokens: int,
    verification_mode: str,
    chunk_count: int,
    chunks_reused: int,
    usage: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    segments = transcript["segments"]
    context = hotword_context(transcript.get("metadata") or {})
    by_id = {str(item["id"]): item for item in segments}
    corrections_by_id: dict[str, list[dict[str, Any]]] = {}
    audit: list[dict[str, Any]] = []
    for raw in raw_corrections:
        if not isinstance(raw, dict):
            continue
        correction_id = str(raw.get("correction_id") or "")
        segment_id = str(raw.get("segment_id") or raw.get("id") or "")
        original_span = str(raw.get("original_span") or "").strip()
        replacement = str(raw.get("replacement") or "").strip()
        confidence = str(raw.get("confidence") or "").strip().lower()
        reason = str(raw.get("reason") or "").strip()
        base = {
            "correction_id": correction_id,
            "segment_id": segment_id,
            "original_span": original_span,
            "replacement": replacement,
            "reason": reason,
            "confidence": confidence or "unknown",
        }
        if correction_id and correction_id in globally_rejected:
            audit.append(
                {**base, "status": "rejected", "status_reason": "全局一致性检查未通过：" + globally_rejected[correction_id]}
            )
            continue
        segment = by_id.get(segment_id)
        if segment is None or not original_span or not replacement or original_span == replacement:
            audit.append({**base, "status": "rejected", "status_reason": "修改位置或替换内容无效"})
            continue
        if len(original_span) > 80 or len(replacement) > 100 or original_span not in segment["text"]:
            audit.append({**base, "status": "rejected", "status_reason": "原错误文字不在指定片段中"})
            continue
        corrections_by_id.setdefault(segment_id, []).append(base)

    final_segments: list[dict[str, Any]] = []
    applied = 0
    pending = 0
    rejected = sum(item["status"] == "rejected" for item in audit)
    for item in segments:
        segment_id = str(item["id"])
        original = item["text"]
        final = original
        segment_pending = bool(item.get("review_reasons"))
        segment_audit: list[dict[str, Any]] = []
        for correction in sorted(
            corrections_by_id.get(segment_id, []), key=lambda value: len(value["original_span"]), reverse=True
        ):
            if correction["original_span"] not in final:
                record = {**correction, "status": "rejected", "status_reason": "同一片段中的修改发生冲突"}
                audit.append(record)
                segment_audit.append(record)
                rejected += 1
                segment_pending = True
                continue
            proposed = final.replace(correction["original_span"], correction["replacement"], 1)
            safe, risks = guard_correction(final, proposed)
            if correction["confidence"] != "high" or not safe:
                reason = "置信度不足" if correction["confidence"] != "high" else "程序保护拦截：" + ", ".join(risks)
                record = {**correction, "status": "pending", "status_reason": reason}
                audit.append(record)
                segment_audit.append(record)
                pending += 1
                segment_pending = True
                continue
            final = proposed
            record = {**correction, "status": "applied", "status_reason": "高置信度且通过程序保护"}
            audit.append(record)
            segment_audit.append(record)
            applied += 1
        final_segments.append(
            {
                "id": item["id"],
                "start": item["start"],
                "end": item["end"],
                "raw_text": original,
                "final_text": final,
                "verification": "pending" if segment_pending else ("applied" if final != original else "unchanged"),
                "knowledge_ready": not segment_pending,
                "review_reasons": item.get("review_reasons", []),
                "corrections": segment_audit,
            }
        )
    stats = {
        "segments": len(final_segments),
        "proposed": len(audit),
        "accepted": applied,
        "rejected": rejected,
        "uncertain": pending,
        "knowledge_ready": sum(bool(item["knowledge_ready"]) for item in final_segments),
        "estimated_tokens": estimated_tokens,
        "chunk_count": chunk_count,
        "chunks_reused": chunks_reused,
        "global_consistency": verification_mode == "resumable_chunked_with_global_consistency",
    }
    created_at = iso_now()
    result = {
        "schema_version": 2,
        "created_at": created_at,
        "status": "reviewed" if pending + rejected == 0 else "reviewed_with_pending",
        "source": transcript["source"],
        "raw_transcript": str(transcript["path"]),
        "domain": {"name": context["category"], "topics": []},
        "hotwords": context["hotwords"],
        "models": {"corrector": getattr(client, "model", ""), "verification_mode": verification_mode},
        "stats": stats,
        "segments": final_segments,
    }
    correction_log = {
        "schema_version": 2,
        "created_at": created_at,
        "source": transcript["source"],
        "raw_transcript": str(transcript["path"]),
        "model": getattr(client, "model", ""),
        "verification_mode": verification_mode,
        "review_complete": True,
        "stats": stats,
        "corrections": audit,
        "usage": usage,
    }
    return result, correction_log, stats


def _review_transcript(
    client: Any,
    transcript: dict[str, Any],
    context_window: int,
    checkpoint_dir: Path | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    pause_check: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    segments = transcript["segments"]
    estimated_tokens = estimate_transcript_tokens(segments)
    context_window = max(8_000, int(context_window))
    chunks, chunked = _partition_segments(segments, context_window)

    if not chunked:
        if progress:
            progress(1, 3, "review")
        if pause_check:
            pause_check()
        payload, usage = _review_chunk(client, transcript, 0, len(segments), 0, 1, context_window)
        raw_corrections = [item for item in payload["corrections"] if isinstance(item, dict)]
        if progress:
            progress(2, 3, "apply")
        result = _apply_corrections(
            client,
            transcript,
            raw_corrections,
            {},
            estimated_tokens,
            "whole_file_single_model",
            1,
            0,
            usage,
        )
        if progress:
            progress(3, 3, "done")
        return result

    review_id = _review_fingerprint(transcript, client, chunks)
    active_checkpoint = checkpoint_dir / review_id if checkpoint_dir is not None else None
    manifest = {
        "schema_version": 1,
        "algorithm": REVIEW_ALGORITHM_VERSION,
        "review_id": review_id,
        "created_at": iso_now(),
        "source": transcript["source"],
        "raw_transcript": str(transcript["path"]),
        "model": getattr(client, "model", ""),
        "chunk_count": len(chunks),
        "chunks": [
            {
                "index": index,
                "start_segment": start,
                "end_segment": end,
                "segment_ids": [str(item["id"]) for item in segments[start:end]],
            }
            for index, (start, end) in enumerate(chunks)
        ],
    }
    if active_checkpoint is not None:
        active_checkpoint.mkdir(parents=True, exist_ok=True)
        manifest_path = active_checkpoint / "manifest.json"
        existing_manifest = _load_json(manifest_path)
        if not existing_manifest or existing_manifest.get("review_id") != review_id:
            atomic_write_json(manifest_path, manifest)

    total_steps = len(chunks) + 2
    chunk_responses: list[dict[str, Any]] = []
    chunk_usages: list[dict[str, Any]] = []
    chunks_reused = 0
    for index, (start, end) in enumerate(chunks):
        if pause_check:
            pause_check()
        segment_ids = [str(item["id"]) for item in segments[start:end]]
        checkpoint_path = active_checkpoint / f"chunk-{index + 1:04d}.json" if active_checkpoint else None
        checkpoint = _load_json(checkpoint_path) if checkpoint_path else None
        if _valid_chunk_checkpoint(checkpoint, review_id, index, segment_ids):
            response = checkpoint["response"]
            usage = checkpoint.get("usage") if isinstance(checkpoint.get("usage"), dict) else {}
            chunks_reused += 1
        else:
            response, usage = _review_chunk(client, transcript, start, end, index, len(chunks), context_window)
            if checkpoint_path:
                atomic_write_json(
                    checkpoint_path,
                    {
                        "schema_version": 1,
                        "review_id": review_id,
                        "chunk_index": index,
                        "chunk_total": len(chunks),
                        "segment_ids": segment_ids,
                        "completed_at": iso_now(),
                        "response": response,
                        "usage": usage,
                    },
                )
        chunk_responses.append(response)
        chunk_usages.append(usage)
        if progress:
            progress(index + 1, total_steps, "review_chunk")

    candidates: list[dict[str, Any]] = []
    for chunk_index, response in enumerate(chunk_responses):
        for item_index, raw in enumerate(response.get("corrections", [])):
            if not isinstance(raw, dict):
                continue
            candidates.append(
                {
                    **raw,
                    "segment_id": str(raw.get("segment_id") or raw.get("id") or ""),
                    "correction_id": _candidate_id(chunk_index, item_index, raw),
                    "chunk_index": chunk_index,
                }
            )

    if pause_check:
        pause_check()
    candidate_ids = [item["correction_id"] for item in candidates]
    global_path = active_checkpoint / "global-consistency.json" if active_checkpoint else None
    global_checkpoint = _load_json(global_path) if global_path else None
    if _valid_global_checkpoint(global_checkpoint, review_id, candidate_ids):
        global_response = global_checkpoint["response"]
        global_usage = global_checkpoint.get("usage") if isinstance(global_checkpoint.get("usage"), dict) else {}
    else:
        global_response, global_usage = _global_consistency_check(client, transcript, chunks, candidates)
        if global_path:
            atomic_write_json(
                global_path,
                {
                    "schema_version": 1,
                    "review_id": review_id,
                    "candidate_ids": candidate_ids,
                    "completed_at": iso_now(),
                    "response": global_response,
                    "usage": global_usage,
                },
            )
    if progress:
        progress(len(chunks) + 1, total_steps, "global_consistency")

    approved_ids = {str(item) for item in global_response["approved_correction_ids"]}
    rejection_reasons = {
        str(item.get("correction_id") or ""): str(item.get("reason") or "存在跨分段冲突")
        for item in global_response["rejected_corrections"]
        if isinstance(item, dict)
    }
    approved_candidates = [item for item in candidates if item["correction_id"] in approved_ids]
    rejected_candidates = [item for item in candidates if item["correction_id"] in rejection_reasons]
    result = _apply_corrections(
        client,
        transcript,
        approved_candidates + rejected_candidates,
        rejection_reasons,
        estimated_tokens,
        "resumable_chunked_with_global_consistency",
        len(chunks),
        chunks_reused,
        {"chunks": chunk_usages, "global_consistency": global_usage},
    )
    if progress:
        progress(total_steps, total_steps, "done")
    return result


def review_full_file(
    client: Any,
    transcript: dict[str, Any],
    context_window: int = DEFAULT_CONTEXT_WINDOW,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return _review_transcript(client, transcript, context_window)


def process_file(
    raw_path: Path,
    source_root: Path,
    output_root: Path,
    client: Any,
    context_window: int,
    progress: Callable[[int, int, str], None] | None = None,
    pause_check: Callable[[], None] | None = None,
) -> tuple[list[Path], dict[str, Any]]:
    transcript = load_transcript(raw_path)
    if transcript is None:
        raise ValueError("不是有效的结构化转写 JSON")
    verified = verified_output_path(raw_path, source_root, output_root)
    checkpoint_base = output_root / ".review-checkpoints" / verified.name.removesuffix(".verified.json")
    result, correction_log, stats = _review_transcript(
        client,
        transcript,
        context_window,
        checkpoint_base,
        progress,
        pause_check,
    )
    corrections = corrections_output_path(verified)
    atomic_write_json(verified, result)
    atomic_write_json(corrections, correction_log)
    return [verified, corrections], stats


def run_pipeline(
    source_root: Path,
    output_root: Path,
    corrector_config: ProviderConfig,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    skip_existing: bool = True,
    gui_events: bool = False,
    pause_file: Path | None = None,
) -> dict[str, Any]:
    records = discover_transcripts(source_root, output_root)
    if not records:
        raise RuntimeError("所选目录中没有有效的结构化转写 JSON")
    client = create_client(corrector_config)
    summary: dict[str, Any] = {
        "total": len(records), "completed": 0, "failed": 0, "skipped": 0,
        "accepted": 0, "rejected": 0, "uncertain": 0, "output_dir": str(output_root.resolve()),
    }
    emit_event(gui_events, "clean_batch_start", **summary)

    def wait_if_paused() -> None:
        while pause_file is not None and pause_file.is_file():
            time.sleep(0.2)

    for file_index, record in enumerate(records, start=1):
        wait_if_paused()
        raw_path = Path(record["path"])
        verified = verified_output_path(raw_path, source_root, output_root)
        corrections = corrections_output_path(verified)
        if skip_existing and verified.is_file() and corrections.is_file():
            summary["skipped"] += 1
            emit_event(gui_events, "clean_file_skipped", path=str(raw_path), outputs=[str(verified), str(corrections)], file_index=file_index, file_total=len(records))
            continue
        emit_event(gui_events, "clean_file_start", path=str(raw_path), file_index=file_index, file_total=len(records), segment_count=record["segment_count"], estimated_tokens=record.get("estimated_tokens", 0))

        def report(current: int, total: int, stage: str) -> None:
            emit_event(gui_events, "clean_file_progress", path=str(raw_path), file_index=file_index, file_total=len(records), current=current, total=total, stage=stage, progress=round(current / total * 100, 2))

        try:
            outputs, stats = process_file(
                raw_path,
                source_root,
                output_root,
                client,
                context_window,
                report,
                wait_if_paused,
            )
            summary["completed"] += 1
            for key in ("accepted", "rejected", "uncertain"):
                summary[key] += int(stats[key])
            emit_event(gui_events, "clean_file_done", path=str(raw_path), outputs=[str(item) for item in outputs], output=str(outputs[0]), stats=stats, file_index=file_index, file_total=len(records))
        except Exception as exc:
            summary["failed"] += 1
            error = f"{type(exc).__name__}: {exc}"
            print(f"整文件校对失败：{raw_path}\n{error}", file=sys.stderr, flush=True)
            emit_event(gui_events, "clean_file_error", path=str(raw_path), error=error, file_index=file_index, file_total=len(records))
        wait_if_paused()
    emit_event(gui_events, "clean_batch_done", **summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对转写执行可恢复的分段校验与轻量全局一致性检查")
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--corrector-provider", choices=["local", "openai", "deepseek"], default="openai")
    parser.add_argument("--corrector-model", required=True)
    parser.add_argument("--corrector-base-url", required=True)
    parser.add_argument("--context-window", type=int, default=DEFAULT_CONTEXT_WINDOW)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--gui-events", action="store_true")
    parser.add_argument("--pause-file", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    key = os.environ.get("CORRECTOR_API_KEY", "")
    try:
        summary = run_pipeline(
            args.source_root.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
            ProviderConfig(args.corrector_provider, args.corrector_model, args.corrector_base_url, key),
            args.context_window,
            args.skip_existing,
            args.gui_events,
            args.pause_file.expanduser().resolve() if args.pause_file else None,
        )
    except Exception as exc:
        print(f"整文件校对无法启动：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
