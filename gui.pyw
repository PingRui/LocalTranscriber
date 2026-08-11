from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import ctypes
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import webview

from app_config import MODEL_LABELS, SUPPORTED_MODELS, load_config, state_dir


APP_DIR = Path(__file__).resolve().parent
PYTHON = Path(sys.executable).resolve()
TRANSCRIBER = APP_DIR / "transcribe.py"
UI_FILE = APP_DIR / "ui" / "index.html"
ICON_FILE = APP_DIR / "assets" / "localtranscriber-icon.ico"
APP_CONFIG = load_config()
STATE_DIR = state_dir()
LOG_FILE = STATE_DIR / "last_run.log"
HISTORY_FILE = STATE_DIR / "history.json"
EVENT_PREFIX = "@@LOCAL_TRANSCRIBER_EVENT@@"
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".webm"}
MEDIA_DIALOG_TYPES = (
    "视频和音频 (*.mp4;*.mov;*.mkv;*.avi;*.mp3;*.wav;*.m4a;*.aac;*.flac;*.webm)",
    "所有文件 (*.*)",
)
ALLOWED_LANGUAGES = {"auto", "zh", "en", "ja", "ko"}
ALLOWED_DEVICES = {"auto", "cuda", "cpu"}
ALLOWED_MODELS = set(SUPPORTED_MODELS)
ALLOWED_CONTEXT_MODES = {"continuous", "isolated"}


def configure_app_identity() -> None:
    """Give the Windows taskbar a stable identity separate from pythonw.exe."""
    if sys.platform != "win32":
        return
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "LocalTranscriber.Desktop"
    )


def set_process_suspended(pid: int, suspended: bool) -> bool:
    """Suspend or resume the transcription worker on Windows."""
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


def path_key(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def format_file_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            digits = 0 if unit == "B" else 1
            return f"{value:.{digits}f} {unit}"
        value /= 1024
    return f"{size} B"


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class TranscriberApi:
    def __init__(self, initial_files: tuple[str, ...] = ()) -> None:
        self.window: webview.Window | None = None
        self.lock = threading.RLock()
        self.files: list[dict[str, Any]] = []
        self.result_dirs: dict[str, Path] = {}
        self.log_lines: list[str] = []
        self.process: subprocess.Popen[str] | None = None
        self.running = False
        self.paused = False
        self.scanning = False
        self.cancel_requested = False
        self.status_before_pause = ""
        self.status_text = "请添加需要转写的文件"
        self.progress = 0.0
        self.model_status = "模型待加载"
        self.model_loading = False
        self.output_path = ""
        self.default_model = str(APP_CONFIG["default_model"])
        self.default_device = str(APP_CONFIG["default_device"])
        self.hf_cache_dir = str(APP_CONFIG["hf_cache_dir"])
        self.history: dict[str, dict[str, Any]] = self.load_history()
        self.source_map_file: Path | None = None
        self.batch_summary = {"total": 0, "completed": 0, "failed": 0, "skipped": 0}
        self.add_paths(initial_files)

    def attach_window(self, window: webview.Window) -> None:
        self.window = window

    def file_record(self, value: str | Path) -> dict[str, Any]:
        path = Path(value).expanduser().resolve()
        try:
            size_label = format_file_size(path.stat().st_size)
        except OSError:
            size_label = ""
        return {
            "path": str(path),
            "name": path.name,
            "folder": str(path.parent),
            "size_label": size_label,
            "media_type": "audio" if path.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".flac"} else "video",
            "status": "等待中",
            "progress": 0.0,
        }

    def load_history(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        records = payload.get("items", []) if isinstance(payload, dict) else []
        history: dict[str, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, dict) or not record.get("path"):
                continue
            try:
                history[path_key(str(record["path"]))] = dict(record)
            except OSError:
                continue
        return history

    def save_history(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            items = sorted(
                self.history.values(),
                key=lambda item: str(item.get("updated_at", item.get("created_at", ""))),
                reverse=True,
            )[:500]
            HISTORY_FILE.write_text(
                json.dumps({"items": items}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def history_items(self) -> list[dict[str, Any]]:
        return sorted(
            (dict(item) for item in self.history.values()),
            key=lambda item: str(item.get("updated_at", item.get("created_at", ""))),
            reverse=True,
        )

    def sync_history(
        self,
        item: dict[str, Any],
        *,
        output_dir: str | Path | None = None,
        outputs: list[str] | None = None,
        persist: bool = False,
    ) -> None:
        key = path_key(item["path"])
        previous = self.history.get(key, {})
        record = {
            **previous,
            "path": item["path"],
            "name": item["name"],
            "folder": item["folder"],
            "size_label": item.get("size_label", ""),
            "media_type": item.get("media_type", "video"),
            "status": item.get("status", "等待中"),
            "progress": float(item.get("progress", 0.0)),
            "created_at": previous.get("created_at") or iso_now(),
            "updated_at": iso_now(),
        }
        if output_dir:
            record["result_dir"] = str(Path(output_dir).expanduser().resolve())
        if outputs is not None:
            record["outputs"] = [str(Path(value).expanduser().resolve()) for value in outputs]
        self.history[key] = record
        if persist:
            self.save_history()

    def add_paths(self, paths: tuple[str, ...] | list[str]) -> int:
        added = 0
        with self.lock:
            known = {path_key(item["path"]) for item in self.files}
            for value in paths:
                path = Path(value).expanduser().resolve()
                key = path_key(path)
                if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS or key in known:
                    continue
                self.files.append(self.file_record(path))
                known.add(key)
                added += 1
            if added:
                self.status_text = f"已添加 {len(self.files)} 个文件，可以开始转写"
        return added

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "files": [dict(item) for item in self.files],
                "history": self.history_items(),
                "running": self.running,
                "paused": self.paused,
                "scanning": self.scanning,
                "task_mode": "single" if len(self.files) == 1 else "batch",
                "status_text": self.status_text,
                "progress": self.progress,
                "logs": list(self.log_lines[-300:]),
                "result_dirs": {item["path"]: str(self.result_dirs[key]) for item in self.files if (key := path_key(item["path"])) in self.result_dirs},
                "model_status": self.model_status,
                "model_loading": self.model_loading,
                "default_model": self.default_model,
                "default_device": self.default_device,
                "output_path": self.output_path,
                "batch_summary": dict(self.batch_summary),
            }

    def response(self, ok: bool = True, **payload: Any) -> dict[str, Any]:
        return {"ok": ok, "snapshot": self.snapshot(), **payload}

    def notify(self, event_type: str, message: str | None = None, level: str = "info", **payload: Any) -> None:
        window = self.window
        if window is None:
            return
        event = {
            "type": event_type,
            "message": message,
            "level": level,
            "snapshot": self.snapshot(),
            **payload,
        }
        try:
            window.evaluate_js(f"window.LocalTranscriber && window.LocalTranscriber.receive({json.dumps(event, ensure_ascii=False)})")
        except Exception:
            # The window can disappear while a worker is completing.
            pass

    def bootstrap(self) -> dict[str, Any]:
        return self.response()

    def choose_files(self) -> dict[str, Any]:
        if self.running or self.scanning or self.window is None:
            return self.response(False, error="当前任务进行中，暂时不能添加文件。")
        paths = self.window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=MEDIA_DIALOG_TYPES,
        )
        if not paths:
            return self.response()
        added = self.add_paths(list(paths))
        message = f"已添加 {added} 个文件" if added else "没有新增支持的媒体文件"
        return self.response(message=message)

    def add_files(self, paths: list[str]) -> dict[str, Any]:
        if self.running or self.scanning:
            return self.response(False, error="当前任务进行中，暂时不能添加文件。")
        added = self.add_paths(paths)
        message = f"已添加 {added} 个文件" if added else "没有新增支持的媒体文件"
        return self.response(message=message)

    def choose_folder(self, auto_start: bool = False, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.running or self.scanning or self.window is None:
            return self.response(False, error="当前任务进行中，暂时不能扫描文件夹。")
        selected = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        if not selected:
            return self.response()
        folder = str(selected[0])
        with self.lock:
            self.scanning = True
            self.progress = 0.0
            self.status_text = f"正在扫描全部子文件夹：{folder}"
        threading.Thread(
            target=self.scan_folder,
            args=(folder, bool(auto_start), dict(settings or {})),
            daemon=True,
        ).start()
        return self.response(message="已开始扫描文件夹")

    def scan_folder(self, folder: str, auto_start: bool, settings: dict[str, Any]) -> None:
        found: list[str] = []
        errors: list[str] = []

        def record_error(error: OSError) -> None:
            errors.append(str(error))

        try:
            next_report = 100
            for directory, _subdirs, filenames in os.walk(folder, onerror=record_error, followlinks=False):
                for filename in filenames:
                    if Path(filename).suffix.lower() in MEDIA_EXTENSIONS:
                        found.append(str(Path(directory, filename).resolve()))
                if len(found) >= next_report:
                    with self.lock:
                        self.status_text = f"正在扫描全部子文件夹：已发现 {len(found)} 个媒体文件"
                    self.notify("scan_progress")
                    next_report += 100
            found.sort(key=str.casefold)
            before = len(self.files)
            self.add_paths(found)
            added = len(self.files) - before
            with self.lock:
                self.scanning = False
                self.status_text = (
                    f"扫描完成：发现 {len(found)} 个媒体文件，新加入 {added} 个"
                    if found
                    else "扫描完成，没有找到支持的视频或音频"
                )
                for error in errors[:10]:
                    self.append_log(error, notify=False)
            self.notify(
                "scan_done",
                message=(f"扫描完成，新加入 {added} 个文件" if found else "没有找到支持的媒体文件"),
            )
            if found and auto_start:
                self.start_transcription(settings)
        except Exception as exc:
            with self.lock:
                self.scanning = False
                self.status_text = "文件夹扫描失败"
            self.notify("scan_error", f"扫描失败：{type(exc).__name__}: {exc}", "error")

    def remove_files(self, paths: list[str]) -> dict[str, Any]:
        if self.running or self.scanning:
            return self.response(False, error="当前任务进行中，不能修改队列。")
        keys = {path_key(item) for item in paths}
        with self.lock:
            before = len(self.files)
            self.files = [item for item in self.files if path_key(item["path"]) not in keys]
            for key in keys:
                self.result_dirs.pop(key, None)
            removed = before - len(self.files)
            if not self.files:
                self.status_text = "请添加需要转写的文件"
        return self.response(message=f"已移除 {removed} 个文件" if removed else None)

    def clear_files(self) -> dict[str, Any]:
        if self.running or self.scanning:
            return self.response(False, error="当前任务进行中，不能清空队列。")
        with self.lock:
            self.files.clear()
            self.result_dirs.clear()
            self.progress = 0.0
            self.status_text = "请添加需要转写的文件"
        return self.response()

    def choose_output(self) -> dict[str, Any]:
        if self.running or self.scanning or self.window is None:
            return self.response(False, error="当前任务进行中，不能修改输出位置。")
        selected = self.window.create_file_dialog(webview.FileDialog.FOLDER, directory=self.output_path)
        if not selected:
            return self.response(path=self.output_path)
        with self.lock:
            self.output_path = str(Path(selected[0]).expanduser().resolve())
        return self.response(path=self.output_path)

    def normalize_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        language = str(settings.get("language", "auto")).lower()
        device = str(settings.get("device", self.default_device)).lower()
        output_mode = str(settings.get("output_mode", "source")).lower()
        output_path = str(settings.get("output_path", "")).strip()
        prompt = str(settings.get("prompt", "")).strip()
        source_url = str(settings.get("source_url", "")).strip()
        raw_source_urls = settings.get("source_urls", {})
        source_urls: dict[str, str] = {}
        if isinstance(raw_source_urls, dict):
            known_paths = {path_key(item["path"]): item["path"] for item in self.files}
            for raw_path, raw_url in raw_source_urls.items():
                try:
                    key = path_key(str(raw_path))
                except OSError:
                    continue
                url = str(raw_url).strip()
                if key in known_paths and url:
                    source_urls[known_paths[key]] = url
        if source_url and len(self.files) == 1:
            source_urls[self.files[0]["path"]] = source_url
        context_mode = str(settings.get("context_mode", "isolated")).lower()
        llm_repair = bool(settings.get("llm_repair", False))
        deepseek_api_key = str(settings.get("deepseek_api_key", "")).strip()
        model = str(settings.get("model", self.default_model)).lower()
        return {
            "model": model if model in ALLOWED_MODELS else self.default_model,
            "language": language if language in ALLOWED_LANGUAGES else "auto",
            "device": device if device in ALLOWED_DEVICES else "auto",
            "output_mode": output_mode if output_mode in {"source", "custom"} else "source",
            "output_path": output_path,
            "prompt": prompt,
            "source_url": source_url,
            "source_urls": source_urls,
            "context_mode": context_mode if context_mode in ALLOWED_CONTEXT_MODES else "isolated",
            "llm_repair": llm_repair,
            "deepseek_api_key": deepseek_api_key,
            "skip_existing": bool(settings.get("skip_existing", True)),
        }

    def start_transcription(self, raw_settings: dict[str, Any]) -> dict[str, Any]:
        if self.running or self.scanning:
            return self.response(False, error="已有任务正在运行。")
        if not self.files:
            return self.response(False, error="请先添加一个或多个视频、音频文件。")

        settings = self.normalize_settings(raw_settings)
        if settings["output_mode"] == "custom" and not settings["output_path"]:
            return self.response(False, error="请选择统一输出文件夹。")
        invalid_urls = [
            url for url in settings["source_urls"].values()
            if not url.lower().startswith(("http://", "https://"))
        ]
        if invalid_urls:
            return self.response(False, error="视频来源地址必须以 http:// 或 https:// 开头。")
        if settings["llm_repair"] and not settings["deepseek_api_key"]:
            return self.response(False, error="请填写 DeepSeek API Key，或关闭大模型校订。")

        missing = [item["path"] for item in self.files if not Path(item["path"]).is_file()]
        if missing:
            return self.response(False, error="部分文件已被移动或删除：\n" + "\n".join(missing[:8]))

        command = [
            str(PYTHON),
            "-u",
            str(TRANSCRIBER),
            "--gui-events",
            "--model",
            settings["model"],
            "--language",
            settings["language"],
            "--device",
            settings["device"],
        ]
        if settings["skip_existing"]:
            command.append("--skip-existing")
        if settings["output_mode"] == "custom":
            output = str(Path(settings["output_path"]).expanduser().resolve())
            command.extend(("--output", output))
            self.output_path = output
        if settings["prompt"]:
            command.extend(("--prompt", settings["prompt"]))
        if self.source_map_file:
            try:
                self.source_map_file.unlink(missing_ok=True)
            except OSError:
                pass
            self.source_map_file = None
        if settings["source_urls"]:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".json",
                prefix="localtranscriber-source-",
                delete=False,
            ) as source_file:
                json.dump(settings["source_urls"], source_file, ensure_ascii=False)
                self.source_map_file = Path(source_file.name)
            command.extend(("--source-url-map", str(self.source_map_file)))
        command.extend(("--context-mode", settings["context_mode"]))
        if settings["llm_repair"]:
            command.extend(("--llm-repair", "--llm-model", "deepseek-v4-flash"))
        command.extend(item["path"] for item in self.files)

        with self.lock:
            self.running = True
            self.paused = False
            self.cancel_requested = False
            self.status_before_pause = ""
            self.result_dirs.clear()
            self.log_lines.clear()
            self.progress = 0.0
            self.model_loading = True
            selected_model_label = MODEL_LABELS[settings["model"]]
            self.model_status = f"正在加载 {selected_model_label}"
            self.status_text = f"正在加载本地模型（{selected_model_label}）…"
            self.batch_summary = {"total": len(self.files), "completed": 0, "failed": 0, "skipped": 0}
            for item in self.files:
                item["status"] = "等待中"
                item["progress"] = 0.0
                item["source_url"] = settings["source_urls"].get(item["path"], "")
                self.sync_history(item)
            self.save_history()

        threading.Thread(
            target=self.run_process,
            args=(command, settings["deepseek_api_key"] if settings["llm_repair"] else ""),
            daemon=True,
        ).start()
        task_label = "单个转写" if len(self.files) == 1 else f"批量转写（{len(self.files)} 个文件）"
        self.notify("task_start")
        return self.response(message=f"{task_label}已开始")

    def run_process(self, command: list[str], deepseek_api_key: str = "") -> None:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["HF_HOME"] = self.hf_cache_dir
        if deepseek_api_key:
            env["DEEPSEEK_API_KEY"] = deepseek_api_key
        deepseek_api_key = ""
        code = -1
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.process = process
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if line.startswith(EVENT_PREFIX):
                    try:
                        self.handle_event(json.loads(line[len(EVENT_PREFIX) :]))
                    except json.JSONDecodeError:
                        self.append_log(line)
                else:
                    self.append_log(line)
            code = process.wait()
        except Exception as exc:
            self.append_log(f"启动失败：{type(exc).__name__}: {exc}")
        finally:
            self.process = None
            self.finish(code)

    def find_file(self, source: str) -> dict[str, Any] | None:
        key = path_key(source)
        return next((item for item in self.files if path_key(item["path"]) == key), None)

    def handle_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("event", ""))
        source = str(event.get("source", ""))
        with self.lock:
            item = self.find_file(source) if source else None
            if kind == "source_context_loading":
                self.model_loading = True
                self.model_status = "正在读取来源上下文"
                self.status_text = "正在从来源页面提取人物和专业词汇…"
            elif kind == "source_context_ready":
                title = str(event.get("title", ""))
                term_count = int(event.get("term_count", 0))
                self.model_status = f"来源上下文已就绪 · {term_count} 个词"
                self.status_text = f"已读取来源：{title}" if title else "来源上下文已就绪"
            elif kind == "source_context_error":
                self.model_status = "来源上下文不可用"
                self.status_text = "来源页面读取失败，将继续本地转写"
            elif kind == "file_retry":
                self.status_text = "检测到异常重复，正在自动使用安全模式重试…"
            elif kind == "llm_repair_start" and item:
                item["status"] = "大模型校订中"
                item["progress"] = 0.0
                self.progress = 0.0
                self.status_text = "正在使用 DeepSeek 结合上下文校订…"
            elif kind == "llm_repair_progress" and item:
                progress = float(event.get("progress", 0.0))
                item["status"] = "大模型校订中"
                item["progress"] = progress
                self.progress = progress
                self.status_text = f"DeepSeek 校订：{int(event.get('current', 0))}/{int(event.get('total', 0))}"
            elif kind == "llm_repair_done" and item:
                item["status"] = "校订完成"
                item["progress"] = 100.0
                self.progress = 100.0
            elif kind == "model_loading":
                model_name = str(event.get("model", "medium"))
                model_label = MODEL_LABELS.get(model_name, model_name)
                device = str(event.get("device", ""))
                self.model_loading = True
                self.model_status = f"正在加载 {model_label} · {device.upper()}"
                self.status_text = f"正在加载本地模型（{model_label} · {device}）…"
            elif kind == "model_ready":
                model_name = str(event.get("model", "medium"))
                model_label = MODEL_LABELS.get(model_name, model_name)
                device = str(event.get("device", ""))
                self.model_loading = False
                self.model_status = f"{model_label} · {device.upper()}"
                self.status_text = "模型已就绪，准备转写"
            elif kind == "file_start" and item:
                item["status"] = "转写中"
                item["progress"] = 0.0
                self.progress = 0.0
                self.status_text = f"正在转写：{item['name']}"
            elif kind == "file_progress" and item:
                progress = float(event.get("progress", 0.0))
                item["status"] = "转写中"
                item["progress"] = progress
                self.progress = progress
                timestamp = str(event.get("timestamp", ""))
                self.status_text = f"正在转写：{item['name']}  {progress:.1f}%  {timestamp}"
            elif kind == "file_done" and item:
                item["status"] = "已完成"
                item["progress"] = 100.0
                self.progress = 100.0
                output_dir = event.get("output_dir")
                if output_dir:
                    self.result_dirs[path_key(source)] = Path(str(output_dir))
                self.sync_history(
                    item,
                    output_dir=str(output_dir) if output_dir else None,
                    outputs=[str(value) for value in event.get("outputs", [])],
                    persist=True,
                )
            elif kind == "file_skipped" and item:
                item["status"] = "已有结果"
                item["progress"] = 100.0
                output_dir = event.get("output_dir")
                if output_dir:
                    self.result_dirs[path_key(source)] = Path(str(output_dir))
                self.sync_history(
                    item,
                    output_dir=str(output_dir) if output_dir else None,
                    outputs=[str(value) for value in event.get("outputs", [])],
                    persist=True,
                )
            elif kind == "file_error" and item:
                item["status"] = "失败"
                error = str(event.get("error", "未知错误"))
                self.append_log(f"{item['name']}：{error}", notify=False)
                self.sync_history(item, persist=True)
            elif kind == "batch_done":
                self.batch_summary = {
                    "total": int(event.get("total", 0)),
                    "completed": int(event.get("completed", 0)),
                    "failed": int(event.get("failed", 0)),
                    "skipped": int(event.get("skipped", 0)),
                }
            if item and kind in {
                "file_start", "file_progress", "file_retry", "llm_repair_start",
                "llm_repair_progress", "llm_repair_done",
            }:
                self.sync_history(item)
        self.notify(kind, source=source)

    def append_log(self, text: str, notify: bool = True) -> None:
        text = text.strip()
        if not text:
            return
        with self.lock:
            self.log_lines.append(text)
            if len(self.log_lines) > 500:
                self.log_lines = self.log_lines[-500:]
        if notify:
            self.notify("log")

    def finish(self, code: int) -> None:
        with self.lock:
            self.running = False
            self.paused = False
            self.status_before_pause = ""
            self.model_loading = False
            try:
                LOG_FILE.write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
            except OSError:
                pass

            if self.cancel_requested:
                self.status_text = "已取消转写；已完成的结果仍然保留"
                for item in self.files:
                    if item["status"] in {"等待中", "转写中"}:
                        item["status"] = "已取消"
                        self.sync_history(item)
                message = "转写已取消，已完成的结果仍然保留"
                level = "info"
            else:
                completed = self.batch_summary["completed"]
                failed = self.batch_summary["failed"]
                skipped = self.batch_summary["skipped"]
                total = self.batch_summary["total"] or len(self.files)
                is_single = len(self.files) == 1
                if is_single and code == 0 and completed == 1:
                    self.status_text = f"转写完成：{self.files[0]['name']}"
                    message = self.status_text
                    level = "info"
                elif is_single and code == 0 and skipped == 1:
                    self.status_text = f"已有完整结果：{self.files[0]['name']}"
                    message = self.status_text
                    level = "info"
                elif is_single:
                    self.status_text = f"转写失败：{self.files[0]['name']}"
                    message = self.status_text
                    level = "error"
                elif code == 0 and completed + skipped == total:
                    self.status_text = f"批量转写完成：新转写 {completed} 个，跳过已有 {skipped} 个"
                    message = self.status_text
                    level = "info"
                elif completed or failed or skipped:
                    self.status_text = f"批量转写结束：成功 {completed} 个，已有 {skipped} 个，失败 {failed} 个"
                    message = self.status_text
                    level = "error" if failed else "info"
                else:
                    self.status_text = "转写未能启动，请查看运行信息"
                    message = self.status_text
                    level = "error"
            self.save_history()
        if self.source_map_file:
            try:
                self.source_map_file.unlink(missing_ok=True)
            except OSError:
                pass
            self.source_map_file = None
        self.notify("process_done", message=message, level=level)

    def pause_transcription(self) -> dict[str, Any]:
        if not self.running:
            return self.response(False, error="当前没有正在运行的转写任务。")
        if self.paused:
            return self.response()
        process = self.process
        if process is None or process.poll() is not None:
            return self.response(False, error="任务正在启动，请稍后再暂停。")
        if not set_process_suspended(process.pid, True):
            return self.response(False, error="暂停失败，请稍后重试。")
        with self.lock:
            self.paused = True
            self.status_before_pause = self.status_text
            active_item = next((item for item in self.files if item["status"] == "转写中"), None)
            if active_item:
                active_item["status"] = "已暂停"
                self.status_text = f"已暂停：{active_item['name']}"
            else:
                self.status_text = "转写任务已暂停"
        return self.response(message="转写已暂停，可随时继续")

    def resume_transcription(self) -> dict[str, Any]:
        if not self.running:
            return self.response(False, error="当前没有正在运行的转写任务。")
        if not self.paused:
            return self.response()
        process = self.process
        if process is None or process.poll() is not None:
            return self.response(False, error="任务进程已经结束。")
        if not set_process_suspended(process.pid, False):
            return self.response(False, error="继续转写失败，请稍后重试。")
        with self.lock:
            self.paused = False
            active_item = next((item for item in self.files if item["status"] == "已暂停"), None)
            if active_item:
                active_item["status"] = "转写中"
            self.status_text = self.status_before_pause or "正在继续转写…"
            self.status_before_pause = ""
        return self.response(message="已继续转写")

    def cancel_transcription(self) -> dict[str, Any]:
        if not self.running or self.cancel_requested:
            return self.response()
        with self.lock:
            self.cancel_requested = True
            self.status_text = "正在取消…"
        process = self.process
        if process and process.poll() is None:
            try:
                if self.paused:
                    set_process_suspended(process.pid, False)
                    self.paused = False
                process.terminate()
            except OSError:
                pass
        return self.response(message="正在停止当前批次")

    def resolve_result_dir(self, source: str, settings: dict[str, Any]) -> Path:
        key = path_key(source)
        result_dir = self.result_dirs.get(key)
        history_record = self.history.get(key, {})
        if result_dir is None and history_record.get("result_dir"):
            result_dir = Path(str(history_record["result_dir"]))
        if result_dir is None:
            if settings["output_mode"] == "custom" and settings["output_path"]:
                result_dir = Path(settings["output_path"]).expanduser().resolve()
            else:
                result_dir = Path(source).expanduser().resolve().parent / "转写结果"
        return result_dir

    def result_candidates(
        self,
        source: str,
        settings: dict[str, Any],
        material: str,
    ) -> list[Path]:
        key = path_key(source)
        history_record = self.history.get(key, {})
        recorded = [Path(str(value)) for value in history_record.get("outputs", [])]
        result_dir = self.resolve_result_dir(source, settings)
        source_path = Path(source).expanduser().resolve()
        base_stem = source_path.stem if settings["model"] == "medium" else f"{source_path.stem}.{settings['model']}"

        if material == "corrections":
            preferred = [path for path in recorded if path.name.endswith(".llm-corrections.json")]
            preferred.append(result_dir / f"{base_stem}.llm-corrections.json")
            preferred.extend(sorted(result_dir.glob(f"{source_path.stem}*.llm-corrections.json"), key=lambda path: path.name.casefold(), reverse=True))
            return preferred
        markdown_files = [path for path in recorded if path.suffix.lower() == ".md"]
        if material == "raw":
            preferred = [path for path in markdown_files if not path.name.endswith(".llm.md")]
            preferred.append(result_dir / f"{base_stem}.md")
            preferred.extend(
                path for path in sorted(result_dir.glob(f"{source_path.stem}*.md"), key=lambda path: path.name.casefold(), reverse=True)
                if not path.name.endswith(".llm.md")
            )
            return preferred
        preferred = [path for path in markdown_files if path.name.endswith(".llm.md")]
        preferred.extend(path for path in markdown_files if not path.name.endswith(".llm.md"))
        preferred.extend((result_dir / f"{base_stem}.llm.md", result_dir / f"{base_stem}.md"))
        preferred.extend(sorted(result_dir.glob(f"{source_path.stem}*.llm.md"), key=lambda path: path.name.casefold(), reverse=True))
        preferred.extend(
            path for path in sorted(result_dir.glob(f"{source_path.stem}*.md"), key=lambda path: path.name.casefold(), reverse=True)
            if not path.name.endswith(".llm.md")
        )
        return preferred

    def read_result(
        self,
        source: str,
        raw_settings: dict[str, Any],
        material: str = "accurate",
    ) -> dict[str, Any]:
        key = path_key(source)
        if self.find_file(source) is None and key not in self.history:
            return self.response(False, error="没有找到这条转写记录。")
        settings = self.normalize_settings(raw_settings)
        material = material if material in {"accurate", "raw", "corrections"} else "accurate"
        candidates = self.result_candidates(source, settings, material)
        result_path = next((path for path in candidates if path.is_file()), None)
        if result_path is None:
            item = self.find_file(source)
            record = item or self.history.get(key, {})
            status = str(record.get("status", ""))
            if status in {"等待中", "模型加载中", "转写中", "已暂停", "大模型校订中", "校订完成"}:
                return self.response(False, pending=True)
            if material == "corrections":
                return self.response(
                    False,
                    unavailable=True,
                    reason="本次任务没有生成校订记录",
                )
            return self.response(False, error="该任务还没有生成对应内容。")
        try:
            if material != "corrections":
                content = result_path.read_text(encoding="utf-8-sig")
            else:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                corrections = payload.get("corrections", []) if isinstance(payload, dict) else []
                accepted = [item for item in corrections if isinstance(item, dict) and item.get("accepted")]
                lines = ["# 校订记录", "", f"共接受 {len(accepted)} 处校订。", ""]
                for item in accepted:
                    start = float(item.get("start") or 0.0)
                    minutes, seconds = divmod(int(start), 60)
                    lines.extend(
                        (
                            f"## [{minutes:02d}:{seconds:02d}]",
                            "",
                            f"- 原文：{item.get('original', '')}",
                            f"- 校订：{item.get('corrected', '')}",
                            f"- 原因：{item.get('reason', '') or '上下文校订'}",
                            "",
                        )
                    )
                content = "\n".join(lines)
        except (OSError, json.JSONDecodeError) as exc:
            return self.response(False, error=f"读取结果失败：{exc}")
        return self.response(content=content, filename=result_path.name, path=str(result_path))

    def open_result(self, source: str, raw_settings: dict[str, Any]) -> dict[str, Any]:
        key = path_key(source)
        item = self.find_file(source)
        if item is None and key not in self.history:
            return self.response(False, error="没有找到选中的文件。")
        settings = self.normalize_settings(raw_settings)
        result_dir = self.resolve_result_dir(source, settings)
        if not result_dir.is_dir():
            return self.response(False, error="该文件还没有生成转写结果。")
        os.startfile(result_dir)
        return self.response()

    def on_closing(self) -> bool:
        if not self.running:
            return True
        window = self.window
        if window is None:
            return False
        should_close = window.create_confirmation_dialog(
            "转写仍在进行",
            "关闭窗口会停止当前批次，已完成的结果会保留。确定关闭吗？",
        )
        if not should_close:
            return False
        self.cancel_requested = True
        process = self.process
        if process and process.poll() is None:
            try:
                if self.paused:
                    set_process_suspended(process.pid, False)
                    self.paused = False
                process.terminate()
            except OSError:
                pass
        return True


def smoke_test() -> None:
    if not UI_FILE.is_file():
        raise FileNotFoundError(UI_FILE)
    if not ICON_FILE.is_file():
        raise FileNotFoundError(ICON_FILE)
    lucide_script = APP_DIR / "ui" / "vendor" / "lucide.min.js"
    lucide_license = APP_DIR / "ui" / "vendor" / "LUCIDE-LICENSE"
    if not lucide_script.is_file():
        raise FileNotFoundError(lucide_script)
    if not lucide_license.is_file():
        raise FileNotFoundError(lucide_license)
    html = UI_FILE.read_text(encoding="utf-8")
    script = (APP_DIR / "ui" / "app.js").read_text(encoding="utf-8")
    if "window.LocalTranscriber" not in script:
        raise RuntimeError("前端事件入口缺失")
    if "自动识别（仅转写）" not in html:
        raise RuntimeError("语言默认选项缺失")
    if "modelSelect" not in html:
        raise RuntimeError("转写模型配置缺失")
    if "pauseButton" not in html:
        raise RuntimeError("暂停和继续控制缺失")
    if "补充视频来源（可选）" not in script or "source_urls" not in script:
        raise RuntimeError("逐文件来源上下文缺失")
    if "llmRepair" not in html or "deepseekApiKey" not in html:
        raise RuntimeError("DeepSeek 校订设置缺失")
    if "historyList" not in html or "markdownPreview" not in html:
        raise RuntimeError("内容历史或 Markdown 查看器缺失")
    if 'vendor/lucide.min.js' not in html or 'data-lucide=' not in html:
        raise RuntimeError("本地 Lucide 图标资源缺失")
    TranscriberApi()
    print("GUI_SMOKE_OK")


def main() -> None:
    if "--smoke-test" in sys.argv:
        smoke_test()
        return
    configure_app_identity()
    initial_files = tuple(arg for arg in sys.argv[1:] if not arg.startswith("--"))
    api = TranscriberApi(initial_files)
    window = webview.create_window(
        "本地语音转写",
        url=UI_FILE.resolve().as_uri(),
        js_api=api,
        width=1120,
        height=760,
        min_size=(900, 620),
        background_color="#F3F5F8",
        text_select=False,
    )
    assert window is not None
    api.attach_window(window)
    window.events.closing += api.on_closing
    webview.start(debug=False, private_mode=True, icon=str(ICON_FILE))


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
