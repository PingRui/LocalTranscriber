from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import webview

from app_config import MODEL_LABELS, SUPPORTED_MODELS, load_config, save_config, state_dir
from knowledge_space import (
    MEDIA_EXTENSIONS,
    OBSIDIAN_FOLDER,
    WORK_FOLDER,
    answer_question,
    clean_task_work,
    create_task,
    discover_videos,
    initialize_space,
    load_index,
    load_latest_resumable_task,
    reconcile_space_metadata,
    relink_video,
    space_paths,
    write_task,
)
from knowledge_pipeline import prepare_manual_confirmation, run_resumable_task
from llm_client import OpenAICompatibleClient, normalize_openai_base_url
from model_provider_config import load_provider_settings, save_provider_settings


APP_DIR = Path(__file__).resolve().parent
EXECUTABLE = Path(sys.executable).resolve()
PYTHON = EXECUTABLE.with_name("python.exe") if EXECUTABLE.name.casefold() == "pythonw.exe" else EXECUTABLE
TRANSCRIBER = APP_DIR / "transcribe.py"
KNOWLEDGE_WORKER = APP_DIR / "knowledge_worker.py"
WHOLE_FILE_REVIEW = APP_DIR / "whole_file_review.py"
UI_FILE = APP_DIR / "ui" / "index.html"
ICON_FILE = APP_DIR / "assets" / "localtranscriber-icon.ico"
STATE_DIR = state_dir()
APP_SETTINGS_FILE = STATE_DIR / "knowledge-app.json"
MODEL_PROVIDER_SETTINGS_FILE = STATE_DIR / "model-providers.json"
LOG_FILE = STATE_DIR / "last_run.log"
EVENT_PREFIX = "@@LOCAL_TRANSCRIBER_EVENT@@"
MEDIA_DIALOG_TYPES = (
    "视频和音频 (*.mp4;*.mov;*.mkv;*.avi;*.webm;*.mp3;*.wav;*.m4a;*.aac;*.flac)",
    "所有文件 (*.*)",
)
STAGES = (
    ("copy", "复制视频"),
    ("analyze", "分析样本"),
    ("confirm", "确认专业词汇"),
    ("transcribe", "全文转录"),
    ("verify", "可信校对"),
    ("publish", "生成知识"),
    ("write", "写入知识空间"),
)
INSTANCE_MUTEX_NAME = "Local\\LocalTranscriber.Knowledge"
_INSTANCE_MUTEX_HANDLE: int | None = None


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def configure_app_identity() -> None:
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LocalTranscriber.Knowledge")


def acquire_single_instance(name: str = INSTANCE_MUTEX_NAME) -> bool:
    global _INSTANCE_MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return False
    _INSTANCE_MUTEX_HANDLE = int(handle)
    return True


def release_single_instance() -> None:
    global _INSTANCE_MUTEX_HANDLE
    handle = _INSTANCE_MUTEX_HANDLE
    _INSTANCE_MUTEX_HANDLE = None
    if sys.platform == "win32" and handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def set_process_suspended(pid: int, suspended: bool) -> bool:
    if sys.platform != "win32":
        return False
    process_suspend_resume = 0x0800
    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(process_suspend_resume, False, pid)
    if not handle:
        return False
    try:
        action = ntdll.NtSuspendProcess if suspended else ntdll.NtResumeProcess
        action.argtypes = (ctypes.c_void_p,)
        action.restype = ctypes.c_long
        return action(handle) == 0
    finally:
        kernel32.CloseHandle(handle)


def format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return fallback


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class KnowledgeApi:
    def __init__(self, initial_files: tuple[str, ...] = ()) -> None:
        self.window: webview.Window | None = None
        self.lock = threading.RLock()
        self.app_config = load_config()
        saved = read_json(APP_SETTINGS_FILE, {})
        self.space_root = ""
        self._preferred_space_root = str(saved.get("space_root") or "")
        self._knowledge_count = 0
        self._index_mtime_ns: int | None = None
        self._integrity_status = "idle"
        self.language = str(saved.get("language") or "auto")
        self.temp_policy = "manual"
        self.recent_spaces = [str(item) for item in saved.get("recent_spaces", []) if str(item).strip()][:12]
        provider = load_provider_settings(MODEL_PROVIDER_SETTINGS_FILE).get("corrector", {})
        self.api_base_url = str(provider.get("base_url") or "")
        self.api_model = str(provider.get("model") or "")
        self.api_key = str(provider.get("api_key") or "")
        self.api_verified_at = str(provider.get("verified_at") or "")
        self.context_window = int(provider.get("context_window") or 128_000)
        self.queue: list[dict[str, Any]] = []
        self.task: dict[str, Any] | None = None
        self.running = False
        self.paused = False
        self.cancel_requested = False
        self.process: subprocess.Popen[str] | None = None
        self.activities: list[dict[str, str]] = []
        self.messages: list[dict[str, Any]] = []
        self.chat_running = False
        self._last_notify = 0.0
        if initial_files:
            self._add_discovered(discover_videos(initial_files))

    def attach_window(self, window: webview.Window) -> None:
        self.window = window

    def save_app_settings(self) -> None:
        write_json(
            APP_SETTINGS_FILE,
            {
                "version": 2,
                "space_root": self.space_root or self._preferred_space_root,
                "recent_spaces": self.recent_spaces,
                "language": self.language,
                "temp_policy": self.temp_policy,
            },
        )

    def api_ready(self) -> bool:
        return bool(self.api_base_url and self.api_model and self.api_key and self.api_verified_at)

    def _space_summary(self) -> dict[str, Any]:
        if not self.space_root:
            return {"ready": False, "root": "", "name": "", "knowledge_count": 0}
        root = Path(self.space_root)
        return {
            "ready": True,
            "root": str(root),
            "name": root.name,
            "knowledge_count": self._knowledge_count,
            "obsidian": str(root / OBSIDIAN_FOLDER),
            "integrity": self._integrity_status,
        }

    def _remember_index_state(self, root: Path, knowledge_count: int) -> None:
        self._knowledge_count = max(0, int(knowledge_count))
        try:
            self._index_mtime_ns = space_paths(root)["index"].stat().st_mtime_ns
        except OSError:
            self._index_mtime_ns = None

    def _refresh_index_state(self) -> None:
        if not self.space_root:
            return
        root = Path(self.space_root)
        index_path = space_paths(root)["index"]
        try:
            current_mtime_ns = index_path.stat().st_mtime_ns
            if self._index_mtime_ns == current_mtime_ns:
                return
            knowledge_count = len(load_index(root))
        except (OSError, UnicodeError, ValueError):
            return
        self._knowledge_count = knowledge_count
        self._index_mtime_ns = current_mtime_ns

    def _check_space_integrity(self, root: Path, entries: list[dict[str, Any]]) -> None:
        try:
            result = reconcile_space_metadata(root, entries)
        except (OSError, UnicodeError, ValueError):
            result = {"ok": False, "migration_required": False, "projection_stale": True}
        if self.space_root != str(root):
            return
        if result.get("migration_required"):
            self._integrity_status = "migration_pending"
            self.activity("检测到旧版知识数据；将在下次知识写入或显式修复时迁移", "warning")
        elif result.get("projection_stale"):
            self._integrity_status = "projection_stale"
            self.activity("知识索引可用，Obsidian 投影需要按需更新", "warning")
        else:
            self._integrity_status = "ready"
        self.notify("space_integrity", force=True)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            task = json.loads(json.dumps(self.task, ensure_ascii=False)) if self.task else None
            return {
                "space": self._space_summary(),
                "recent_spaces": list(self.recent_spaces),
                "queue": [dict(item, size=format_size(int(item.get("size_bytes", 0)))) for item in self.queue],
                "task": task,
                "running": self.running,
                "paused": self.paused,
                "activities": list(self.activities[-80:]),
                "ai": {
                    "base_url": self.api_base_url,
                    "model": self.api_model,
                    "has_key": bool(self.api_key),
                    "verified": self.api_ready(),
                    "verified_at": self.api_verified_at,
                    "context_window": self.context_window,
                },
                "runtime": {
                    "model": str(self.app_config.get("default_model") or "medium"),
                    "model_options": [
                        {"value": name, "label": MODEL_LABELS.get(name, name)} for name in SUPPORTED_MODELS
                    ],
                    "device": str(self.app_config.get("default_device") or "auto"),
                    "language": self.language,
                    "temp_policy": self.temp_policy,
                },
                "chat": {"running": self.chat_running, "messages": list(self.messages)},
            }

    def response(self, ok: bool = True, message: str | None = None, error: str | None = None, **extra: Any) -> dict[str, Any]:
        return {"ok": ok, "message": message, "error": error, "snapshot": self.snapshot(), **extra}

    def notify(self, event_type: str, message: str | None = None, level: str = "info", force: bool = False) -> None:
        if event_type in {"video_done", "task_done"}:
            self._refresh_index_state()
        window = self.window
        if window is None:
            return
        now = time.monotonic()
        if not force and event_type.endswith("_progress") and now - self._last_notify < 0.12:
            return
        self._last_notify = now
        payload = {
            "type": event_type,
            "message": message,
            "level": level,
            "snapshot": self.snapshot(),
        }
        try:
            window.evaluate_js(
                f"window.LocalTranscriber && window.LocalTranscriber.receive({json.dumps(payload, ensure_ascii=False)})"
            )
        except Exception:
            pass

    def activity(self, text: str, level: str = "info") -> None:
        with self.lock:
            self.activities.append({"time": datetime.now().strftime("%H:%M:%S"), "text": text, "level": level})
            self.activities = self.activities[-100:]

    def bootstrap(self) -> dict[str, Any]:
        return self.response()

    def open_folder_dialog(self, initial: str = "") -> Any:
        if self.window is None:
            return None
        options: dict[str, str] = {}
        candidate = str(initial or "").strip()
        if candidate:
            try:
                path = Path(candidate).expanduser()
                if path.is_dir():
                    options["directory"] = str(path.resolve())
            except (OSError, TypeError, ValueError):
                pass
        try:
            return self.window.create_file_dialog(webview.FileDialog.FOLDER, **options)
        except (OSError, TypeError, ValueError):
            if options:
                return self.window.create_file_dialog(webview.FileDialog.FOLDER)
            raise

    def choose_space(self) -> dict[str, Any]:
        if self.running:
            return self.response(False, error="任务进行中，暂时不能切换知识空间。")
        if self.chat_running:
            return self.response(False, error="上一条问答仍在处理中，暂时不能切换知识空间。")
        try:
            selected = self.open_folder_dialog(self.space_root or self._preferred_space_root)
        except (OSError, TypeError, ValueError) as exc:
            return self.response(False, error=f"无法打开文件夹选择窗口：{exc}")
        if not selected:
            return self.response()
        return self.open_space(str(selected[0]))

    def open_space(self, path: str) -> dict[str, Any]:
        if self.running:
            return self.response(False, error="任务进行中，暂时不能切换知识空间。")
        if self.chat_running:
            return self.response(False, error="上一条问答仍在处理中，暂时不能切换知识空间。")
        try:
            root = Path(path).expanduser().resolve()
            initialize_space(root)
            entries = load_index(root)
            knowledge_count = len(entries)
            task = load_latest_resumable_task(root)
        except (OSError, UnicodeError, ValueError) as exc:
            return self.response(False, error=f"无法打开知识空间：{exc}")
        with self.lock:
            self.space_root = str(root)
            self._preferred_space_root = str(root)
            self._remember_index_state(root, knowledge_count)
            self._integrity_status = "checking"
            self.recent_spaces = [str(root), *[item for item in self.recent_spaces if os.path.normcase(item) != os.path.normcase(str(root))]][:12]
            self.queue.clear()
            self.messages.clear()
            self.task = task
            self.save_app_settings()
        if self.window is not None:
            threading.Thread(target=self._check_space_integrity, args=(root, entries), daemon=True).start()
        message = f"已打开知识空间：{root.name}"
        if self.task:
            message += "，发现可继续的任务"
        return self.response(message=message)

    def remove_recent_space(self, path: str) -> dict[str, Any]:
        target = str(path or "").strip()
        if not target:
            return self.response(False, error="最近知识空间路径无效。")
        try:
            target_key = os.path.normcase(str(Path(target).expanduser().resolve()))
        except (OSError, TypeError, ValueError):
            target_key = os.path.normcase(target)
        remaining = []
        removed = False
        for item in self.recent_spaces:
            try:
                item_key = os.path.normcase(str(Path(item).expanduser().resolve()))
            except (OSError, TypeError, ValueError):
                item_key = os.path.normcase(item)
            if item_key == target_key:
                removed = True
            else:
                remaining.append(item)
        if not removed:
            return self.response(message="该目录已不在最近列表中")
        self.recent_spaces = remaining
        self.save_app_settings()
        return self.response(message="已从最近列表移除；磁盘文件未删除")

    def choose_videos(self) -> dict[str, Any]:
        if self.window is None:
            return self.response(False, error="桌面窗口尚未就绪，暂时不能添加视频。")
        with self.lock:
            if self.running and not self.paused:
                return self.response(False, error="请先暂停当前任务，再追加视频。")
            active_task_id = str(self.task.get("task_id") or "") if self.running and self.paused and self.task else ""
        selected = self.window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=MEDIA_DIALOG_TYPES,
        )
        if not selected:
            return self.response()
        added = self._add_discovered(discover_videos(selected), active_task_id=active_task_id)
        if not added:
            return self.response(message="没有新增视频，重复文件已自动跳过")
        message = f"已追加 {added} 个视频到当前任务末尾" if active_task_id else f"已添加 {added} 个视频"
        return self.response(message=message)

    def choose_video_folder(self) -> dict[str, Any]:
        with self.lock:
            if self.running and not self.paused:
                return self.response(False, error="请先暂停当前任务，再追加文件夹。")
            active_task_id = str(self.task.get("task_id") or "") if self.running and self.paused and self.task else ""
        try:
            selected = self.open_folder_dialog()
        except (OSError, TypeError, ValueError) as exc:
            return self.response(False, error=f"无法打开文件夹选择窗口：{exc}")
        if not selected:
            return self.response()
        discovered = discover_videos([selected[0]])
        added = self._add_discovered(discovered, active_task_id=active_task_id)
        duplicates = len(discovered) - added
        action = "追加到当前任务" if active_task_id else "新增"
        return self.response(
            message=f"递归发现 {len(discovered)} 个视频，{action} {added} 个" + (f"，跳过 {duplicates} 个重复项" if duplicates else "")
        )

    def _add_discovered(self, values: list[dict[str, Any]], active_task_id: str = "") -> int:
        with self.lock:
            task = self.task if self.task and str(self.task.get("task_id") or "") == active_task_id else None
            existing = {os.path.normcase(str(item["source"])) for item in self.queue}
            if task:
                existing.update(os.path.normcase(str(item.get("source") or "")) for item in task.get("videos", []))
            added_items: list[dict[str, Any]] = []
            for item in values:
                key = os.path.normcase(str(item["source"]))
                if key in existing:
                    continue
                normalized = dict(item)
                self.queue.append(normalized)
                added_items.append(normalized)
                existing.add(key)
            added = len(added_items)
            if task and added_items:
                task.setdefault("videos", []).extend(
                    {
                        **item,
                        "status": "waiting",
                        "stage": "copy",
                        "progress": 0.0,
                        "message": "运行中追加，等待开始",
                    }
                    for item in added_items
                )
                task.pop("completed_at", None)
                self._update_overall()
                self.activity(f"暂停时追加 {added} 个视频，已保存到当前任务末尾")
                self._persist_task()
        return added

    def remove_video(self, source: str) -> dict[str, Any]:
        if self.running:
            return self.response(False, error="任务进行中，不能修改队列。")
        key = os.path.normcase(str(Path(source).expanduser().resolve()))
        before = len(self.queue)
        self.queue = [item for item in self.queue if os.path.normcase(str(item["source"])) != key]
        return self.response(message="已从待处理列表移除" if len(self.queue) < before else None)

    def clear_queue(self) -> dict[str, Any]:
        if self.running:
            return self.response(False, error="任务进行中，不能清空队列。")
        self.queue.clear()
        return self.response()

    def test_and_save_ai(self, base_url: str, model: str, api_key: str, context_window: Any) -> dict[str, Any]:
        key = str(api_key or "").strip() or self.api_key
        try:
            window = max(8_000, int(context_window))
            client = OpenAICompatibleClient(
                base_url=normalize_openai_base_url(base_url),
                model=str(model).strip(),
                api_key=key,
                allow_remote=True,
                timeout=90,
            )
            client.test_chat_completion()
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            self.api_verified_at = ""
            return self.response(False, error=f"连接测试失败：{exc}")
        verified_at = iso_now()
        payload = {
            "provider": "openai",
            "base_url": client.base_url,
            "model": client.model,
            "api_key": key,
            "verified_at": verified_at,
            "context_window": window,
        }
        save_provider_settings(MODEL_PROVIDER_SETTINGS_FILE, {"corrector": payload, "verifier": payload})
        self.api_base_url = client.base_url
        self.api_model = client.model
        self.api_key = key
        self.api_verified_at = verified_at
        self.context_window = window
        return self.response(message="OpenAI 兼容接口已连接并保存")

    def save_runtime_settings(self, model: str, device: str, language: str, temp_policy: str) -> dict[str, Any]:
        if model not in SUPPORTED_MODELS:
            return self.response(False, error="转写模型无效")
        if device not in {"auto", "cuda", "cpu"}:
            return self.response(False, error="计算设备无效")
        if language not in {"auto", "zh", "en", "ja", "ko"}:
            return self.response(False, error="转写语言无效")
        if temp_policy != "manual":
            return self.response(False, error="临时文件策略无效")
        self.app_config["default_model"] = model
        self.app_config["default_device"] = device
        save_config(self.app_config)
        self.language = language
        self.temp_policy = temp_policy
        self.save_app_settings()
        return self.response(message="设置已保存")

    def _new_stage_state(self) -> list[dict[str, Any]]:
        return [{"id": stage_id, "label": label, "status": "waiting", "progress": 0.0, "message": ""} for stage_id, label in STAGES]

    def _set_stage(self, stage_id: str, status: str, progress: float = 0.0, message: str = "") -> None:
        if not self.task:
            return
        for stage in self.task["stages"]:
            if stage["id"] == stage_id:
                stage.update(status=status, progress=round(max(0.0, min(progress, 100.0)), 2), message=message)
                break
        self.task["current_stage"] = stage_id
        self.task["status_text"] = message or next(label for key, label in STAGES if key == stage_id)
        self._update_overall()
        self._persist_task()

    def _update_overall(self) -> None:
        if not self.task:
            return
        total = max(1, len(self.task.get("videos", [])))
        completed = sum(item.get("status") == "completed" for item in self.task.get("videos", []))
        stage_values = [float(item.get("progress", 0.0)) for item in self.task.get("stages", [])]
        current_fraction = sum(stage_values) / (len(stage_values) * 100) if stage_values else 0.0
        current_index = int(self.task.get("current_index", 0))
        current_videos = self.task.get("videos", [])
        current_done = 0 <= current_index < len(current_videos) and current_videos[current_index].get("status") == "completed"
        self.task["overall_progress"] = round((completed + (0.0 if current_done else current_fraction)) / total * 100, 2)

    def _persist_task(self) -> None:
        if not self.task or not self.space_root:
            return
        try:
            write_task(Path(self.space_root), self.task)
        except (OSError, ValueError):
            pass

    def start_knowledge_task(self) -> dict[str, Any]:
        if self.running:
            return self.response(False, error="已有知识生成任务正在运行。")
        if not self.space_root:
            return self.response(False, error="请先选择知识空间。")
        if not self.queue:
            return self.response(False, error="请先选择视频或视频文件夹。")
        if not self.api_ready():
            return self.response(False, error="请先在设置中测试并保存 OpenAI 兼容接口。")
        try:
            persisted = create_task(Path(self.space_root), [item["source"] for item in self.queue])
        except (OSError, ValueError) as exc:
            return self.response(False, error=f"无法创建任务：{exc}")
        with self.lock:
            self.task = {
                **persisted,
                "status": "running",
                "started_at": iso_now(),
                "current_index": 0,
                "current_stage": "copy",
                "status_text": "正在准备任务",
                "overall_progress": 0.0,
                "stages": self._new_stage_state(),
                "hotword_profile": None,
                "completed": 0,
                "failed": 0,
                "pending_confirmation": 0,
                "resume_version": 1,
            }
            self.running = True
            self.paused = False
            self.cancel_requested = False
            self.activities.clear()
            self.activity(f"任务开始，共 {len(self.queue)} 个视频")
            self._persist_task()
        threading.Thread(target=self._run_task_resumable, daemon=True).start()
        return self.response(message="已经开始生成视频知识")

    def _run_task_resumable(self) -> None:
        run_resumable_task(
            self,
            python=PYTHON,
            knowledge_worker=KNOWLEDGE_WORKER,
            transcriber=TRANSCRIBER,
            reviewer=WHOLE_FILE_REVIEW,
        )

    def continue_knowledge_task(self) -> dict[str, Any]:
        if self.running:
            return self.response(False, error="当前任务已经在运行。")
        if not self.task or self.task.get("status") not in {"interrupted", "cancelled", "failed", "needs_attention"}:
            return self.response(False, error="没有可以继续的任务。")
        unresolved = [item for item in self.task.get("videos", []) if item.get("status") == "needs_confirmation"]
        retryable = [item for item in self.task.get("videos", []) if item.get("status") in {"failed", "interrupted", "waiting", "processing"}]
        if unresolved and not retryable:
            return self.response(False, error="请先确认当前视频的专业词汇。")
        for video in self.task.get("videos", []):
            if video.get("status") == "failed":
                video["status"] = "interrupted"
        self.task["status"] = "running"
        self.task["status_text"] = "正在从已保存的阶段继续"
        self.running = True
        self.paused = False
        self.cancel_requested = False
        self.activity("从断点继续任务，已完成阶段不会重复执行")
        self._persist_task()
        threading.Thread(target=self._run_task_resumable, daemon=True).start()
        return self.response(message="已从断点继续任务")

    def _client(self) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            base_url=self.api_base_url,
            model=self.api_model,
            api_key=self.api_key,
            allow_remote=True,
            timeout=180,
        )

    def _run_subprocess(self, command: list[str], extra_env: dict[str, str], phase: str) -> None:
        env = os.environ.copy()
        env.update(extra_env)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(
            command,
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.process = process
        assert process.stdout is not None
        for raw_line in process.stdout:
            if self.cancel_requested:
                process.terminate()
                break
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX) :])
                except json.JSONDecodeError:
                    continue
                self._handle_worker_event(phase, event)
            else:
                LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                with LOG_FILE.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        code = process.wait()
        self.process = None
        if self.cancel_requested:
            raise RuntimeError("任务已取消")
        if code != 0:
            raise RuntimeError(f"{phase} 阶段执行失败，退出码 {code}")

    def _handle_worker_event(self, phase: str, event: dict[str, Any]) -> None:
        kind = str(event.get("event") or "")
        if phase == "analyze":
            if kind == "task_hotwords_sample_start":
                self._set_stage("analyze", "running", 20, "正在提取样本文字")
            elif kind == "task_hotwords_sample_done":
                self._set_stage("analyze", "running", 55, f"样本文字 {int(event.get('sample_chars', 0))} 字")
            elif kind == "task_hotwords_extracting":
                self._set_stage("analyze", "running", 75, "正在判断内容与专业词汇")
            elif kind == "task_hotwords_ready":
                self._set_stage("analyze", "running", 95, "专业词汇建议已生成")
        elif phase == "transcribe":
            if kind == "file_progress":
                value = float(event.get("progress", 0.0))
                self._set_stage("transcribe", "running", value, f"正在全文转录 · {value:.0f}%")
        elif phase == "verify":
            if kind == "clean_file_progress":
                value = float(event.get("progress", 0.0))
                stage = str(event.get("stage") or "")
                if stage == "review_chunk":
                    current = int(event.get("current", 0))
                    total = max(1, int(event.get("total", 1)) - 2)
                    detail = f"正在分段可信校对 · {min(current, total)}/{total}"
                elif stage == "global_consistency":
                    detail = "正在进行全局一致性检查"
                elif stage == "apply":
                    detail = "正在应用安全修正"
                else:
                    detail = f"正在可信校对 · {value:.0f}%"
                self._set_stage("verify", "running", value, detail)
        self.notify("task_progress")

    def confirm_hotwords(self, words: list[str] | str, category: str = "") -> dict[str, Any]:
        if not self.task or self.task.get("current_stage") != "confirm":
            return self.response(False, error="当前没有等待确认的专业词汇。")
        values = (
            [item.strip() for item in words.replace("，", ",").split(",") if item.strip()]
            if isinstance(words, str)
            else [str(item).strip() for item in words if str(item).strip()]
        )
        values = list(dict.fromkeys(values))[:80]
        try:
            prepare_manual_confirmation(self, values, category)
        except (OSError, ValueError) as exc:
            return self.response(False, error=f"无法保存专业词汇：{exc}")
        response = self.continue_knowledge_task()
        if response.get("ok"):
            response["message"] = f"已确认 {len(values)} 个专业词汇，并从断点继续"
        return response

    def pause_task(self) -> dict[str, Any]:
        if not self.running or self.paused:
            return self.response(False, error="当前任务不能暂停。")
        process = self.process
        if process is None or process.poll() is not None or not set_process_suspended(process.pid, True):
            return self.response(False, error="当前阶段暂不支持暂停。")
        self.paused = True
        self.activity("任务已暂停")
        return self.response(message="任务已暂停")

    def resume_task(self) -> dict[str, Any]:
        if not self.running or not self.paused:
            return self.response(False, error="当前任务没有暂停。")
        process = self.process
        if process is None or process.poll() is not None or not set_process_suspended(process.pid, False):
            return self.response(False, error="无法继续当前任务。")
        self.paused = False
        self.activity("任务已继续")
        return self.response(message="任务已继续")

    def cancel_task(self) -> dict[str, Any]:
        if not self.running:
            return self.response(False, error="当前没有运行中的任务。")
        self.cancel_requested = True
        process = self.process
        if process and process.poll() is None:
            try:
                if self.paused:
                    set_process_suspended(process.pid, False)
                process.terminate()
            except OSError:
                pass
        return self.response(message="正在取消任务，已完成成果会保留")

    def retry_failed_task(self) -> dict[str, Any]:
        return self.continue_knowledge_task()

    def cleanup_task(self) -> dict[str, Any]:
        if self.running:
            return self.response(False, error="任务进行中，不能清理临时文件。")
        if not self.task or self.task.get("status") not in {"completed", "cancelled", "failed"}:
            return self.response(False, error="没有可清理的已结束任务。")
        try:
            removed = clean_task_work(Path(self.space_root), str(self.task["task_id"]))
        except (OSError, ValueError) as exc:
            return self.response(False, error=f"清理失败：{exc}")
        if removed:
            self.activity("已清理本次任务临时文件")
        self.task = None
        self.queue.clear()
        return self.response(message="临时文件已清理；视频、索引和 Obsidian Wiki 均已保留")

    def ask_knowledge(self, question: str) -> dict[str, Any]:
        text = str(question or "").strip()
        if not text:
            return self.response(False, error="请输入问题。")
        if not self.space_root:
            return self.response(False, error="请先选择知识空间。")
        if not self.api_ready():
            return self.response(False, error="请先配置并测试 OpenAI 兼容接口。")
        with self.lock:
            if self.chat_running:
                return self.response(False, error="上一条问题仍在处理中。")
            conversation = [dict(item) for item in self.messages[-6:]]
            space_root = self.space_root
            self.messages.append({"role": "user", "content": text, "created_at": iso_now()})
            self.chat_running = True
        threading.Thread(target=self._answer_knowledge, args=(space_root, text, conversation), daemon=True).start()
        return self.response()

    def _answer_knowledge(self, space_root: str, question: str, conversation: list[dict[str, Any]]) -> None:
        try:
            result = answer_question(Path(space_root), question, self._client(), conversation=conversation)
            message = {
                "role": "assistant",
                "content": result["answer"],
                "citations": result["citations"],
                "created_at": iso_now(),
            }
            with self.lock:
                self.messages.append(message)
        except Exception as exc:
            with self.lock:
                self.messages.append({"role": "assistant", "content": f"检索失败：{exc}", "citations": [], "error": True, "created_at": iso_now()})
        finally:
            self.chat_running = False
            self.notify("chat_done", force=True)

    def clear_chat(self) -> dict[str, Any]:
        with self.lock:
            if self.chat_running:
                return self.response(False, error="上一条问答仍在处理中，暂时不能清空对话。")
            self.messages.clear()
        return self.response()

    def get_video_source(self, path: str, start: Any = 0) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            return self.response(False, error="视频文件不可用，请重新关联。")
        return self.response(uri=target.as_uri(), start=max(0.0, float(start)))

    def relink_missing_video(self, video_id: str) -> dict[str, Any]:
        if self.window is None or not self.space_root:
            return self.response(False, error="当前无法重新关联视频。")
        selected = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=MEDIA_DIALOG_TYPES)
        if not selected:
            return self.response()
        try:
            result = relink_video(Path(self.space_root), video_id, Path(selected[0]))
        except (OSError, ValueError, KeyError) as exc:
            return self.response(False, error=f"重新关联失败：{exc}")
        return self.response(message="视频已重新关联", path=result["path"])

    def on_closing(self) -> bool:
        if not self.running:
            return True
        if self.window is None:
            return False
        should_close = self.window.create_confirmation_dialog(
            "知识生成仍在进行",
            "关闭窗口会停止当前任务，已经完成的视频知识会保留。确定关闭吗？",
        )
        if should_close:
            self.cancel_task()
        return bool(should_close)


def smoke_test() -> None:
    if not UI_FILE.is_file():
        raise FileNotFoundError(UI_FILE)
    if not ICON_FILE.is_file():
        raise FileNotFoundError(ICON_FILE)
    html = UI_FILE.read_text(encoding="utf-8")
    script = (APP_DIR / "ui" / "app.js").read_text(encoding="utf-8")
    required_html = (
        "knowledgeGenerationView", "knowledgeChatView", "chooseVideoFolderButton",
        "taskStages", "appendVideosButton", "appendFolderButton", "aiBaseUrl", "testAiButton", "knowledgeComposer", "evidencePlayer",
    )
    for value in required_html:
        if value not in html:
            raise RuntimeError(f"界面缺少：{value}")
    required_script = (
        "window.LocalTranscriber", "start_knowledge_task", "confirm_hotwords",
        "ask_knowledge", "relink_missing_video", "cleanup_task",
    )
    for value in required_script:
        if value not in script:
            raise RuntimeError(f"前端逻辑缺少：{value}")
    KnowledgeApi()
    print("GUI_SMOKE_OK")


def main() -> None:
    if "--smoke-test" in sys.argv:
        smoke_test()
        return
    if not acquire_single_instance():
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, "LocalTranscriber 已在运行。", "LocalTranscriber", 0x40)
        return
    try:
        configure_app_identity()
        initial_files = tuple(item for item in sys.argv[1:] if not item.startswith("--"))
        api = KnowledgeApi(initial_files)
        window = webview.create_window(
            "LocalTranscriber 视频知识库",
            url=UI_FILE.resolve().as_uri(),
            js_api=api,
            width=1120,
            height=760,
            min_size=(900, 620),
            background_color="#FFFFFF",
            text_select=False,
        )
        assert window is not None
        api.attach_window(window)
        window.events.closing += api.on_closing
        webview.start(debug=False, private_mode=True, icon=str(ICON_FILE))
    finally:
        release_single_instance()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            LOG_FILE.write_text(f"GUI 启动失败：{type(exc).__name__}: {exc}\n", encoding="utf-8")
        except Exception:
            pass
        raise
