from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from llm_client import DEFAULT_LOCAL_BASE_URL, DEFAULT_LOCAL_MODEL, OpenAICompatibleClient
from hotword_suggestions import build_hotword_suggestions


DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
NEGATIONS = {"no", "not", "never", "without", "isn't", "wasn't", "don't", "doesn't", "didn't", "can't", "cannot", "不会", "不是", "没有", "未"}
PUNCTUATION_PATTERN = re.compile(r"[\s，。！？；：、,.!?;:'\"“”‘’（）()【】\[\]《》<>—…·-]+")


@dataclass
class DeepSeekClient:
    api_key: str
    model: str = DEFAULT_MODEL
    endpoint: str = DEFAULT_ENDPOINT
    timeout: float = 120.0

    def complete_json(self, system_prompt: str, user_payload: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": 16384,
            "stream": False,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                content = result["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                return parsed, dict(result.get("usage") or {})
            except HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", errors="replace")
                if exc.code in {401, 403}:
                    raise RuntimeError("DeepSeek API Key 无效或没有权限") from exc
                if exc.code == 429 or exc.code >= 500:
                    last_error = RuntimeError(f"DeepSeek 暂时不可用（HTTP {exc.code}）")
                else:
                    raise RuntimeError(f"DeepSeek 请求失败（HTTP {exc.code}）：{detail}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
        raise RuntimeError(f"DeepSeek 校订失败：{last_error}")


SYSTEM_PROMPT = """你是严格的语音转写校订器。你只修复自动转写错误，不做摘要、翻译、润色或扩写。
必须遵守：
1. 保留每个 segment 的含义、语气、事实、数字和说话顺序。
2. 根据来源信息和相邻上下文修复人名、公司名、模型名、同音词、漏词和明显语义冲突。
3. 如果不能确定，保留原文。
4. 不合并、不拆分 segment，不修改 id 和时间戳。
5. 返回 JSON，必须包含 segments 数组；每项只包含 id、corrected_text、reason。未修改也要返回，reason 为空字符串。
"""


def _chunks(segments: list[dict[str, object]], max_segments: int = 28, max_chars: int = 12000) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    start = 0
    while start < len(segments):
        end = start
        chars = 0
        while end < len(segments) and end - start < max_segments:
            size = len(str(segments[end].get("text", "")))
            if end > start and chars + size > max_chars:
                break
            chars += size
            end += 1
        chunks.append((start, max(end, start + 1)))
        start = max(end, start + 1)
    return chunks


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"(?:\$|¥|￥)?\d+(?:[.,]\d+)*(?:%|万|亿|k|m|b)?", text, flags=re.IGNORECASE))


def _negations(text: str) -> set[str]:
    lowered = text.casefold()
    return {word for word in NEGATIONS if word in lowered}


def _validate_correction(original: str, corrected: str) -> tuple[bool, list[str]]:
    if not corrected.strip():
        return False, ["empty_output"]
    ratio = len(corrected) / max(len(original), 1)
    if ratio < 0.55 or ratio > 1.65:
        return False, ["length_changed_too_much"]
    risks: list[str] = []
    if _numbers(original) != _numbers(corrected):
        risks.append("numbers_changed")
    if _negations(original) != _negations(corrected):
        risks.append("negation_changed")
    return True, risks


def _strict_validate_correction(original: str, corrected: str, protected_terms: set[str]) -> tuple[bool, list[str]]:
    accepted, risks = _validate_correction(original, corrected)
    if not accepted:
        return accepted, risks
    if "numbers_changed" in risks:
        return False, risks
    for term in protected_terms:
        if term and term in original and term not in corrected:
            return False, [*risks, f"protected_term_changed:{term}"]
    original_comparable = PUNCTUATION_PATTERN.sub("", original).casefold()
    corrected_comparable = PUNCTUATION_PATTERN.sub("", corrected).casefold()
    if SequenceMatcher(None, original_comparable, corrected_comparable).ratio() < 0.72:
        return False, [*risks, "text_changed_too_much"]
    return True, risks


def _apply_term_aliases(text: str, term_aliases: dict[str, str]) -> str:
    for before, after in sorted(term_aliases.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(before, after)
    return text


def repair_segments(
    segments: list[dict[str, object]],
    source_context: dict[str, object],
    client: DeepSeekClient,
    progress: Callable[[int, int], None] | None = None,
    strict_preservation: bool = False,
    term_aliases: dict[str, str] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, int]]:
    repaired = [dict(segment) for segment in segments]
    corrections: list[dict[str, object]] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    chunks = _chunks(
        segments,
        max_segments=5 if strict_preservation else 28,
        max_chars=2200 if strict_preservation else 12000,
    )

    aliases = term_aliases or {}
    source = {
        "title": source_context.get("title", ""),
        "author": source_context.get("author", ""),
        "description": str(source_context.get("description", ""))[:5000],
        "canonical_terms": list(
            dict.fromkeys([*list(source_context.get("terms", []) or []), *aliases.values()])
        ),
        "people": source_context.get("people", []),
    }
    protected_terms = {
        str(item).strip()
        for item in [source.get("title", ""), *list(source.get("canonical_terms", []) or [])]
        if str(item).strip()
    }

    for chunk_index, (start, end) in enumerate(chunks, start=1):
        target = [
            {
                "id": index,
                "start": segment.get("start"),
                "end": segment.get("end"),
                "text": _apply_term_aliases(str(segment.get("text", "")), aliases),
            }
            for index, segment in enumerate(segments[start:end], start=start)
        ]
        payload = {
            "source": source,
            "previous_context": _apply_term_aliases(
                " ".join(str(item.get("text", "")) for item in segments[max(0, start - 2) : start]),
                aliases,
            ),
            "segments": target,
            "next_context": _apply_term_aliases(
                " ".join(str(item.get("text", "")) for item in segments[end : min(len(segments), end + 2)]),
                aliases,
            ),
            "output_schema": {"segments": [{"id": 0, "corrected_text": "", "reason": ""}]},
        }
        result, usage = client.complete_json(SYSTEM_PROMPT, payload)
        for key in usage_total:
            usage_total[key] += int(usage.get(key, 0) or 0)

        returned = result.get("segments")
        if not isinstance(returned, list):
            raise RuntimeError("大模型返回结果缺少 segments 数组")
        expected_ids = [str(index) for index in range(start, end)]
        returned_ids = [str(item.get("id")) for item in returned if isinstance(item, dict)]
        if returned_ids != expected_ids:
            raise RuntimeError(f"大模型返回的片段 ID 或顺序不一致：{returned_ids}")
        by_id = {item.get("id"): item for item in returned if isinstance(item, dict)}
        for index in range(start, end):
            item = by_id.get(index) or by_id.get(str(index))
            if not item:
                continue
            original = str(segments[index].get("text", ""))
            normalized_original = _apply_term_aliases(original, aliases)
            corrected = str(item.get("corrected_text", "")).strip()
            if corrected == original:
                continue
            accepted, risks = (
                _strict_validate_correction(normalized_original, corrected, protected_terms)
                if strict_preservation
                else _validate_correction(normalized_original, corrected)
            )
            record = {
                "segment_id": index,
                "start": segments[index].get("start"),
                "original": original,
                "corrected": corrected,
                "reason": (
                    "已确认术语映射；" + str(item.get("reason", ""))
                    if normalized_original != original
                    else str(item.get("reason", ""))
                ).rstrip("；"),
                "accepted": accepted,
                "review_required": bool(risks),
                "risks": risks,
            }
            corrections.append(record)
            if accepted:
                repaired[index]["text"] = corrected
                repaired[index]["llm_repaired"] = True
                repaired[index]["llm_review_required"] = bool(risks)
        if progress:
            progress(chunk_index, len(chunks))
    return repaired, corrections, usage_total


def _timestamp(seconds: float, srt: bool = False) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{',' if srt else ':'}{milliseconds:03d}"


def write_repair_outputs(
    raw_metadata: dict[str, object],
    repaired_segments: list[dict[str, object]],
    corrections: list[dict[str, object]],
    usage: dict[str, int],
    output_dir: Path,
    stem: str,
    model: str,
    provider: str = "deepseek",
) -> list[Path]:
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    review_count = sum(bool(item.get("review_required")) for item in corrections if item.get("accepted"))
    accepted_count = sum(bool(item.get("accepted")) for item in corrections)
    llm_metadata = {
        **raw_metadata,
        "llm_repair": {
            "provider": provider,
            "model": model,
            "created_at": created_at,
            "accepted_corrections": accepted_count,
            "review_required": review_count,
            "usage": usage,
        },
        "segments": repaired_segments,
    }
    json_path = output_dir / f"{stem}.llm.json"
    md_path = output_dir / f"{stem}.llm.md"
    txt_path = output_dir / f"{stem}.llm.txt"
    srt_path = output_dir / f"{stem}.llm.srt"
    corrections_path = output_dir / f"{stem}.llm-corrections.json"
    suggestions_path = output_dir / f"{stem}.hotword-suggestions.json"

    task_hotwords = raw_metadata.get("task_hotwords")
    task_hotwords = task_hotwords if isinstance(task_hotwords, dict) else {}
    suggestions = build_hotword_suggestions(
        corrections,
        category=str(task_hotwords.get("category") or ""),
        existing_terms=task_hotwords.get("terms", []) if isinstance(task_hotwords.get("terms"), list) else [],
    )

    json_path.write_text(json.dumps(llm_metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    corrections_path.write_text(json.dumps({"model": model, "corrections": corrections}, ensure_ascii=False, indent=2), encoding="utf-8")
    suggestions_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "pending" if suggestions else "empty",
                "category": str(task_hotwords.get("category") or ""),
                "tags": task_hotwords.get("tags", []),
                "source_set_ids": task_hotwords.get("source_set_ids", []),
                "created_at": created_at,
                "suggestions": suggestions,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    txt_path.write_text("\n".join(str(item["text"]) for item in repaired_segments) + "\n", encoding="utf-8-sig")
    srt_path.write_text(
        "\n\n".join(
            f"{index}\n{_timestamp(float(item['start']), True)} --> {_timestamp(float(item['end']), True)}\n{item['text']}"
            for index, item in enumerate(repaired_segments, start=1)
        )
        + "\n",
        encoding="utf-8-sig",
    )
    lines = [
        "---",
        f'source: "{str(raw_metadata.get("source", "")).replace(chr(92), "/")}"',
        f'created: "{created_at}"',
        f'llm_repair_model: "{model}"',
        f'accepted_corrections: {accepted_count}',
        f'review_required: {review_count}',
        "---",
        "",
        f"# {stem}｜智能校订稿",
        "",
        f"> 已根据来源信息和上下文完成受约束校订，共应用 {accepted_count} 处修改，其中 {review_count} 处涉及数字或否定词，建议复核修改记录。",
        "",
        "## 逐段转写",
        "",
    ]
    lines.extend(f"**[{_timestamp(float(item['start']))[:8]}]** {item['text']}" for item in repaired_segments)
    md_path.write_text("\n\n".join(lines) + "\n", encoding="utf-8-sig")
    return [md_path, txt_path, srt_path, json_path, corrections_path, suggestions_path]


def repair_transcript_file(
    json_path: Path,
    api_key: str = "",
    model: str = DEFAULT_MODEL,
    progress: Callable[[int, int], None] | None = None,
    provider: str = "deepseek",
    base_url: str = DEFAULT_LOCAL_BASE_URL,
    term_aliases: dict[str, str] | None = None,
) -> list[Path]:
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    segments = raw.get("segments")
    if not isinstance(segments, list):
        raise RuntimeError("原始 JSON 中没有可校订的 segments")
    source_context = raw.get("source_context") if isinstance(raw.get("source_context"), dict) else {}
    if provider == "local":
        client = OpenAICompatibleClient(base_url=base_url, model=model or DEFAULT_LOCAL_MODEL)
        client.ensure_model()
    elif provider in {"openai", "compatible"}:
        client = OpenAICompatibleClient(
            base_url=base_url,
            model=model or DEFAULT_LOCAL_MODEL,
            api_key=api_key,
            allow_remote=True,
        )
    elif provider == "deepseek":
        if not api_key:
            raise RuntimeError("已启用 DeepSeek 校订，但没有提供 API Key")
        client = DeepSeekClient(api_key=api_key, model=model)
    else:
        raise ValueError(f"不支持的校订提供方：{provider}")
    repaired, corrections, usage = repair_segments(
        segments,
        source_context,
        client,
        progress,
        strict_preservation=provider in {"local", "openai", "compatible"},
        term_aliases=term_aliases,
    )
    return write_repair_outputs(raw, repaired, corrections, usage, json_path.parent, json_path.stem, model, provider)


def main() -> int:
    parser = argparse.ArgumentParser(description="使用 OpenAI 兼容模型校订本地转写 JSON")
    parser.add_argument("json_file")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--provider", choices=["deepseek", "local", "openai"], default="openai")
    parser.add_argument("--base-url", default=DEFAULT_LOCAL_BASE_URL)
    args = parser.parse_args()
    api_key = os.environ.get("LLM_API_KEY", os.environ.get("DEEPSEEK_API_KEY", "")).strip()
    if args.provider == "deepseek" and not api_key:
        raise SystemExit("缺少 DEEPSEEK_API_KEY")
    paths = repair_transcript_file(
        Path(args.json_file).expanduser().resolve(),
        api_key,
        args.model,
        provider=args.provider,
        base_url=args.base_url,
    )
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
