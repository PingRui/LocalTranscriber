from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from app_config import SUPPORTED_MODELS, load_config, model_paths

APP_DIR = Path(__file__).resolve().parent
APP_CONFIG = load_config()
RUNTIME_DIR = Path(os.environ.get("LOCALTRANSCRIBER_RUNTIME_DIR", APP_DIR / "runtime"))
MODEL_PATHS = model_paths(APP_CONFIG)
DEFAULT_MODEL = str(APP_CONFIG["default_model"])
_DLL_DIRECTORY_HANDLES: list[object] = []

os.environ.setdefault("HF_HOME", str(APP_CONFIG["hf_cache_dir"]))

# Keep GPU runtime private to this component instead of changing the system PATH.
if RUNTIME_DIR.is_dir():
    os.environ["PATH"] = f"{RUNTIME_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
    if hasattr(os, "add_dll_directory"):
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(RUNTIME_DIR)))

from faster_whisper import WhisperModel
from llm_repair import DEFAULT_MODEL as DEFAULT_LLM_MODEL
from llm_repair import repair_transcript_file
from source_context import load_source_context
EVENT_PREFIX = "@@LOCAL_TRANSCRIBER_EVENT@@"
DEFAULT_PROMPT = ""


def emit_event(enabled: bool, event: str, **payload: object) -> None:
    if not enabled:
        return
    message = {"event": event, **payload}
    print(EVENT_PREFIX + json.dumps(message, ensure_ascii=False), flush=True)


def timestamp(seconds: float, srt: bool = False) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1_000)
    separator = "," if srt else ":"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{milliseconds:03d}"


def repetition_risk(segments: list[dict[str, object]]) -> bool:
    for segment in segments:
        text = str(segment["text"])
        words = [word.casefold() for word in text.replace(",", " ").split()]
        unique_ratio = len(set(words)) / max(len(words), 1)
        if float(segment["compression_ratio"]) >= 3.0:
            return True
        if len(words) >= 20 and unique_ratio < 0.25:
            return True
    return False


def add_review_reasons(segments: list[dict[str, object]]) -> int:
    review_count = 0
    for segment in segments:
        reasons: list[str] = []
        if float(segment["avg_logprob"]) < -0.7:
            reasons.append("low_confidence")
        if float(segment["compression_ratio"]) > 2.4:
            reasons.append("possible_repetition")
        if float(segment["no_speech_prob"]) > 0.6:
            reasons.append("possible_non_speech")
        segment["review_reasons"] = reasons
        review_count += bool(reasons)
    return review_count


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, start=1):
        current = [index]
        for other_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[other_index] + 1,
                    previous[other_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def canonicalize_entities(
    segments: list[dict[str, object]], source_context: dict[str, object]
) -> list[dict[str, object]]:
    people = [str(item) for item in source_context.get("people", [])]
    primary_people = {str(item) for item in source_context.get("primary_people", [])}
    terms = [str(item) for item in source_context.get("terms", [])]
    corrections: list[dict[str, object]] = []

    for segment in segments:
        text = str(segment["text"])
        original = text
        replacements: list[tuple[str, str]] = []

        for person in people:
            parts = person.split()
            if len(parts) < 2:
                continue
            first, surname = parts[0], " ".join(parts[1:])
            full_pattern = re.compile(rf"\b([A-Z][a-z]+)\s+{re.escape(surname)}\b")

            def replace_full(match: re.Match[str]) -> str:
                found = match.group(1)
                if found != first and edit_distance(found.casefold(), first.casefold()) <= 1:
                    replacements.append((match.group(0), person))
                    return person
                return match.group(0)

            text = full_pattern.sub(replace_full, text)
            if person not in primary_people:
                continue
            first_pattern = re.compile(rf"\b[A-Z][a-z]+\b")

            def replace_first(match: re.Match[str]) -> str:
                found = match.group(0)
                if found != first and edit_distance(found.casefold(), first.casefold()) <= 1:
                    replacements.append((found, first))
                    return first
                return found

            text = first_pattern.sub(replace_first, text)

        for term in terms:
            parts = term.split()
            if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z]+", part) for part in parts):
                continue
            first, second = parts
            pattern = re.compile(rf"\b([A-Za-z]+)\s+{re.escape(second)}\b", flags=re.IGNORECASE)

            def replace_term(match: re.Match[str]) -> str:
                found = match.group(1)
                if found.casefold() != first.casefold() and edit_distance(found.casefold(), first.casefold()) <= 2:
                    replacements.append((match.group(0), term))
                    return term
                return match.group(0)

            text = pattern.sub(replace_term, text)

        if text != original:
            segment["text"] = text
            corrections.append(
                {"start": segment["start"], "replacements": [{"from": old, "to": new} for old, new in replacements]}
            )
    return corrections


def choose_output_dir(source: Path, requested: str | None) -> Path:
    if requested:
        return Path(requested).expanduser().resolve()
    return source.parent / "转写结果"


def expected_outputs(source: Path, requested: str | None, output_stem: str | None = None) -> list[Path]:
    output_dir = choose_output_dir(source, requested)
    stem = output_stem or source.stem
    return [output_dir / f"{stem}{suffix}" for suffix in (".md", ".txt", ".srt", ".json")]


def model_output_stem(source: Path, model_name: str, requested_stem: str | None = None) -> str:
    base = requested_stem or source.stem
    # Keep existing Medium filenames compatible. Turbo results are stored beside
    # them instead of silently overwriting a previous Medium transcription.
    return base if model_name == "medium" else f"{base}.{model_name}"


def load_model(model_name: str, device: str, gui_events: bool = False) -> tuple[WhisperModel, str, str]:
    model_path = MODEL_PATHS[model_name]
    attempts: list[tuple[str, str]]
    if device == "cpu":
        attempts = [("cpu", "int8")]
    elif device == "cuda":
        attempts = [("cuda", "int8_float16")]
    else:
        attempts = [("cuda", "int8_float16"), ("cpu", "int8")]

    last_error: Exception | None = None
    for selected_device, compute_type in attempts:
        try:
            print(f"正在加载模型：{model_name} / {selected_device} / {compute_type}")
            emit_event(
                gui_events,
                "model_loading",
                model=model_name,
                device=selected_device,
                compute_type=compute_type,
            )
            model = WhisperModel(
                str(model_path),
                device=selected_device,
                compute_type=compute_type,
                local_files_only=True,
            )
            emit_event(
                gui_events,
                "model_ready",
                model=model_name,
                device=selected_device,
                compute_type=compute_type,
            )
            return model, selected_device, compute_type
        except Exception as exc:  # GPU runtime availability varies across Windows setups.
            last_error = exc
            if selected_device == "cuda" and device == "auto":
                print(f"显卡加速不可用，自动切换 CPU：{exc}")
                continue
            raise
    raise RuntimeError(f"模型加载失败：{last_error}")


def transcribe_one(
    source: Path,
    model: WhisperModel,
    model_name: str,
    model_device: str,
    compute_type: str,
    output_arg: str | None,
    language: str | None,
    prompt: str,
    source_context: dict[str, object] | None = None,
    context_mode: str = "isolated",
    llm_repair_enabled: bool = False,
    llm_model: str = DEFAULT_LLM_MODEL,
    gui_events: bool = False,
    output_stem: str | None = None,
) -> list[Path]:
    output_dir = choose_output_dir(source, output_arg)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_stem or source.stem

    print(f"\n开始转写：{source}")
    emit_event(gui_events, "file_start", source=str(source), output_dir=str(output_dir))
    started = time.time()
    source_context = source_context or {}
    context_terms = [str(item) for item in source_context.get("terms", [])]
    hotwords = ", ".join(item for item in [*context_terms, prompt] if item)
    initial_prompt = str(source_context.get("initial_prompt", ""))
    def decode(keep_previous: bool, decode_hotwords: str) -> tuple[list[dict[str, object]], object]:
        segments_iter, decode_info = model.transcribe(
            str(source),
            language=language,
            task="transcribe",
            beam_size=5,
            patience=1.0,
            temperature=0.0,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
            condition_on_previous_text=keep_previous,
            initial_prompt=initial_prompt or None,
            hotwords=decode_hotwords or None,
            word_timestamps=False,
            multilingual=True,
            language_detection_segments=3,
        )
        decoded: list[dict[str, object]] = []
        for segment in segments_iter:
            text = segment.text.strip()
            if not text:
                continue
            decoded.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": text,
                    "avg_logprob": segment.avg_logprob,
                    "compression_ratio": segment.compression_ratio,
                    "no_speech_prob": segment.no_speech_prob,
                }
            )
            duration = max(float(decode_info.duration), 0.001)
            progress = min(100.0, segment.end / duration * 100)
            if gui_events:
                emit_event(
                    True,
                    "file_progress",
                    source=str(source),
                    progress=round(progress, 2),
                    timestamp=timestamp(segment.start)[:8],
                    text=text[:80],
                )
            else:
                print(
                    f"\r进度 {progress:6.2f}%  [{timestamp(segment.start)[:8]}] {text[:38]:<38}",
                    end="",
                    flush=True,
                )
        if not gui_events:
            print()
        return decoded, decode_info

    effective_context_mode = context_mode
    fallback_used = False
    segments, info = decode(context_mode == "continuous", hotwords)
    if repetition_risk(segments):
        fallback_used = True
        effective_context_mode = "isolated_fallback"
        safe_hotwords = ", ".join(item for item in [*context_terms[:20], prompt] if item)
        print("检测到异常重复，正在用安全分段模式自动重试…", flush=True)
        emit_event(gui_events, "file_retry", source=str(source), reason="possible_repetition")
        segments, info = decode(False, safe_hotwords)
        hotwords = safe_hotwords
        if repetition_risk(segments):
            print("热词仍可能触发重复，正在无热词重试…", flush=True)
            segments, info = decode(False, "")
            hotwords = ""
            effective_context_mode = "no_hotwords_fallback"

    entity_corrections = canonicalize_entities(segments, source_context)
    review_count = add_review_reasons(segments)

    elapsed = time.time() - started
    detected_language = info.language or language or "unknown"
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    metadata = {
        "source": str(source),
        "created_at": created_at,
        "model": model_name,
        "model_path": str(MODEL_PATHS[model_name]),
        "device": model_device,
        "compute_type": compute_type,
        "language": detected_language,
        "language_probability": info.language_probability,
        "duration_seconds": info.duration,
        "processing_seconds": round(elapsed, 2),
        "context_mode": context_mode,
        "effective_context_mode": effective_context_mode,
        "automatic_fallback_used": fallback_used,
        "review_segment_count": review_count,
        "entity_corrections": entity_corrections,
        "source_context": source_context,
        "hotwords": hotwords,
        "segments": segments,
    }

    txt_path = output_dir / f"{stem}.txt"
    srt_path = output_dir / f"{stem}.srt"
    md_path = output_dir / f"{stem}.md"
    json_path = output_dir / f"{stem}.json"

    txt_path.write_text("\n".join(str(item["text"]) for item in segments) + "\n", encoding="utf-8-sig")

    srt_blocks = []
    for index, item in enumerate(segments, start=1):
        srt_blocks.append(
            f"{index}\n{timestamp(float(item['start']), True)} --> {timestamp(float(item['end']), True)}\n{item['text']}"
        )
    srt_path.write_text("\n\n".join(srt_blocks) + "\n", encoding="utf-8-sig")

    frontmatter_source = str(source).replace("\\", "/").replace('"', '\\"')
    md_lines = [
        "---",
        f'source: "{frontmatter_source}"',
        f'created: "{created_at}"',
        f'transcription_model: "faster-whisper-{model_name}"',
        f'language: "{detected_language}"',
        f'duration_seconds: {float(info.duration):.3f}',
        f'quality_review_segments: {review_count}',
        "---",
        "",
        f"# {stem}｜完整转写",
        "",
        "> 本文由本地语音模型自动转写，未做摘要或内容删减。时间戳可用于回看原视频。",
        f"> 自动质量检查：{review_count} 个片段建议人工复核；重复循环会自动触发安全重试。",
        "",
        "## 逐段转写",
        "",
    ]
    if source_context.get("source_url"):
        md_lines.insert(3, f'source_url: "{source_context["source_url"]}"')
    md_lines.extend(f"**[{timestamp(float(item['start']))[:8]}]** {item['text']}" for item in segments)
    md_path.write_text("\n\n".join(md_lines) + "\n", encoding="utf-8-sig")
    json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    outputs = [md_path, txt_path, srt_path, json_path]
    if llm_repair_enabled:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("已启用 DeepSeek 校订，但没有提供 API Key")
        print(f"正在使用 {llm_model} 结合上下文校订…", flush=True)
        emit_event(gui_events, "llm_repair_start", source=str(source), model=llm_model)

        def report_llm_progress(current: int, total: int) -> None:
            progress_value = current / max(total, 1) * 100
            print(f"DeepSeek 校订进度：{current}/{total}", flush=True)
            emit_event(
                gui_events,
                "llm_repair_progress",
                source=str(source),
                progress=round(progress_value, 2),
                current=current,
                total=total,
            )

        llm_outputs = repair_transcript_file(json_path, api_key, llm_model, report_llm_progress)
        outputs.extend(llm_outputs)
        emit_event(
            gui_events,
            "llm_repair_done",
            source=str(source),
            model=llm_model,
            outputs=[str(path) for path in llm_outputs],
        )

    print(f"完成，用时 {elapsed / 60:.1f} 分钟。输出目录：{output_dir}")
    emit_event(
        gui_events,
        "file_done",
        source=str(source),
        output_dir=str(output_dir),
        processing_seconds=round(elapsed, 2),
        outputs=[str(path) for path in outputs],
    )
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="完全本地的视频/音频转写工具")
    parser.add_argument("files", nargs="+", help="一个或多个视频/音频文件")
    parser.add_argument("--output", help="指定输出目录；默认在源文件旁创建“转写结果”")
    parser.add_argument("--model", choices=SUPPORTED_MODELS, default=DEFAULT_MODEL, help="本地转写模型")
    parser.add_argument("--language", default="auto", help="语言代码；默认 auto，仅识别原语言，不进行翻译")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="可选的人名、产品名和专业词汇提示")
    parser.add_argument("--source-url", help="视频来源页面；自动提取标题、人名和专业词汇")
    parser.add_argument("--source-url-map", help="JSON 文件：媒体绝对路径到来源网址的映射，支持批量任务")
    parser.add_argument("--refresh-source-context", action="store_true", help="忽略来源上下文缓存并重新获取")
    parser.add_argument("--llm-repair", action="store_true", help="转写后使用 DeepSeek 进行上下文校订")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL, help="DeepSeek 校订模型")
    parser.add_argument(
        "--context-mode",
        choices=["continuous", "isolated"],
        default="isolated",
        help="isolated 是防重复的安全默认值；continuous 仅适合已验证的连续内容",
    )
    parser.add_argument("--skip-existing", action="store_true", help="四种结果均已存在时跳过该文件")
    parser.add_argument("--gui-events", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requested_sources = [Path(item).expanduser().resolve() for item in args.files]
    sources = [item for item in requested_sources if item.is_file()]
    missing = [item for item in requested_sources if not item.is_file()]

    source_urls: dict[str, str] = {}
    if args.source_url_map:
        try:
            payload = json.loads(Path(args.source_url_map).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("来源映射必须是 JSON 对象")
            for raw_source, raw_url in payload.items():
                url = str(raw_url).strip()
                if not url:
                    continue
                source_key = os.path.normcase(str(Path(str(raw_source)).expanduser().resolve()))
                source_urls[source_key] = url
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"读取来源网址映射失败：{exc}", file=sys.stderr)
            return 2
    if args.source_url:
        if len(requested_sources) != 1:
            print("--source-url 只支持单文件；批量任务请使用 --source-url-map。", file=sys.stderr)
            return 2
        source_urls[os.path.normcase(str(requested_sources[0]))] = args.source_url.strip()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    language = None if args.language.lower() == "auto" else args.language
    completed = 0
    failed = len(missing)
    skipped = 0
    for source in missing:
        error = "文件不存在，可能已被移动、删除或尚未完整下载"
        print(f"转写失败：{source}\n{error}", file=sys.stderr, flush=True)
        emit_event(args.gui_events, "file_error", source=str(source), error=error)

    pending: list[tuple[Path, str | None]] = []
    stem_counts: dict[str, int] = {}
    for source in sources:
        output_stem: str | None = None
        if args.output:
            stem_key = source.stem.casefold()
            stem_counts[stem_key] = stem_counts.get(stem_key, 0) + 1
            if stem_counts[stem_key] > 1:
                output_stem = f"{source.stem}_{stem_counts[stem_key]}"
        output_stem = model_output_stem(source, args.model, output_stem)
        output_paths = expected_outputs(source, args.output, output_stem)
        completion_paths = output_paths
        if args.llm_repair:
            output_dir = output_paths[0].parent
            completion_paths = [
                output_dir / f"{output_stem}.llm{suffix}"
                for suffix in (".md", ".txt", ".srt", ".json")
            ] + [output_dir / f"{output_stem}.llm-corrections.json"]
        if args.skip_existing and all(path.is_file() for path in completion_paths):
            skipped += 1
            output_dir = output_paths[0].parent
            print(f"跳过已有结果：{source}", flush=True)
            emit_event(
                args.gui_events,
                "file_skipped",
                source=str(source),
                output_dir=str(output_dir),
                outputs=[str(path) for path in completion_paths],
            )
            continue
        pending.append((source, output_stem))

    model_path = MODEL_PATHS[args.model]
    if pending and not model_path.is_dir():
        error = f"本地模型尚未准备好：{model_path}。请重新运行 install.ps1 安装所选模型。"
        print(error, file=sys.stderr, flush=True)
        for source, _output_stem in pending:
            failed += 1
            emit_event(args.gui_events, "file_error", source=str(source), error=error)
        pending.clear()

    model: WhisperModel | None = None
    model_device = ""
    compute_type = ""
    if pending:
        try:
            model, model_device, compute_type = load_model(args.model, args.device, args.gui_events)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(f"模型加载失败：{error}", file=sys.stderr, flush=True)
            for source, _output_stem in pending:
                failed += 1
                emit_event(args.gui_events, "file_error", source=str(source), error=error)
            pending.clear()

    for source, output_stem in pending:
        try:
            assert model is not None
            source_context: dict[str, object] = {}
            source_url = source_urls.get(os.path.normcase(str(source)), "")
            if source_url:
                context_path = choose_output_dir(source, args.output) / f"{source.stem}.source-context.json"
                print(f"正在读取来源页面：{source.name}", flush=True)
                emit_event(
                    args.gui_events,
                    "source_context_loading",
                    source=str(source),
                    source_url=source_url,
                )
                source_context = load_source_context(source_url, context_path, args.refresh_source_context)
                if source_context.get("error"):
                    print(f"来源上下文读取失败，继续本地转写：{source_context['error']}", flush=True)
                    emit_event(
                        args.gui_events,
                        "source_context_error",
                        source=str(source),
                        error=source_context["error"],
                    )
                else:
                    terms = source_context.get("terms", [])
                    print(f"已读取来源上下文：{source_context.get('title', '')}（{len(terms)} 个规范词汇）", flush=True)
                    emit_event(
                        args.gui_events,
                        "source_context_ready",
                        source=str(source),
                        title=source_context.get("title", ""),
                        term_count=len(terms),
                        cache_hit=source_context.get("cache_hit", False),
                    )
            transcribe_one(
                source,
                model,
                args.model,
                model_device,
                compute_type,
                args.output,
                language,
                args.prompt,
                source_context,
                args.context_mode,
                args.llm_repair,
                args.llm_model,
                args.gui_events,
                output_stem,
            )
            completed += 1
        except Exception as exc:
            failed += 1
            print(f"转写失败：{source}\n{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            emit_event(
                args.gui_events,
                "file_error",
                source=str(source),
                error=f"{type(exc).__name__}: {exc}",
            )
    emit_event(
        args.gui_events,
        "batch_done",
        total=len(requested_sources),
        completed=completed,
        failed=failed,
        skipped=skipped,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
