from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from hotword_library import normalize_tags


MIN_DISCOVERY_CHARS = 600
MAX_DISCOVERY_CHARS = 5000
MAX_HOTWORDS = 60


class JsonClient(Protocol):
    model: str
    base_url: str

    def complete_json(
        self,
        system_prompt: str,
        user_payload: object,
        max_tokens: int = 4096,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_hotword_payload(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    category = str(payload.get("category") or "未分类").strip()[:80] or "未分类"
    raw_words = payload.get("hotwords")
    if not isinstance(raw_words, list):
        return category, []
    seen: set[str] = set()
    words: list[dict[str, Any]] = []
    for raw in raw_words:
        if isinstance(raw, str):
            term, aliases, evidence = raw, [], ""
        elif isinstance(raw, dict):
            term = str(raw.get("term") or "")
            aliases = raw.get("aliases") if isinstance(raw.get("aliases"), list) else []
            evidence = str(raw.get("evidence") or "")
        else:
            continue
        term = term.strip()
        if not 2 <= len(term) <= 40 or term.casefold() in seen or any(char in term for char in "。！？\n"):
            continue
        seen.add(term.casefold())
        words.append(
            {
                "term": term,
                "aliases": [str(item).strip() for item in aliases[:5] if str(item).strip()],
                "evidence": evidence.strip()[:160],
            }
        )
        if len(words) >= MAX_HOTWORDS:
            break
    return category, words


class TaskHotwordDiscovery:
    def __init__(
        self,
        client: JsonClient | None,
        state_file: Path,
        discovery_seconds: float = 180.0,
        min_chars: int = MIN_DISCOVERY_CHARS,
        known_domains: list[dict[str, Any]] | None = None,
    ) -> None:
        self.client = client
        self.state_file = state_file
        self.discovery_seconds = max(30.0, float(discovery_seconds))
        # The desktop knowledge flow handles one video at a time. A short video
        # may contain fewer than 200 useful characters, but its complete sample
        # is still sufficient for a small terminology suggestion. Keep the
        # regular 600-character default while allowing that caller to opt into
        # a lower, explicit threshold.
        self.min_chars = max(50, int(min_chars))
        self.samples: list[dict[str, str]] = []
        self.hotwords: list[str] = []
        self.category = ""
        self.tags: list[str] = []
        self.source_set_ids: list[str] = []
        self.status = "collecting"
        self.error = ""
        self.confidence = 0.0
        self.known_domains = list(known_domains or [])

    @classmethod
    def from_file(cls, state_file: Path) -> "TaskHotwordDiscovery":
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("无法读取复用热词集") from exc
        raw_words = payload.get("hotwords") if isinstance(payload, dict) else None
        if not isinstance(raw_words, list):
            raise ValueError("复用热词集没有有效热词")
        discovery = cls(None, state_file)
        discovery.category, records = normalize_hotword_payload(
            {"category": payload.get("category"), "hotwords": raw_words}
        )
        discovery.tags = normalize_tags(payload.get("tags"), discovery.category)
        try:
            discovery.confidence = max(0.0, min(float(payload.get("confidence") or 0.0), 1.0))
        except (TypeError, ValueError):
            discovery.confidence = 0.0
        discovery.source_set_ids = [
            str(value) for value in payload.get("source_set_ids", []) if str(value).strip()
        ]
        discovery.hotwords = [item["term"] for item in records]
        discovery.status = "ready"
        return discovery

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def collecting(self) -> bool:
        return self.status == "collecting"

    @property
    def sample_chars(self) -> int:
        return sum(len(item["text"]) for item in self.samples)

    def add_sample(self, source: Path, segments: list[dict[str, object]]) -> int:
        if not self.collecting:
            return self.sample_chars
        text = "".join(str(item.get("text") or "").strip() for item in segments)
        if text:
            self.samples.append({"source": str(source), "text": text})
        self.save()
        return self.sample_chars

    def extract(self, on_extracting: Callable[[int], None] | None = None) -> bool:
        if not self.collecting:
            return self.ready
        total_text = "\n".join(item["text"] for item in self.samples)
        if len(total_text) < self.min_chars:
            self.save()
            return False
        if self.client is None:
            raise RuntimeError("复用热词集不能重新生成热词")
        try:
            if on_extracting is not None:
                on_extracting(len(total_text))
            payload, _usage = self.client.complete_json(
                """你负责在全量语音转写开始前建立任务级热词。根据多个媒体的文件名和少量预转写片段，判断一个简洁内容分类，并提取全量语音识别最需要的专业名词、产品名、人名、机构名、缩写和易错同音词。不要总结内容，不要输出普通高频词。返回 JSON：{\"category\":\"分类\",\"tags\":[\"类目标签\"],\"hotwords\":[{\"term\":\"规范词\",\"aliases\":[\"预转写中可能出现的错误形式\"],\"evidence\":\"简短依据\"}]}。最多 60 个热词。""",
                {
                    "sources": [item["source"] for item in self.samples],
                    "transcript": total_text[:MAX_DISCOVERY_CHARS],
                    "known_domains": self.known_domains,
                    "instructions": (
                        "优先复用已有领域名称。已有领域 saturated=true 时只返回样本中有明确词形证据的新词；"
                        "不要重复已有词，不要凭常识补词。额外返回 0 到 1 的 confidence。"
                    ),
                },
                max_tokens=1600,
            )
            if not isinstance(payload.get("hotwords"), list):
                raise ValueError("模型没有返回有效的 hotwords 数组")
            self.category, records = normalize_hotword_payload(payload)
            try:
                self.confidence = max(0.0, min(float(payload.get("confidence") or 0.0), 1.0))
            except (TypeError, ValueError):
                self.confidence = 0.0
            self.tags = normalize_tags(payload.get("tags"), self.category)
            self.hotwords = [item["term"] for item in records]
            self.status = "ready"
            self.error = ""
            self.save(records)
        except (OSError, ValueError, RuntimeError) as exc:
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            self.save()
        return self.ready

    def observe(
        self,
        source: Path,
        segments: list[dict[str, object]],
        on_extracting: Callable[[int], None] | None = None,
    ) -> bool:
        if not self.collecting:
            return self.ready
        self.add_sample(source, segments)
        return self.extract(on_extracting)

    def save(self, records: list[dict[str, Any]] | None = None) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "updated_at": iso_now(),
            "status": self.status,
            "category": self.category,
            "confidence": self.confidence,
            "tags": self.tags or normalize_tags([], self.category),
            "hotwords": records if records is not None else [{"term": item} for item in self.hotwords],
            "sources": [item["source"] for item in self.samples],
            "sample_chars": self.sample_chars,
            "sample_text": "\n".join(item["text"] for item in self.samples)[:MAX_DISCOVERY_CHARS],
            "source_set_ids": self.source_set_ids,
            "api": (
                {"base_url": self.client.base_url, "model": self.client.model}
                if self.client is not None
                else {}
            ),
            "error": self.error,
        }
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_file)
