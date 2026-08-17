from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from knowledge_space import (
    _query_terms,
    initialize_space,
    iso_now,
    load_concepts,
    load_evidence_units,
    load_index,
    load_relations,
    load_space_manifest,
    space_paths,
)


REGISTRY_SCHEMA_VERSION = 1
DEFAULT_STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LocalTranscriber"
DEFAULT_REGISTRY_PATH = DEFAULT_STATE_DIR / "knowledge-spaces.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.is_file() and path.read_text(encoding="utf-8-sig") == content:
            return
    except (OSError, UnicodeError):
        pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _safe_registry_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _summary_from_entries(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = [dict(item) for item in entries]
    domains = sorted(
        {str(item.get("domain") or "未分类").strip() or "未分类" for item in values},
        key=str.casefold,
    )
    source_ids = {str(item.get("video_id") or "") for item in values if str(item.get("video_id") or "")}
    return {
        "knowledge_count": len(values),
        "source_count": len(source_ids),
        "domains": domains,
    }


class SpaceRegistry:
    """User-approved knowledge-space roots. Absolute paths never leave this class."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or DEFAULT_REGISTRY_PATH).expanduser().resolve()
        self._lock = threading.RLock()

    def _records(self) -> list[dict[str, Any]]:
        payload = _safe_registry_payload(self.path)
        values = payload.get("spaces")
        return [dict(item) for item in values if isinstance(item, dict)] if isinstance(values, list) else []

    def _write(self, records: Iterable[dict[str, Any]]) -> None:
        normalized = sorted(
            (dict(item) for item in records),
            key=lambda item: (str(item.get("name") or "").casefold(), str(item.get("space_id") or "")),
        )
        _atomic_write_json(
            self.path,
            {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "updated_at": iso_now(),
                "spaces": normalized,
            },
        )

    def register(
        self,
        root: Path,
        *,
        entries: Iterable[dict[str, Any]] | None = None,
        display_name: str = "",
    ) -> dict[str, Any]:
        resolved = root.expanduser().resolve()
        initialize_space(resolved)
        manifest = load_space_manifest(resolved)
        values = list(entries) if entries is not None else load_index(resolved)
        summary = _summary_from_entries(values)
        paths = space_paths(resolved)
        record = {
            "space_id": manifest["space_id"],
            "name": str(display_name or resolved.name or "知识空间").strip(),
            "root": str(resolved),
            "enabled": True,
            **summary,
            "index_signature": list(_file_signature(paths["index"]) or ()),
            "registered_at": iso_now(),
        }
        with self._lock:
            records = self._records()
            records = [
                item
                for item in records
                if str(item.get("space_id") or "") != record["space_id"]
                and os.path.normcase(str(item.get("root") or "")) != os.path.normcase(str(resolved))
            ]
            records.append(record)
            self._write(records)
        return self._public(record)

    def unregister(self, space_id: str) -> bool:
        target = str(space_id or "").strip()
        with self._lock:
            records = self._records()
            retained = [item for item in records if str(item.get("space_id") or "") != target]
            if len(retained) == len(records):
                return False
            self._write(retained)
        return True

    def unregister_path(self, root: Path) -> bool:
        resolved = root.expanduser().resolve()
        key = os.path.normcase(str(resolved))
        with self._lock:
            records = self._records()
            retained = [item for item in records if os.path.normcase(str(item.get("root") or "")) != key]
            if len(retained) == len(records):
                return False
            self._write(retained)
        return True

    def list_spaces(self) -> list[dict[str, Any]]:
        with self._lock:
            records = self._records()
        return [self._public(item) for item in records]

    def resolve(self, space_id: str) -> tuple[Path, dict[str, Any]]:
        target = str(space_id or "").strip()
        if not re.fullmatch(r"space-[a-f0-9]{32}", target):
            raise KeyError("知识空间 ID 无效")
        with self._lock:
            record = next((item for item in self._records() if str(item.get("space_id") or "") == target), None)
        if record is None or not bool(record.get("enabled", True)):
            raise KeyError("知识空间未注册或已禁用")
        root = Path(str(record.get("root") or "")).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError("知识空间目录不可用，请在 LocalTranscriber 中重新关联")
        manifest = load_space_manifest(root)
        if manifest["space_id"] != target:
            raise ValueError("知识空间身份与注册记录不一致")
        return root, record

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        root = Path(str(record.get("root") or ""))
        return {
            "space_id": str(record.get("space_id") or ""),
            "name": str(record.get("name") or "知识空间"),
            "enabled": bool(record.get("enabled", True)),
            "available": root.is_dir(),
            "knowledge_count": int(record.get("knowledge_count") or 0),
            "source_count": int(record.get("source_count") or 0),
            "domains": [str(item) for item in record.get("domains", []) if str(item).strip()],
            "registered_at": str(record.get("registered_at") or ""),
        }


@dataclass
class _CompiledRecord:
    raw: dict[str, Any]
    knowledge_id: str
    record_type: str
    concept_id: str
    source_id: str
    title: str
    aliases: str
    body: str
    source_title: str
    domain: str
    title_terms: set[str]
    alias_terms: set[str]
    body_terms: set[str]
    source_terms: set[str]


@dataclass
class _SpaceCache:
    root: Path
    index_signature: tuple[int, int] | None = None
    metadata_signature: tuple[int, int] | None = None
    concept_signature: tuple[int, int] | None = None
    relation_signature: tuple[int, int] | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    concepts: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    compiled: list[_CompiledRecord] = field(default_factory=list)
    entries_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_by_source: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    concepts_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    relations_by_concept: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    load_counts: dict[str, int] = field(default_factory=lambda: {"index": 0, "evidence": 0, "concepts": 0, "relations": 0})


class KnowledgeService:
    """Agent-neutral, read-only knowledge capability with per-space lazy caches."""

    def __init__(self, registry: SpaceRegistry | None = None) -> None:
        self.registry = registry or SpaceRegistry()
        self._lock = threading.RLock()
        self._cache: dict[str, _SpaceCache] = {}

    def list_spaces(self) -> list[dict[str, Any]]:
        return self.registry.list_spaces()

    def register_space(
        self,
        root: Path,
        *,
        entries: Iterable[dict[str, Any]] | None = None,
        display_name: str = "",
    ) -> dict[str, Any]:
        return self.registry.register(root, entries=entries, display_name=display_name)

    def unregister_space(self, space_id: str) -> bool:
        removed = self.registry.unregister(space_id)
        if removed:
            with self._lock:
                self._cache.pop(space_id, None)
        return removed

    def catalog(self, space_id: str, *, domain: str = "", limit: int = 100) -> dict[str, Any]:
        cache = self._get_cache(space_id)
        domain_filter = str(domain or "").strip().casefold()
        domains: dict[str, int] = {}
        sources: dict[str, dict[str, Any]] = {}
        for entry in cache.entries:
            entry_domain = str(entry.get("domain") or "未分类").strip() or "未分类"
            domains[entry_domain] = domains.get(entry_domain, 0) + 1
            if domain_filter and entry_domain.casefold() != domain_filter:
                continue
            source_id = str(entry.get("video_id") or "")
            if not source_id:
                continue
            source = sources.setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "title": str(entry.get("source_title") or Path(str(entry.get("video_relative_path") or "")).stem or "视频来源"),
                    "domain": entry_domain,
                    "knowledge_count": 0,
                },
            )
            source["knowledge_count"] += 1
        concepts = [
            {
                "concept_id": str(item.get("concept_id") or ""),
                "name": str(item.get("canonical_name") or ""),
                "aliases": [str(value) for value in item.get("aliases", []) if str(value).strip()],
                "domains": [str(value) for value in item.get("domains", []) if str(value).strip()],
            }
            for item in cache.concepts
            if not domain_filter or domain_filter in {str(value).casefold() for value in item.get("domains", [])}
        ]
        bounded = max(1, min(int(limit), 500))
        return {
            "space_id": space_id,
            "domains": [{"name": name, "knowledge_count": count} for name, count in sorted(domains.items(), key=lambda pair: pair[0].casefold())],
            "sources": sorted(sources.values(), key=lambda item: str(item["title"]).casefold())[:bounded],
            "concepts": sorted(concepts, key=lambda item: str(item["name"]).casefold())[:bounded],
            "truncated": len(sources) > bounded or len(concepts) > bounded,
        }

    def search(
        self,
        space_id: str,
        query: str,
        *,
        limit: int = 8,
        domain: str = "",
        source_id: str = "",
        include_related: bool = True,
    ) -> dict[str, Any]:
        question = str(query or "").strip()
        query_terms = _query_terms(question)
        if not query_terms:
            raise ValueError("请输入要检索的知识")
        cache = self._get_cache(space_id)
        domain_filter = str(domain or "").strip().casefold()
        source_filter = str(source_id or "").strip()
        scored: list[tuple[int, _CompiledRecord, list[str]]] = []
        matched_ids: set[str] = set()
        direct_concepts: set[str] = set()
        for record in cache.compiled:
            if domain_filter and record.domain.casefold() != domain_filter:
                continue
            if source_filter and record.source_id != source_filter:
                continue
            score, fields = self._score(question, query_terms, record)
            if score <= 0:
                continue
            scored.append((score, record, fields))
            matched_ids.add(record.knowledge_id)
            if record.concept_id:
                direct_concepts.add(record.concept_id)

        if include_related and direct_concepts:
            related_ids: set[str] = set()
            for concept_id in direct_concepts:
                for relation in cache.relations_by_concept.get(concept_id, []):
                    left = str(relation.get("from_concept_id") or "")
                    right = str(relation.get("to_concept_id") or "")
                    related_ids.add(right if left == concept_id else left)
            for record in cache.compiled:
                if record.knowledge_id in matched_ids or record.concept_id not in related_ids:
                    continue
                if domain_filter and record.domain.casefold() != domain_filter:
                    continue
                if source_filter and record.source_id != source_filter:
                    continue
                scored.append((3, record, ["related_concept"]))
                matched_ids.add(record.knowledge_id)

        scored.sort(key=lambda item: (-item[0], item[1].title.casefold(), item[1].knowledge_id))
        bounded = max(1, min(int(limit), 50))
        results = [self._public_search_result(cache, record, score, fields) for score, record, fields in scored[:bounded]]
        return {
            "space_id": space_id,
            "query": question,
            "count": len(results),
            "results": results,
            "evidence_policy": "Only evidence_ids returned by this service may be used as factual citations.",
        }

    def get_evidence(self, space_id: str, evidence_id: str) -> dict[str, Any]:
        cache = self._get_cache(space_id)
        target = cache.evidence_by_id.get(str(evidence_id or "").strip())
        if target is None:
            raise KeyError("可信证据不存在")
        return self._public_evidence(cache, target)

    def expand_evidence_context(
        self,
        space_id: str,
        evidence_id: str,
        *,
        before: int = 2,
        after: int = 2,
    ) -> dict[str, Any]:
        cache = self._get_cache(space_id)
        target_id = str(evidence_id or "").strip()
        target = cache.evidence_by_id.get(target_id)
        if target is None:
            raise KeyError("可信证据不存在")
        source_id = str(target.get("source_id") or "")
        values = cache.evidence_by_source.get(source_id, [])
        index = next((position for position, item in enumerate(values) if str(item.get("evidence_id") or "") == target_id), -1)
        if index < 0:
            raise KeyError("可信证据上下文不可用")
        before_count = max(0, min(int(before), 10))
        after_count = max(0, min(int(after), 10))
        selected = values[max(0, index - before_count) : index + after_count + 1]
        return {
            "space_id": space_id,
            "source_id": source_id,
            "target_evidence_id": target_id,
            "evidence": [dict(self._public_evidence(cache, item), is_target=str(item.get("evidence_id") or "") == target_id) for item in selected],
        }

    def get_related_concepts(self, space_id: str, concept_id: str, *, limit: int = 20) -> dict[str, Any]:
        cache = self._get_cache(space_id)
        target_id = str(concept_id or "").strip()
        target = cache.concepts_by_id.get(target_id)
        if target is None:
            raise KeyError("概念不存在")
        values: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for relation in cache.relations_by_concept.get(target_id, []):
            left = str(relation.get("from_concept_id") or "")
            right = str(relation.get("to_concept_id") or "")
            related_id = right if left == target_id else left
            relation_type = str(relation.get("type") or "related_to")
            direction = "outgoing" if left == target_id else "incoming"
            key = related_id, relation_type, direction
            if not related_id or key in seen:
                continue
            seen.add(key)
            related = cache.concepts_by_id.get(related_id, {})
            values.append(
                {
                    "concept_id": related_id,
                    "name": str(related.get("canonical_name") or related_id),
                    "aliases": [str(item) for item in related.get("aliases", []) if str(item).strip()],
                    "relation_type": relation_type,
                    "direction": direction,
                    "source_id": str(relation.get("source_id") or ""),
                }
            )
        bounded = max(1, min(int(limit), 100))
        return {
            "space_id": space_id,
            "concept": {
                "concept_id": target_id,
                "name": str(target.get("canonical_name") or target_id),
            },
            "related": values[:bounded],
        }

    def open_video_evidence(self, space_id: str, evidence_id: str) -> dict[str, Any]:
        cache = self._get_cache(space_id)
        evidence = cache.evidence_by_id.get(str(evidence_id or "").strip())
        if evidence is None:
            raise KeyError("可信证据不存在")
        locator = evidence.get("locator") if isinstance(evidence.get("locator"), dict) else {}
        relative = str(evidence.get("video_relative_path") or "")
        video = self._resolve_video(cache.root, relative)
        if not video.is_file():
            raise FileNotFoundError("证据视频不可用，请在 LocalTranscriber 中重新关联")
        player = Path(__file__).resolve().with_name("evidence_player.py")
        executable = Path(sys.executable)
        if os.name == "nt" and executable.name.casefold() == "python.exe":
            pythonw = executable.with_name("pythonw.exe")
            if pythonw.is_file():
                executable = pythonw
        subprocess.Popen(
            [
                str(executable),
                str(player),
                "--file",
                str(video),
                "--start",
                str(float(locator.get("start") or 0)),
                "--end",
                str(float(locator.get("end") or locator.get("start") or 0)),
                "--title",
                str(evidence.get("source_title") or video.name),
            ],
            cwd=str(player.parent),
        )
        return {
            "space_id": space_id,
            "evidence_id": str(evidence.get("evidence_id") or ""),
            "opened": True,
            "video": video.name,
            "start": float(locator.get("start") or 0),
            "end": float(locator.get("end") or locator.get("start") or 0),
        }

    def cache_diagnostics(self, space_id: str) -> dict[str, Any]:
        cache = self._get_cache(space_id)
        return {"space_id": space_id, "load_counts": dict(cache.load_counts)}

    def _get_cache(self, space_id: str) -> _SpaceCache:
        root, _record = self.registry.resolve(space_id)
        paths = space_paths(root)
        index_signature = _file_signature(paths["index"])
        metadata_signature = _file_signature(paths["metadata"])
        concept_signature = _file_signature(paths["concepts"])
        relation_signature = _file_signature(paths["relations"])
        with self._lock:
            cache = self._cache.get(space_id)
            if cache is None or cache.root != root:
                cache = _SpaceCache(root=root)
                self._cache[space_id] = cache
            search_changed = False
            if cache.index_signature != index_signature:
                cache.entries = load_index(root)
                cache.index_signature = index_signature
                cache.load_counts["index"] += 1
                search_changed = True
            if cache.metadata_signature != metadata_signature:
                cache.evidence = load_evidence_units(root)
                cache.metadata_signature = metadata_signature
                cache.load_counts["evidence"] += 1
                search_changed = True
            if search_changed:
                self._compile_search(cache)
            if cache.concept_signature != concept_signature:
                cache.concepts = load_concepts(root)
                cache.concepts_by_id = {str(item.get("concept_id") or ""): item for item in cache.concepts if str(item.get("concept_id") or "")}
                cache.concept_signature = concept_signature
                cache.load_counts["concepts"] += 1
            if cache.relation_signature != relation_signature:
                cache.relations = load_relations(root)
                grouped: dict[str, list[dict[str, Any]]] = {}
                for relation in cache.relations:
                    for key in (str(relation.get("from_concept_id") or ""), str(relation.get("to_concept_id") or "")):
                        if key:
                            grouped.setdefault(key, []).append(relation)
                cache.relations_by_concept = grouped
                cache.relation_signature = relation_signature
                cache.load_counts["relations"] += 1
            return cache

    @staticmethod
    def _compile_search(cache: _SpaceCache) -> None:
        cache.entries_by_id = {str(item.get("knowledge_id") or ""): item for item in cache.entries if str(item.get("knowledge_id") or "")}
        cache.evidence_by_id = {str(item.get("evidence_id") or ""): item for item in cache.evidence if str(item.get("evidence_id") or "")}
        evidence_by_source: dict[str, list[dict[str, Any]]] = {}
        for evidence in cache.evidence:
            evidence_by_source.setdefault(str(evidence.get("source_id") or ""), []).append(evidence)
        for values in evidence_by_source.values():
            values.sort(key=lambda item: (float((item.get("locator") or {}).get("start") or 0), str(item.get("segment_id") or "")))
        cache.evidence_by_source = evidence_by_source
        covered_evidence_ids: set[str] = set()
        compiled: list[_CompiledRecord] = []
        for entry in cache.entries:
            evidence_ids = [str(item) for item in entry.get("evidence_ids", []) if str(item)]
            covered_evidence_ids.update(evidence_ids)
            title = str(entry.get("title") or "")
            aliases = " ".join(str(item) for item in entry.get("aliases", []) if str(item).strip())
            source_title = str(entry.get("source_title") or "")
            body = f"{entry.get('content') or ''} {entry.get('evidence_text') or ''} {entry.get('domain') or ''}"
            compiled.append(
                _CompiledRecord(
                    raw=entry,
                    knowledge_id=str(entry.get("knowledge_id") or ""),
                    record_type="knowledge",
                    concept_id=str(entry.get("concept_id") or ""),
                    source_id=str(entry.get("video_id") or ""),
                    title=title,
                    aliases=aliases,
                    body=body,
                    source_title=source_title,
                    domain=str(entry.get("domain") or "未分类"),
                    title_terms=_query_terms(title),
                    alias_terms=_query_terms(aliases),
                    body_terms=_query_terms(body),
                    source_terms=_query_terms(source_title),
                )
            )
        for evidence in cache.evidence:
            evidence_id = str(evidence.get("evidence_id") or "")
            if not evidence_id or evidence_id in covered_evidence_ids:
                continue
            title = str(evidence.get("source_title") or "可信视频原文")
            body = f"{evidence.get('text') or ''} {evidence.get('domain') or ''}"
            compiled.append(
                _CompiledRecord(
                    raw=evidence,
                    knowledge_id=evidence_id,
                    record_type="trusted_evidence",
                    concept_id="",
                    source_id=str(evidence.get("source_id") or ""),
                    title=title,
                    aliases="",
                    body=body,
                    source_title=title,
                    domain=str(evidence.get("domain") or "未分类"),
                    title_terms=_query_terms(title),
                    alias_terms=set(),
                    body_terms=_query_terms(body),
                    source_terms=_query_terms(title),
                )
            )
        cache.compiled = compiled

    @staticmethod
    def _score(question: str, query_terms: set[str], record: _CompiledRecord) -> tuple[int, list[str]]:
        fields: list[str] = []
        score = 0
        for name, terms, weight in (
            ("title", record.title_terms, 12),
            ("alias", record.alias_terms, 10),
            ("source", record.source_terms, 4),
            ("content", record.body_terms, 2),
        ):
            overlap = len(query_terms & terms)
            if overlap:
                fields.append(name)
                score += overlap * weight
        folded = question.casefold().strip()
        for name, value, boost in (
            ("title_exact", record.title, 40),
            ("alias_exact", record.aliases, 30),
            ("content_exact", record.body, 20),
        ):
            if folded and folded in value.casefold():
                fields.append(name)
                score += boost
        return score, fields

    @staticmethod
    def _public_search_result(cache: _SpaceCache, record: _CompiledRecord, score: int, fields: list[str]) -> dict[str, Any]:
        raw = record.raw
        if record.record_type == "knowledge":
            evidence_ids = [str(item) for item in raw.get("evidence_ids", []) if str(item)]
            start = float(raw.get("evidence_start") or 0)
            end = float(raw.get("evidence_end") or start)
            relative = str(raw.get("video_relative_path") or "")
            content = str(raw.get("content") or "")
        else:
            locator = raw.get("locator") if isinstance(raw.get("locator"), dict) else {}
            evidence_ids = [record.knowledge_id]
            start = float(locator.get("start") or 0)
            end = float(locator.get("end") or start)
            relative = str(raw.get("video_relative_path") or "")
            content = str(raw.get("text") or "")
        video = KnowledgeService._resolve_video(cache.root, relative)
        return {
            "knowledge_id": record.knowledge_id,
            "record_type": record.record_type,
            "concept_id": record.concept_id,
            "title": record.title,
            "content": content,
            "domain": record.domain,
            "source_id": record.source_id,
            "source_title": record.source_title,
            "evidence_ids": evidence_ids,
            "score": score,
            "matched_fields": fields,
            "video": {
                "video_id": record.source_id,
                "filename": video.name if relative else "",
                "start": start,
                "end": end,
                "available": bool(relative and video.is_file()),
            },
        }

    @staticmethod
    def _public_evidence(cache: _SpaceCache, evidence: dict[str, Any]) -> dict[str, Any]:
        locator = evidence.get("locator") if isinstance(evidence.get("locator"), dict) else {}
        relative = str(evidence.get("video_relative_path") or "")
        video = KnowledgeService._resolve_video(cache.root, relative)
        return {
            "evidence_id": str(evidence.get("evidence_id") or ""),
            "source_id": str(evidence.get("source_id") or ""),
            "source_title": str(evidence.get("source_title") or "视频来源"),
            "domain": str(evidence.get("domain") or "未分类"),
            "segment_id": str(evidence.get("segment_id") or ""),
            "text": str(evidence.get("text") or ""),
            "verification_status": str(evidence.get("verification_status") or "verified"),
            "video": {
                "filename": video.name if relative else "",
                "start": float(locator.get("start") or 0),
                "end": float(locator.get("end") or locator.get("start") or 0),
                "available": bool(relative and video.is_file()),
            },
        }

    @staticmethod
    def _resolve_video(root: Path, relative: str) -> Path:
        if not relative:
            return root / "__missing_video__"
        candidate = (root / Path(relative)).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("视频路径越出知识空间") from exc
        return candidate
