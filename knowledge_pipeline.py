from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from domain_hotwords import (
    assess_hotword_profile,
    known_domains_for_prompt,
    learn_from_verified,
    normalize_category,
)
from knowledge_space import archive_trusted_source, copy_video, load_index, publish_verified, space_paths


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _find_transcript(directory: Path) -> Path | None:
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        if path.name.endswith((".llm.json", ".verified.json", ".corrections.json", ".source-context.json")):
            continue
        payload = _read_json(path)
        if isinstance(payload.get("segments"), list) and payload.get("segments"):
            return path
    return None


def _find_verified(directory: Path) -> Path | None:
    for path in sorted(directory.glob("*.verified.json")) if directory.is_dir() else []:
        payload = _read_json(path)
        if isinstance(payload.get("segments"), list) and payload.get("segments"):
            return path
    return None


def _mark_completed(api: Any, stage_id: str, message: str) -> None:
    api._set_stage(stage_id, "completed", 100, message)


def _mark_skipped(api: Any, stage_id: str, message: str) -> None:
    api._set_stage(stage_id, "skipped", 100, message)


def _select_pending_confirmation(api: Any) -> None:
    if not api.task:
        return
    pending = [
        (index, video) for index, video in enumerate(api.task.get("videos", []))
        if video.get("status") == "needs_confirmation"
    ]
    if not pending:
        api.task["hotword_profile"] = None
        return
    index, video = pending[0]
    profile = _read_json(Path(str(video.get("hotword_file") or "")))
    assessment = profile.get("assessment") if isinstance(profile.get("assessment"), dict) else {}
    api.task["current_index"] = index
    api.task["current_video"] = video.get("name")
    api.task["current_stage"] = "confirm"
    api.task["stages"] = api._new_stage_state()
    for stage in api.task["stages"]:
        if stage["id"] in {"copy", "analyze"}:
            stage.update(status="completed", progress=100, message="已保存")
        elif stage["id"] == "confirm":
            stage.update(status="needs_confirmation", progress=0, message="专业词汇存在异常，等待确认")
    api.task["hotword_profile"] = {
        "category": assessment.get("category") or profile.get("category") or "未分类",
        "confidence": assessment.get("confidence") or profile.get("confidence") or 0,
        "hotwords": [item.get("term") for item in assessment.get("selected", []) if item.get("term")],
        "manual_reasons": list(assessment.get("manual_reasons") or []),
        "file": str(video.get("hotword_file") or ""),
        "video_index": index,
    }


def run_resumable_task(
    api: Any,
    *,
    python: Path,
    knowledge_worker: Path,
    transcriber: Path,
    reviewer: Path,
) -> None:
    """Run a task from durable artifacts. Every expensive stage is safe to resume or rerun."""
    assert api.task and api.space_root
    root = Path(api.space_root)
    task_dir = space_paths(root)["work"] / "tasks" / api.task["task_id"]
    known_domains_file = task_dir / "known-domains.json"
    try:
        for index, video in enumerate(api.task.get("videos", [])):
            if api.cancel_requested:
                break
            if video.get("status") == "completed":
                completed_work = task_dir / f"video-{index + 1:04d}"
                completed_verified = _find_verified(completed_work / "verified")
                completed_transcript = _find_transcript(completed_work / "transcript")
                copied_path = Path(str(video.get("copied_path") or ""))
                durable_verified = (
                    space_paths(root)["sources"]
                    / str(video.get("video_id") or "")
                    / "transcript.verified.json"
                )
                if copied_path.is_file() and completed_verified and not durable_verified.is_file():
                    archived = archive_trusted_source(root, copied_path, completed_verified, completed_transcript)
                    api.activity(
                        f"已补齐永久可信证据：{video.get('name')} · {archived['evidence_count']} 个片段"
                    )
                continue
            if video.get("status") == "needs_confirmation":
                continue
            api.task["current_index"] = index
            api.task["current_video"] = video.get("name")
            api.task["stages"] = api._new_stage_state()
            api.task["hotword_profile"] = None
            video["status"] = "processing"
            api.activity(f"开始处理：{video.get('name')}")
            api._persist_task()
            video_work = task_dir / f"video-{index + 1:04d}"
            video_work.mkdir(parents=True, exist_ok=True)

            try:
                copied_path = Path(str(video.get("copied_path") or ""))
                if copied_path.is_file():
                    copied = {
                        "path": str(copied_path.resolve()),
                        "video_id": str(video.get("video_id") or ""),
                        "copied": False,
                    }
                    _mark_skipped(api, "copy", "已找到知识空间中的视频副本")
                else:
                    api._set_stage("copy", "running", 0, "正在复制视频到知识空间")

                    def on_copy(done: int, total: int) -> None:
                        progress = done / max(total, 1) * 100
                        api._set_stage("copy", "running", progress, f"正在复制视频 · {progress:.0f}%")
                        api.notify("task_progress")

                    copied = copy_video(root, Path(video["source"]), on_copy)
                    video.update(copied_path=copied["path"], video_id=copied["video_id"])
                    _mark_completed(api, "copy", "视频已复制")
                    api.activity("视频已复制到知识空间" if copied["copied"] else "已复用知识空间中的相同视频")
                    api._persist_task()

                hotword_file = Path(str(video.get("hotword_file") or video_work / "hotwords.json"))
                profile = _read_json(hotword_file)
                confirmation = str(profile.get("confirmation") or "")
                if profile.get("status") == "ready" and confirmation in {"auto", "manual"}:
                    assessment = profile.get("assessment") if isinstance(profile.get("assessment"), dict) else {}
                    _mark_skipped(api, "analyze", "已复用样本分析结果")
                    _mark_skipped(api, "confirm", "已复用确认过的专业词汇")
                else:
                    _write_json(known_domains_file, known_domains_for_prompt(root))
                    api._set_stage("analyze", "running", 5, "正在本地预转录少量样本")
                    command = [
                        str(python), str(knowledge_worker), "analyze", copied["path"],
                        "--output", str(hotword_file),
                        "--model", str(api.app_config.get("default_model") or "medium"),
                        "--device", str(api.app_config.get("default_device") or "auto"),
                        "--language", api.language,
                        "--api-base-url", api.api_base_url,
                        "--api-model", api.api_model,
                        "--known-domains", str(known_domains_file),
                        "--gui-events",
                    ]
                    api._run_subprocess(command, {"KNOWLEDGE_API_KEY": api.api_key}, "analyze")
                    profile = _read_json(hotword_file)
                    assessment = assess_hotword_profile(root, profile)
                    profile["assessment"] = assessment
                    profile["approved_hotwords"] = list(assessment["selected"])
                    video["hotword_file"] = str(hotword_file)
                    video["hotword_assessment"] = assessment
                    _mark_completed(api, "analyze", f"已判断为 {assessment['category']}")
                    if assessment["auto_confirmed"]:
                        profile["status"] = "ready"
                        profile["confirmation"] = "auto"
                        profile["hotwords"] = list(assessment["selected"])
                        _write_json(hotword_file, profile)
                        video["hotword_status"] = "auto_confirmed"
                        _mark_completed(
                            api,
                            "confirm",
                            "领域词库稳定，仅检查新增词" if assessment["saturated_domain"] else "已根据样本证据自动确认",
                        )
                        api.activity(
                            f"专业词汇已自动确认：{assessment['category']} · {len(assessment['selected_terms'])} 个词"
                        )
                    else:
                        profile["status"] = "needs_confirmation"
                        profile["confirmation"] = "pending"
                        _write_json(hotword_file, profile)
                        video["status"] = "needs_confirmation"
                        video["hotword_status"] = "needs_confirmation"
                        api._set_stage("confirm", "needs_confirmation", 0, "专业词汇存在异常，等待统一确认")
                        api.activity(f"{video.get('name')} 的专业词汇需要人工确认，批次继续处理其他视频", "warning")
                        api._persist_task()
                        continue

                raw_dir = video_work / "transcript"
                transcript_file = _find_transcript(raw_dir)
                if transcript_file:
                    video["transcript_file"] = str(transcript_file)
                    _mark_skipped(api, "transcribe", "已复用完整转录")
                else:
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    api._set_stage("transcribe", "running", 0, "正在从 0 秒开始全文转录")
                    command = [
                        str(python), str(transcriber), copied["path"],
                        "--output", str(raw_dir),
                        "--model", str(api.app_config.get("default_model") or "medium"),
                        "--device", str(api.app_config.get("default_device") or "auto"),
                        "--language", api.language,
                        "--hotword-reuse-file", str(hotword_file),
                        "--gui-events",
                    ]
                    api._run_subprocess(command, {}, "transcribe")
                    transcript_file = _find_transcript(raw_dir)
                    if transcript_file is None:
                        raise RuntimeError("全文转录没有生成可恢复的结构化结果")
                    video["transcript_file"] = str(transcript_file)
                    _mark_completed(api, "transcribe", "全文转录完成")
                    api.activity("全文转录完成")
                    api._persist_task()

                verified_dir = video_work / "verified"
                verified_file = _find_verified(verified_dir)
                if verified_file:
                    video["verified_file"] = str(verified_file)
                    _mark_skipped(api, "verify", "已复用可信校对结果")
                else:
                    verified_dir.mkdir(parents=True, exist_ok=True)
                    api._set_stage("verify", "running", 0, "正在进行整文件可信校对")
                    command = [
                        str(python), str(reviewer), str(raw_dir),
                        "--output-dir", str(verified_dir),
                        "--corrector-provider", "openai",
                        "--corrector-model", api.api_model,
                        "--corrector-base-url", api.api_base_url,
                        "--context-window", str(api.context_window),
                        "--gui-events",
                        "--pause-file", str(video_work / "review.pause"),
                    ]
                    api._run_subprocess(command, {"CORRECTOR_API_KEY": api.api_key}, "verify")
                    verified_file = _find_verified(verified_dir)
                    if verified_file is None:
                        raise RuntimeError("可信校对没有生成可恢复的结果")
                    video["verified_file"] = str(verified_file)
                    _mark_completed(api, "verify", "可信校对完成")
                    api.activity("可信校对完成；未确认内容不会进入知识库")
                    api._persist_task()

                if not video.get("hotword_feedback"):
                    video["hotword_feedback"] = learn_from_verified(
                        root,
                        str(copied.get("video_id") or video.get("video_id") or ""),
                        profile,
                        assessment,
                        verified_file,
                    )
                    feedback = video["hotword_feedback"]
                    state = "领域热词已稳定" if feedback["saturated"] else "领域热词继续学习"
                    api.activity(
                        f"{state} · 专业词纠正 {feedback['professional_corrections']} 处 · 新词 {feedback['new_terms']} 个"
                    )
                    api._persist_task()

                published_marker = video_work / "published.json"
                published = _read_json(published_marker)
                indexed = [item for item in load_index(root) if item.get("video_id") == video.get("video_id")]
                if published.get("knowledge_count") and indexed:
                    _mark_skipped(api, "publish", "已复用生成的知识")
                    _mark_skipped(api, "write", "索引和 Obsidian 已写入")
                else:
                    api._set_stage("publish", "running", 20, "正在整理 LLM Wiki")
                    published = publish_verified(
                        root,
                        Path(copied["path"]),
                        verified_file,
                        api._client(),
                        domain_hint=str(assessment.get("category") or profile.get("category") or ""),
                        raw_transcript_path=transcript_file,
                    )
                    _write_json(published_marker, published)
                    _mark_completed(api, "publish", f"生成 {published['knowledge_count']} 个知识点")
                    _mark_completed(api, "write", "已写入索引和 Obsidian")
                video.update(
                    status="completed",
                    wiki=published.get("wiki") or (indexed[0].get("obsidian_path") if indexed else ""),
                    knowledge_count=int(published.get("knowledge_count") or len(indexed)),
                    completed_at=_iso_now(),
                )
                api.activity(f"知识生成完成：{video.get('name')} · {video['knowledge_count']} 个知识点")
                api._persist_task()
                api.notify("video_done", force=True)
            except Exception as exc:
                if api.cancel_requested:
                    video["status"] = "interrupted"
                    video["message"] = "任务已取消，可从断点继续"
                    api._persist_task()
                    break
                video["status"] = "failed"
                video["message"] = f"{type(exc).__name__}: {exc}"
                for stage in api.task.get("stages", []):
                    if stage.get("id") == api.task.get("current_stage"):
                        stage["status"] = "failed"
                        stage["message"] = str(exc)
                api.activity(f"{video.get('name')} 处理失败，已保留断点并继续其他视频：{exc}", "error")
                api._persist_task()
                continue

        pending = [item for item in api.task.get("videos", []) if item.get("status") == "needs_confirmation"]
        failed = [item for item in api.task.get("videos", []) if item.get("status") in {"failed", "interrupted"}]
        completed = [item for item in api.task.get("videos", []) if item.get("status") == "completed"]
        api.task["completed"] = len(completed)
        api.task["failed"] = len(failed)
        api.task["pending_confirmation"] = len(pending)
        if api.cancel_requested:
            api.task["status"] = "cancelled"
            api.task["status_text"] = "任务已取消，已完成阶段均已保存，可稍后继续"
        elif pending:
            api.task["status"] = "needs_attention"
            api.task["status_text"] = f"其余视频已继续处理，还有 {len(pending)} 个视频需要确认专业词汇"
            _select_pending_confirmation(api)
        elif failed:
            api.task["status"] = "failed"
            api.task["status_text"] = f"{len(completed)} 个视频完成，{len(failed)} 个视频可从断点重试"
        else:
            api.task["status"] = "completed"
            api.task["overall_progress"] = 100.0
            api.task["status_text"] = "全部视频知识已生成"
            api.task["completed_at"] = _iso_now()
        api.running = False
        api.paused = False
        api._persist_task()
        api.notify("task_done", message=api.task["status_text"], force=True)
    except Exception as exc:
        api.running = False
        api.paused = False
        api.task["status"] = "interrupted"
        api.task["status_text"] = f"任务意外中断，可从断点继续：{exc}"
        api._persist_task()
        api.notify("task_error", message=api.task["status_text"], level="error", force=True)


def prepare_manual_confirmation(api: Any, words: list[str], category: str = "") -> int:
    """Persist a manual decision for the currently displayed pending video."""
    if not api.task:
        raise ValueError("没有等待确认的任务")
    index = int(api.task.get("hotword_profile", {}).get("video_index", api.task.get("current_index", 0)))
    videos = api.task.get("videos", [])
    if index < 0 or index >= len(videos) or videos[index].get("status") != "needs_confirmation":
        raise ValueError("当前没有等待确认的专业词汇")
    video = videos[index]
    hotword_file = Path(str(video.get("hotword_file") or ""))
    profile = _read_json(hotword_file)
    existing = {
        str(item.get("term") or "").casefold(): item
        for item in profile.get("hotwords", [])
        if isinstance(item, dict) and item.get("term")
    }
    selected = []
    for term in words:
        record = existing.get(term.casefold(), {"term": term, "aliases": [], "evidence": "用户确认"})
        selected.append(
            {
                "term": term,
                "aliases": list(record.get("aliases") or []),
                "source": "manual",
            }
        )
    assessment = profile.get("assessment") if isinstance(profile.get("assessment"), dict) else {}
    confirmed_category = normalize_category(category or assessment.get("category") or profile.get("category"))
    assessment.update(
        category=confirmed_category,
        auto_confirmed=False,
        selected=selected,
        selected_terms=words,
        manual_reasons=[],
    )
    profile.update(
        status="ready",
        confirmation="manual",
        category=confirmed_category,
        hotwords=selected,
        approved_hotwords=selected,
        assessment=assessment,
    )
    _write_json(hotword_file, profile)
    video["status"] = "interrupted"
    video["hotword_status"] = "manual_confirmed"
    video["hotword_assessment"] = assessment
    api.task["hotword_profile"] = None
    api._persist_task()
    return index
