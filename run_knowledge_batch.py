from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
import time
from datetime import datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
GUI_PATH = APP_DIR / "gui.pyw"
RUNNER_STATE = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "LocalTranscriber" / "batch-runner.json"


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_gui_module() -> Any:
    loader = SourceFileLoader("localtranscriber_gui_batch", str(GUI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("无法加载桌面任务模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def task_summary(api: Any, *, phase: str, source_root: Path, message: str = "") -> dict[str, Any]:
    task = api.task or {}
    videos = [item for item in task.get("videos", []) if isinstance(item, dict)]
    counts: dict[str, int] = {}
    for video in videos:
        status = str(video.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    current_index = int(task.get("current_index") or 0)
    current = videos[current_index] if 0 <= current_index < len(videos) else {}
    return {
        "schema_version": 1,
        "phase": phase,
        "message": message or str(task.get("status_text") or ""),
        "source_root": str(source_root),
        "space_root": str(api.space_root or ""),
        "task_id": str(task.get("task_id") or ""),
        "task_status": str(task.get("status") or ""),
        "total": len(videos),
        "counts": counts,
        "current_index": current_index,
        "current_video": str(current.get("name") or task.get("current_video") or ""),
        "current_stage": str(task.get("current_stage") or ""),
        "overall_progress": float(task.get("overall_progress") or 0),
        "activities": list(api.activities[-20:]),
        "updated_at": iso_now(),
    }


def prepare_task(gui: Any, api: Any, source_root: Path) -> int:
    discovered = gui.discover_videos([source_root])
    if not discovered:
        raise ValueError(f"没有发现支持的视频：{source_root}")
    if not api.space_root:
        raise ValueError("尚未配置知识空间")
    if not api.api_ready():
        raise ValueError("OpenAI 兼容接口没有通过连接测试")
    if api.task and api.task.get("status") in {"running", "interrupted", "cancelled", "failed", "needs_attention"}:
        task_id = str(api.task.get("task_id") or "")
        added = api._add_discovered(discovered, active_task_id=task_id)
    else:
        persisted = gui.create_task(Path(api.space_root), [item["source"] for item in discovered])
        api.task = {
            **persisted,
            "status": "interrupted",
            "started_at": iso_now(),
            "current_index": 0,
            "current_stage": "copy",
            "status_text": "等待批量运行器启动",
            "overall_progress": 0.0,
            "stages": api._new_stage_state(),
            "hotword_profile": None,
            "completed": 0,
            "failed": 0,
            "pending_confirmation": 0,
            "resume_version": 1,
        }
        api._persist_task()
        added = len(discovered)
    return added


def auto_confirm_pending(gui: Any, api: Any) -> bool:
    videos = api.task.get("videos", []) if api.task else []
    pending = next(
        ((index, video) for index, video in enumerate(videos) if video.get("status") == "needs_confirmation"),
        None,
    )
    if pending is None:
        return False
    index, video = pending
    hotword_file = Path(str(video.get("hotword_file") or ""))
    try:
        profile = json.loads(hotword_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        profile = {}
    assessment = profile.get("assessment") if isinstance(profile.get("assessment"), dict) else {}
    selected = [
        str(item.get("term") or "").strip()
        for item in assessment.get("selected", [])
        if isinstance(item, dict) and str(item.get("term") or "").strip()
    ]
    api.task["current_index"] = index
    api.task["current_stage"] = "confirm"
    api.task["hotword_profile"] = {
        "video_index": index,
        "category": assessment.get("category") or profile.get("category") or "未分类",
        "hotwords": selected,
        "manual_reasons": list(assessment.get("manual_reasons") or []),
    }
    gui.prepare_manual_confirmation(
        api,
        selected,
        str(assessment.get("category") or profile.get("category") or "未分类"),
    )
    api.activity(f"批量运行器已采用证据通过的专业词继续：{video.get('name')}", "warning")
    return True


def _run_with_gui(gui: Any, source_root: Path) -> int:
    api = gui.KnowledgeApi()
    added = prepare_task(gui, api, source_root)
    write_json(RUNNER_STATE, task_summary(api, phase="starting", source_root=source_root, message=f"追加 {added} 个视频"))
    stop_watcher = threading.Event()

    def watcher() -> None:
        while not stop_watcher.wait(10):
            write_json(RUNNER_STATE, task_summary(api, phase="running", source_root=source_root))

    thread = threading.Thread(target=watcher, daemon=True)
    thread.start()
    retries: dict[str, int] = {}
    try:
        while True:
            if auto_confirm_pending(gui, api):
                pass
            for video in api.task.get("videos", []):
                if video.get("status") in {"failed", "processing"}:
                    video["status"] = "interrupted"
            api.task["status"] = "running"
            api.task["status_text"] = "批量运行器正在从断点继续"
            api.running = True
            api.paused = False
            api.cancel_requested = False
            api._persist_task()
            api._run_task_resumable()

            if api.task.get("status") == "completed":
                write_json(RUNNER_STATE, task_summary(api, phase="completed", source_root=source_root))
                return 0
            if api.task.get("status") == "needs_attention" and auto_confirm_pending(gui, api):
                continue
            failed = [item for item in api.task.get("videos", []) if item.get("status") in {"failed", "interrupted"}]
            if not failed:
                write_json(
                    RUNNER_STATE,
                    task_summary(api, phase="blocked", source_root=source_root, message="任务未完成且没有可重试视频"),
                )
                return 2
            retryable = []
            for video in failed:
                key = str(video.get("source") or video.get("name") or "")
                retries[key] = retries.get(key, 0) + 1
                if retries[key] <= 3:
                    video["status"] = "interrupted"
                    retryable.append(video)
            if not retryable:
                write_json(
                    RUNNER_STATE,
                    task_summary(api, phase="blocked", source_root=source_root, message="失败视频已自动重试 3 次"),
                )
                return 3
            api.activity(f"自动重试 {len(retryable)} 个失败视频", "warning")
            time.sleep(5)
    finally:
        stop_watcher.set()
        thread.join(timeout=2)


def run(source_root: Path) -> int:
    gui = load_gui_module()
    if not gui.acquire_single_instance():
        raise RuntimeError("LocalTranscriber 已在运行，请先关闭桌面程序或现有批处理任务")
    try:
        return _run_with_gui(gui, source_root)
    finally:
        gui.release_single_instance()


def main() -> int:
    parser = argparse.ArgumentParser(description="从持久断点批量生成本地视频知识")
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(source_root)
    try:
        return run(source_root)
    except Exception as exc:
        write_json(
            RUNNER_STATE,
            {
                "schema_version": 1,
                "phase": "crashed",
                "message": f"{type(exc).__name__}: {exc}",
                "source_root": str(source_root),
                "updated_at": iso_now(),
            },
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
