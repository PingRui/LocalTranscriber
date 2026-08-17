from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from llm_client import DEFAULT_LOCAL_BASE_URL, DEFAULT_LOCAL_MODEL
from llm_repair import DEFAULT_MODEL as DEFAULT_DEEPSEEK_MODEL
from llm_repair import repair_transcript_file


EVENT_PREFIX = "@@LOCAL_TRANSCRIBER_EVENT@@"
EXCLUDED_SUFFIXES = (
    ".llm.json",
    ".llm-corrections.json",
    ".source-context.json",
)


def emit_event(enabled: bool, event_type: str, **payload: Any) -> None:
    if not enabled:
        return
    print(EVENT_PREFIX + json.dumps({"event": event_type, **payload}, ensure_ascii=False), flush=True)


def expected_outputs(json_path: Path) -> list[Path]:
    stem = json_path.stem
    return [
        json_path.with_name(f"{stem}.llm.md"),
        json_path.with_name(f"{stem}.llm.txt"),
        json_path.with_name(f"{stem}.llm.srt"),
        json_path.with_name(f"{stem}.llm.json"),
        json_path.with_name(f"{stem}.llm-corrections.json"),
    ]


def load_term_aliases(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    target = path.expanduser().resolve()
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取术语映射文件：{target}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("术语映射必须是 JSON 对象")
    result: dict[str, str] = {}
    for before, after in payload.items():
        source = str(before).strip()
        target_value = str(after).strip()
        if not source or not target_value or source == target_value:
            raise ValueError(f"术语映射无效：{before!r} -> {after!r}")
        result[source] = target_value
    return result


def inspect_transcript(path: Path) -> dict[str, Any] | None:
    if path.name.lower().endswith(EXCLUDED_SUFFIXES):
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
    valid_segments = 0
    for item in segments:
        if not isinstance(item, dict):
            return None
        text = str(item.get("text") or "").strip()
        if not text or "start" not in item or "end" not in item:
            return None
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (TypeError, ValueError):
            return None
        if start < 0 or end < start:
            return None
        valid_segments += 1
    outputs = expected_outputs(path)
    complete = all(item.is_file() for item in outputs)
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "folder": str(path.parent.resolve()),
        "source": source,
        "segment_count": valid_segments,
        "complete": complete,
        "status": "已有完整结果" if complete else "等待清洗",
        "progress": 100.0 if complete else 0.0,
        "outputs": [str(item.resolve()) for item in outputs] if complete else [],
    }


def discover_transcripts(
    root: Path,
    progress: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    folder = root.expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"批量清洗目录不存在：{folder}")
    discovered: list[dict[str, Any]] = []
    inspected = 0
    for path in sorted(folder.rglob("*.json"), key=lambda item: str(item).casefold()):
        inspected += 1
        record = inspect_transcript(path)
        if record is not None:
            discovered.append(record)
        if progress and inspected % 50 == 0:
            progress(inspected)
    return discovered


def clean_batch(
    root: Path,
    provider: str,
    model: str,
    base_url: str,
    api_key: str = "",
    skip_existing: bool = True,
    gui_events: bool = False,
    pause_file: Path | None = None,
    term_aliases: dict[str, str] | None = None,
    repair: Callable[..., list[Path]] = repair_transcript_file,
) -> dict[str, int]:
    records = discover_transcripts(root)
    summary = {
        "total": len(records),
        "completed": 0,
        "failed": 0,
        "skipped": 0,
    }
    emit_event(gui_events, "clean_batch_start", **summary)

    def wait_if_paused() -> None:
        while pause_file is not None and pause_file.is_file():
            time.sleep(0.2)

    for file_index, record in enumerate(records, start=1):
        wait_if_paused()
        path = Path(record["path"])
        if skip_existing and record["complete"]:
            summary["skipped"] += 1
            emit_event(
                gui_events,
                "clean_file_skipped",
                path=str(path),
                file_index=file_index,
                file_total=len(records),
                outputs=record["outputs"],
            )
            continue

        emit_event(
            gui_events,
            "clean_file_start",
            path=str(path),
            file_index=file_index,
            file_total=len(records),
            segment_count=record["segment_count"],
        )

        def report_progress(current: int, total: int) -> None:
            emit_event(
                gui_events,
                "clean_file_progress",
                path=str(path),
                file_index=file_index,
                file_total=len(records),
                current=current,
                total=total,
                progress=round(current / max(total, 1) * 100, 2),
            )
            wait_if_paused()

        try:
            outputs = repair(
                path,
                api_key,
                model,
                report_progress,
                provider=provider,
                base_url=base_url,
                term_aliases=term_aliases or {},
            )
            summary["completed"] += 1
            emit_event(
                gui_events,
                "clean_file_done",
                path=str(path),
                file_index=file_index,
                file_total=len(records),
                outputs=[str(item) for item in outputs],
            )
        except Exception as exc:
            summary["failed"] += 1
            print(f"清洗失败：{path}\n{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            emit_event(
                gui_events,
                "clean_file_error",
                path=str(path),
                file_index=file_index,
                file_total=len(records),
                error=f"{type(exc).__name__}: {exc}",
            )
    emit_event(gui_events, "clean_batch_done", **summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="递归清洗 LocalTranscriber 结构化转写结果")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--provider", choices=["local", "deepseek"], default="local")
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default=DEFAULT_LOCAL_BASE_URL)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--pause-file", type=Path)
    parser.add_argument("--term-aliases", type=Path)
    parser.add_argument("--gui-events", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    model = args.model.strip() or (
        DEFAULT_LOCAL_MODEL if args.provider == "local" else DEFAULT_DEEPSEEK_MODEL
    )
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if args.provider == "deepseek" and not api_key:
        print("缺少 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    try:
        term_aliases = load_term_aliases(args.term_aliases)
        summary = clean_batch(
            args.folder,
            args.provider,
            model,
            args.base_url,
            api_key,
            args.skip_existing,
            args.gui_events,
            args.pause_file,
            term_aliases,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"批量清洗无法启动：{exc}", file=sys.stderr)
        emit_event(args.gui_events, "clean_batch_error", error=f"{type(exc).__name__}: {exc}")
        return 2
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
