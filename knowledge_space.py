from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
LEGACY_MIGRATION_VERSION = 1
OBSIDIAN_PROJECTION_VERSION = 1
VIDEO_FOLDER = "视频"
OBSIDIAN_FOLDER = "Obsidian知识库"
INDEX_NAME = "knowledge-index.jsonl"
WORK_FOLDER = ".work"
KNOWLEDGE_FOLDER = ".knowledge"
SOURCES_FOLDER = "sources"
CONCEPTS_NAME = "concepts.json"
CLAIMS_NAME = "claims.jsonl"
RELATIONS_NAME = "relations.jsonl"
SCHEMA_NAME = "schema.md"
METADATA_NAME = "metadata.json"
SPACE_MANIFEST_NAME = "space.json"
WIKI_INDEX_NAME = "index.md"
WIKI_LOG_NAME = "log.md"
OBSIDIAN_CONCEPT_FOLDER = "概念"
OBSIDIAN_DOMAIN_FOLDER = "领域"
OBSIDIAN_MAP_NAME = "知识地图.md"
MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm",
    ".mp3", ".wav", ".m4a", ".aac", ".flac",
}


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def space_paths(root: Path) -> dict[str, Path]:
    base = root.expanduser().resolve()
    knowledge = base / KNOWLEDGE_FOLDER
    return {
        "root": base,
        "videos": base / VIDEO_FOLDER,
        "obsidian": base / OBSIDIAN_FOLDER,
        "index": base / INDEX_NAME,
        "work": base / WORK_FOLDER,
        "knowledge": knowledge,
        "sources": knowledge / SOURCES_FOLDER,
        "concepts": knowledge / CONCEPTS_NAME,
        "claims": knowledge / CLAIMS_NAME,
        "relations": knowledge / RELATIONS_NAME,
        "schema": knowledge / SCHEMA_NAME,
        "metadata": knowledge / METADATA_NAME,
        "manifest": knowledge / SPACE_MANIFEST_NAME,
    }


def initialize_space(root: Path) -> dict[str, str]:
    paths = space_paths(root)
    new_index = not paths["index"].exists()
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["videos"].mkdir(parents=True, exist_ok=True)
    paths["obsidian"].mkdir(parents=True, exist_ok=True)
    paths["work"].mkdir(parents=True, exist_ok=True)
    paths["knowledge"].mkdir(parents=True, exist_ok=True)
    paths["sources"].mkdir(parents=True, exist_ok=True)
    _initialize_obsidian_vault(paths["obsidian"])
    if not paths["index"].exists():
        paths["index"].write_text("", encoding="utf-8")
    if not paths["concepts"].exists():
        _atomic_write_text(paths["concepts"], json.dumps({"schema_version": 1, "concepts": []}, ensure_ascii=False, indent=2) + "\n")
    for key in ("claims", "relations"):
        if not paths[key].exists():
            paths[key].write_text("", encoding="utf-8")
    if not paths["schema"].exists():
        _atomic_write_text(
            paths["schema"],
            """# LocalTranscriber 知识编译规则

1. 当前只接收视频来源；每条可回答知识必须引用永久可信证据。
2. 分类只用于辅助组织，无法分类不得阻止视频处理和全局检索。
3. 新视频优先复用已有概念；同义名称记录为别名，不复制概念身份。
4. 来源明确表达的内容标记为 asserted；模型不得用常识补充来源没有的事实。
5. 搜索先读取已编译知识，知识遗漏时必须回退到完整可信转录。
6. 索引和 Obsidian 页面都可重建；视频、可信转录、概念、主张和证据关系不可因清理任务而删除。
7. 每条检索证据必须保留 source_id、evidence_id 和 video_time 定位器。
""",
        )
    if not paths["metadata"].exists():
        _atomic_write_text(
            paths["metadata"],
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "legacy_migration_version": LEGACY_MIGRATION_VERSION if new_index else 0,
                    "obsidian_projection_version": OBSIDIAN_PROJECTION_VERSION if new_index else 0,
                    "obsidian_projection_digest": "",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    if not paths["manifest"].exists():
        _atomic_write_text(
            paths["manifest"],
            json.dumps(
                {
                    "schema_version": 1,
                    "space_id": f"space-{uuid.uuid4().hex}",
                    "created_at": iso_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    return {key: str(value) for key, value in paths.items()}


def load_space_manifest(root: Path) -> dict[str, Any]:
    initialize_space(root)
    payload = _load_json_object(space_paths(root)["manifest"], {})
    space_id = str(payload.get("space_id") or "").strip()
    if not re.fullmatch(r"space-[a-f0-9]{32}", space_id):
        raise ValueError("知识空间身份文件损坏")
    return {
        "schema_version": int(payload.get("schema_version") or 1),
        "space_id": space_id,
        "created_at": str(payload.get("created_at") or ""),
    }


def _atomic_write_text(target: Path, content: str) -> None:
    try:
        if target.is_file() and target.read_text(encoding="utf-8-sig") == content:
            return
    except (OSError, UnicodeError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(target)


def _load_metadata(root: Path) -> dict[str, Any]:
    return _load_json_object(
        space_paths(root)["metadata"],
        {
            "schema_version": SCHEMA_VERSION,
            "legacy_migration_version": 0,
            "obsidian_projection_version": 0,
            "obsidian_projection_digest": "",
        },
    )


def _write_metadata(root: Path, metadata: dict[str, Any]) -> None:
    payload = dict(metadata)
    payload["schema_version"] = SCHEMA_VERSION
    _atomic_write_text(
        space_paths(root)["metadata"],
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _initialize_obsidian_vault(vault: Path) -> None:
    obsidian = vault / ".obsidian"
    obsidian.mkdir(parents=True, exist_ok=True)
    defaults = {
        "app.json": {
            "useMarkdownLinks": False,
            "newLinkFormat": "shortest",
            "showUnsupportedFiles": False,
        },
        "core-plugins.json": {
            "file-explorer": True,
            "global-search": True,
            "graph": True,
            "backlink": True,
            "tag-pane": True,
            "page-preview": True,
            "outgoing-link": True,
        },
        "graph.json": {
            "showTags": False,
            "showAttachments": False,
            "hideUnresolved": True,
            "showOrphans": True,
            "showArrow": True,
            "nodeSizeMultiplier": 1,
            "lineSizeMultiplier": 1,
            "centerStrength": 0.52,
            "repelStrength": 10,
            "linkStrength": 1,
            "linkDistance": 250,
        },
    }
    for name, payload in defaults.items():
        target = obsidian / name
        if not target.exists():
            _atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def fingerprint(path: Path) -> str:
    source = path.expanduser().resolve()
    size = source.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with source.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            handle.seek(max(0, size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()


def discover_videos(inputs: Iterable[str | Path]) -> list[dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    for raw in inputs:
        candidate = Path(raw).expanduser().resolve()
        paths: Iterable[Path]
        if candidate.is_dir():
            paths = candidate.rglob("*")
        elif candidate.is_file():
            paths = [candidate]
        else:
            continue
        for path in paths:
            if not path.is_file() or path.suffix.casefold() not in MEDIA_EXTENSIONS:
                continue
            key = os.path.normcase(str(path.resolve()))
            if key in discovered:
                continue
            stat = path.stat()
            discovered[key] = {
                "source": str(path.resolve()),
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
            }
    values = list(discovered.values())
    values.sort(key=lambda item: str(item["source"]).casefold())
    return values


def _existing_video_by_fingerprint(root: Path, value: str) -> Path | None:
    videos = space_paths(root)["videos"]
    if not videos.is_dir():
        return None
    for path in videos.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in MEDIA_EXTENSIONS:
            continue
        try:
            if fingerprint(path) == value:
                return path.resolve()
        except OSError:
            continue
    return None


def copy_video(
    root: Path,
    source: Path,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    paths = space_paths(root)
    initialize_space(root)
    source_path = source.expanduser().resolve()
    if not source_path.is_file() or source_path.suffix.casefold() not in MEDIA_EXTENSIONS:
        raise FileNotFoundError(f"视频不存在或格式不支持：{source_path}")
    source_fingerprint = fingerprint(source_path)
    existing = _existing_video_by_fingerprint(root, source_fingerprint)
    if existing is not None:
        return {
            "video_id": f"video-{source_fingerprint[:24]}",
            "fingerprint": source_fingerprint,
            "path": str(existing),
            "relative_path": existing.relative_to(paths["root"]).as_posix(),
            "copied": False,
        }
    target = paths["videos"] / source_path.name
    if target.exists():
        target = target.with_name(f"{target.stem}-{source_fingerprint[:8]}{target.suffix}")
    temporary = target.with_suffix(target.suffix + ".copying")
    total = source_path.stat().st_size
    copied = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source_path.open("rb") as reader, temporary.open("wb") as writer:
            while True:
                chunk = reader.read(4 * 1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                copied += len(chunk)
                if progress:
                    progress(copied, total)
        shutil.copystat(source_path, temporary)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "video_id": f"video-{source_fingerprint[:24]}",
        "fingerprint": source_fingerprint,
        "path": str(target.resolve()),
        "relative_path": target.resolve().relative_to(paths["root"]).as_posix(),
        "copied": True,
    }


def load_index(root: Path) -> list[dict[str, Any]]:
    index_path = space_paths(root)["index"]
    if not index_path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(index_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"知识索引第 {line_number} 行损坏") from exc
        if isinstance(value, dict) and value.get("knowledge_id"):
            entries.append(value)
    return entries


def write_index(root: Path, entries: Iterable[dict[str, Any]]) -> Path:
    initialize_space(root)
    target = space_paths(root)["index"]
    normalized = sorted(
        (dict(item) for item in entries),
        key=lambda item: (str(item.get("domain", "")), str(item.get("title", "")), str(item.get("knowledge_id", ""))),
    )
    content = "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in normalized)
    _atomic_write_text(target, content)
    return target


def _load_json_object(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(fallback)
    return payload if isinstance(payload, dict) else dict(fallback)


def _load_jsonl(path: Path, identity: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get(identity):
            values.append(item)
    return values


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]], sort_key: str) -> None:
    content = "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in sorted((dict(value) for value in values), key=lambda value: str(value.get(sort_key) or ""))
    )
    _atomic_write_text(path, content)


def load_concepts(root: Path) -> list[dict[str, Any]]:
    payload = _load_json_object(space_paths(root)["concepts"], {"schema_version": 1, "concepts": []})
    values = payload.get("concepts")
    return [dict(item) for item in values if isinstance(item, dict) and item.get("concept_id")] if isinstance(values, list) else []


def write_concepts(root: Path, concepts: Iterable[dict[str, Any]]) -> Path:
    initialize_space(root)
    target = space_paths(root)["concepts"]
    values = sorted(
        (dict(item) for item in concepts),
        key=lambda item: str(item.get("canonical_name") or "").casefold(),
    )
    _atomic_write_text(
        target,
        json.dumps({"schema_version": 1, "updated_at": iso_now(), "concepts": values}, ensure_ascii=False, indent=2) + "\n",
    )
    return target


def load_claims(root: Path) -> list[dict[str, Any]]:
    return _load_jsonl(space_paths(root)["claims"], "claim_id")


def load_relations(root: Path) -> list[dict[str, Any]]:
    return _load_jsonl(space_paths(root)["relations"], "relation_id")


def _evidence_id(video_id: str, segment_id: str) -> str:
    digest = hashlib.sha256(f"{video_id}|{segment_id}".encode("utf-8")).hexdigest()[:24]
    return f"evidence-{digest}"


def _claim_id(concept_id: str, statement: str) -> str:
    normalized = unicodedata.normalize("NFKC", statement).casefold()
    normalized = re.sub(r"\s+|[，。！？、；：,.!?;:]", "", normalized)
    digest = hashlib.sha256(f"{concept_id}|{normalized}".encode("utf-8")).hexdigest()[:24]
    return f"claim-{digest}"


def _source_directory(root: Path, video_id: str) -> Path:
    return space_paths(root)["sources"] / _safe_component(video_id, "video")


def _persist_trusted_source(
    root: Path,
    *,
    video_id: str,
    video_fingerprint: str,
    video_relative_path: str,
    source_title: str,
    domain: str,
    verified_payload: dict[str, Any],
    trusted_segments: list[dict[str, Any]],
    raw_transcript_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Persist evidence outside the disposable task directory before publishing knowledge."""
    directory = _source_directory(root, video_id)
    directory.mkdir(parents=True, exist_ok=True)
    source_record = {
        "schema_version": 1,
        "source_id": video_id,
        "source_type": "video",
        "title": source_title,
        "domain": domain,
        "video_fingerprint": video_fingerprint,
        "video_relative_path": video_relative_path,
        "updated_at": iso_now(),
    }
    _atomic_write_text(directory / "source.json", json.dumps(source_record, ensure_ascii=False, indent=2) + "\n")
    durable_verified = dict(verified_payload)
    durable_verified.update(
        schema_version=1,
        source_id=video_id,
        source_type="video",
        video_fingerprint=video_fingerprint,
        video_relative_path=video_relative_path,
    )
    _atomic_write_text(
        directory / "transcript.verified.json",
        json.dumps(durable_verified, ensure_ascii=False, indent=2) + "\n",
    )
    if raw_transcript_path and raw_transcript_path.is_file():
        try:
            raw_text = raw_transcript_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raw_text = ""
        if raw_text:
            _atomic_write_text(directory / "transcript.raw.json", raw_text)
    evidence_units = [
        {
            "schema_version": 1,
            "evidence_id": _evidence_id(video_id, str(segment["id"])),
            "source_id": video_id,
            "source_type": "video",
            "segment_id": str(segment["id"]),
            "text": str(segment["text"]),
            "locator": {
                "type": "video_time",
                "start": round(float(segment["start"]), 3),
                "end": round(float(segment["end"]), 3),
            },
            "video_relative_path": video_relative_path,
            "source_title": source_title,
            "domain": domain,
            "verification_status": "verified",
        }
        for segment in trusted_segments
    ]
    _write_jsonl(directory / "evidence-units.jsonl", evidence_units, "evidence_id")
    metadata = _load_metadata(root)
    metadata["evidence_updated_at"] = iso_now()
    _write_metadata(root, metadata)
    return evidence_units


def load_evidence_units(root: Path) -> list[dict[str, Any]]:
    sources = space_paths(root)["sources"]
    values: list[dict[str, Any]] = []
    if not sources.is_dir():
        return values
    for path in sorted(sources.glob("*/evidence-units.jsonl")):
        values.extend(_load_jsonl(path, "evidence_id"))
    return values


def archive_trusted_source(
    root: Path,
    video: Path,
    verified_path: Path,
    raw_transcript_path: Path | None = None,
) -> dict[str, Any]:
    """Upgrade an already completed task artifact into the permanent evidence store without an LLM call."""
    paths = space_paths(root)
    initialize_space(root)
    video_path = video.expanduser().resolve()
    video_fingerprint = fingerprint(video_path)
    video_id = f"video-{video_fingerprint[:24]}"
    try:
        video_relative = video_path.relative_to(paths["root"]).as_posix()
    except ValueError as exc:
        raise ValueError("归档视频必须位于当前知识空间内") from exc
    verified_payload, segments = _trusted_segments(verified_path)
    related = [item for item in load_index(root) if str(item.get("video_id") or "") == video_id]
    source_title = str(related[0].get("source_title") or video_path.stem) if related else video_path.stem
    domain = str(related[0].get("domain") or "未分类") if related else "未分类"
    evidence = _persist_trusted_source(
        root,
        video_id=video_id,
        video_fingerprint=video_fingerprint,
        video_relative_path=video_relative,
        source_title=source_title,
        domain=domain,
        verified_payload=verified_payload,
        trusted_segments=segments,
        raw_transcript_path=raw_transcript_path,
    )
    rebuild_obsidian_wiki(root)
    return {"video_id": video_id, "evidence_count": len(evidence)}


def _safe_component(value: str, fallback: str = "未分类") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value.strip()).strip(" .-")
    return (text or fallback)[:80]


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _concept_key(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(title or "")).casefold()
    return re.sub(r"\s+", "", normalized)


def _concept_lookup(concepts: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for concept in concepts:
        names = [concept.get("canonical_name"), *list(concept.get("aliases") or [])]
        for name in names:
            key = _concept_key(str(name or ""))
            if key:
                lookup.setdefault(key, concept)
    return lookup


def _resolve_concept(
    concepts: list[dict[str, Any]],
    title: str,
    aliases: Iterable[str],
    domain: str,
    video_id: str,
    lookup: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    clean_title = re.sub(r"\s+", " ", str(title or "").strip()) or "未命名知识"
    clean_aliases = [
        re.sub(r"\s+", " ", str(item).strip())
        for item in aliases
        if str(item).strip()
    ]
    active_lookup = lookup if lookup is not None else _concept_lookup(concepts)
    concept = active_lookup.get(_concept_key(clean_title))
    if concept is None:
        for alias in clean_aliases:
            concept = active_lookup.get(_concept_key(alias))
            if concept is not None:
                break
    if concept is None:
        digest = hashlib.sha256(_concept_key(clean_title).encode("utf-8")).hexdigest()[:24]
        concept = {
            "concept_id": f"concept-{digest}",
            "canonical_name": clean_title,
            "aliases": [],
            "domains": [],
            "source_ids": [],
            "created_at": iso_now(),
        }
        concepts.append(concept)
    canonical_key = _concept_key(str(concept.get("canonical_name") or clean_title))
    merged_aliases = [str(item).strip() for item in concept.get("aliases", []) if str(item).strip()]
    if _concept_key(clean_title) != canonical_key:
        merged_aliases.append(clean_title)
    merged_aliases.extend(clean_aliases)
    concept["aliases"] = list(
        dict.fromkeys(item for item in merged_aliases if _concept_key(item) != canonical_key)
    )[:30]
    domains = [str(item).strip() for item in concept.get("domains", []) if str(item).strip()]
    if domain and domain != "未分类":
        domains.append(domain)
    concept["domains"] = list(dict.fromkeys(domains))
    source_ids = [str(item) for item in concept.get("source_ids", []) if str(item)]
    source_ids.append(video_id)
    concept["source_ids"] = list(dict.fromkeys(source_ids))
    concept["updated_at"] = iso_now()
    for name in [concept.get("canonical_name"), *list(concept.get("aliases") or [])]:
        key = _concept_key(str(name or ""))
        if key:
            active_lookup.setdefault(key, concept)
    return concept


def _existing_concept_context(root: Path, text: str = "", limit: int = 160) -> list[dict[str, Any]]:
    concepts = load_concepts(root)
    terms = _query_terms(text)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in concepts:
        names = " ".join(
            [str(item.get("canonical_name") or ""), *[str(alias) for alias in item.get("aliases", [])]]
        )
        score = len(terms & _query_terms(names)) if terms else 0
        ranked.append((score, item))
    ranked.sort(
        key=lambda pair: (
            -pair[0],
            str(pair[1].get("canonical_name") or "").casefold(),
        )
    )
    selected = [item for score, item in ranked if score > 0][: max(1, limit)]
    if len(selected) < min(40, limit):
        selected_ids = {str(item.get("concept_id") or "") for item in selected}
        recent = sorted(
            concepts,
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        selected.extend(item for item in recent if str(item.get("concept_id") or "") not in selected_ids)
        selected = selected[: min(40, limit)]
    return [
        {
            "concept_id": item.get("concept_id"),
            "canonical_name": item.get("canonical_name"),
            "aliases": list(item.get("aliases") or [])[:12],
        }
        for item in selected
    ]


def _vault_relative(relative: str) -> str:
    value = str(relative or "").replace("\\", "/").strip("/")
    prefix = f"{OBSIDIAN_FOLDER}/"
    return value[len(prefix) :] if value.startswith(prefix) else value


def _wiki_link(relative: str, label: str) -> str:
    target = _vault_relative(relative)
    if target.casefold().endswith(".md"):
        target = target[:-3]
    safe_label = str(label or "").replace("|", "-").strip()
    return f"[[{target}|{safe_label}]]"


def _source_path_for_entry(entry: dict[str, Any]) -> str:
    explicit = str(entry.get("source_obsidian_relative_path") or "").replace("\\", "/")
    if explicit:
        return explicit
    legacy = str(entry.get("obsidian_relative_path") or "").replace("\\", "/")
    concept_prefix = f"{OBSIDIAN_FOLDER}/{OBSIDIAN_CONCEPT_FOLDER}/"
    if legacy and not legacy.startswith(concept_prefix):
        return legacy
    domain = _safe_component(str(entry.get("domain") or "未分类"))
    title = _safe_component(
        str(entry.get("source_title") or Path(str(entry.get("video_relative_path") or "视频")).stem)
    )
    return f"{OBSIDIAN_FOLDER}/{domain}/{title}.md"


def _source_title_for_entry(entry: dict[str, Any]) -> str:
    title = str(entry.get("source_title") or "").strip()
    if title:
        return title
    return Path(_source_path_for_entry(entry)).stem


def _existing_source_summary(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""
    if content.startswith("---\n"):
        frontmatter_end = content.find("\n---\n", 4)
        if frontmatter_end >= 0:
            content = content[frontmatter_end + 5 :]
    heading = re.search(r"^#\s+.+$", content, flags=re.MULTILINE)
    if heading:
        content = content[heading.end() :]
    next_section = re.search(r"^##\s+", content, flags=re.MULTILINE)
    if next_section:
        content = content[: next_section.start()]
    return content.strip()


def _assign_obsidian_paths(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    values = [dict(item) for item in entries]
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in values:
        title = str(entry.get("title") or "未命名知识").strip() or "未命名知识"
        group_key = str(entry.get("concept_id") or _concept_key(title))
        groups.setdefault(group_key, []).append(entry)

    used_names: dict[str, str] = {}
    concept_paths: dict[str, str] = {}
    for key in sorted(groups):
        display_title = str(
            groups[key][0].get("concept_name") or groups[key][0].get("title") or "未命名知识"
        ).strip() or "未命名知识"
        filename = _safe_component(display_title, "未命名知识")
        collision_key = filename.casefold()
        if collision_key in used_names and used_names[collision_key] != key:
            suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
            filename = f"{filename}-{suffix}"
            collision_key = filename.casefold()
        used_names[collision_key] = key
        concept_paths[key] = f"{OBSIDIAN_FOLDER}/{OBSIDIAN_CONCEPT_FOLDER}/{filename}.md"

    for entry in values:
        group_key = str(entry.get("concept_id") or _concept_key(str(entry.get("title") or "")))
        entry["source_obsidian_relative_path"] = _source_path_for_entry(entry)
        entry["source_title"] = _source_title_for_entry(entry)
        entry["obsidian_relative_path"] = concept_paths[group_key]
    return values


def _render_obsidian_wiki(
    root: Path,
    raw_entries: Iterable[dict[str, Any]],
    *,
    affected_sources: set[str] | None = None,
    affected_concepts: set[str] | None = None,
    affected_domains: set[str] | None = None,
) -> list[dict[str, Any]]:
    paths = space_paths(root)
    entries = _assign_obsidian_paths(raw_entries)
    concepts_by_id = {str(item.get("concept_id")): item for item in load_concepts(root)}
    source_groups: dict[str, list[dict[str, Any]]] = {}
    concept_groups: dict[str, list[dict[str, Any]]] = {}
    domain_sources: dict[str, set[str]] = {}
    domain_concepts: dict[str, set[str]] = {}
    for entry in entries:
        source_relative = str(entry["source_obsidian_relative_path"])
        concept_relative = str(entry["obsidian_relative_path"])
        domain = str(entry.get("domain") or "未分类").strip() or "未分类"
        source_groups.setdefault(source_relative, []).append(entry)
        concept_groups.setdefault(concept_relative, []).append(entry)
        domain_sources.setdefault(domain, set()).add(source_relative)
        domain_concepts.setdefault(domain, set()).add(concept_relative)

    domain_paths = {
        domain: f"{OBSIDIAN_FOLDER}/{OBSIDIAN_DOMAIN_FOLDER}/{_safe_component(domain)}.md"
        for domain in domain_sources
    }
    map_lines = [
        "---",
        "type: map",
        "generated_by: LocalTranscriber",
        "---",
        "",
        "# 知识地图",
        "",
        "这里展示由可信视频知识索引生成的 Wiki 关系。打开 Obsidian 的“关系图谱”可以查看完整知识网络。",
        "",
        "## 领域入口",
        "",
    ]
    for domain in sorted(domain_paths, key=str.casefold):
        map_lines.append(f"- {_wiki_link(domain_paths[domain], domain)}")
    map_lines.extend(
        [
            "",
            "## 当前规模",
            "",
            f"- 视频来源：{len(source_groups)}",
            f"- 独立概念：{len(concept_groups)}",
            f"- 可检索知识：{len(entries)}",
            "",
        ]
    )
    _atomic_write_text(paths["obsidian"] / OBSIDIAN_MAP_NAME, "\n".join(map_lines))

    for domain, source_paths in domain_sources.items():
        if affected_domains is not None and domain not in affected_domains:
            continue
        lines = [
            "---",
            "type: domain",
            f"title: {_yaml_string(domain)}",
            "generated_by: LocalTranscriber",
            "---",
            "",
            f"# {domain}",
            "",
            "## 视频来源",
            "",
        ]
        for source_relative in sorted(source_paths, key=str.casefold):
            source_title = _source_title_for_entry(source_groups[source_relative][0])
            lines.append(f"- {_wiki_link(source_relative, source_title)}")
        lines.extend(["", "## 核心概念", ""])
        for concept_relative in sorted(domain_concepts.get(domain, set()), key=str.casefold):
            concept_title = str(concept_groups[concept_relative][0].get("title") or Path(concept_relative).stem)
            lines.append(f"- {_wiki_link(concept_relative, concept_title)}")
        lines.append("")
        target = paths["root"] / Path(domain_paths[domain])
        _atomic_write_text(target, "\n".join(lines))

    for source_relative, group in source_groups.items():
        if affected_sources is not None and source_relative not in affected_sources:
            continue
        ordered = sorted(group, key=lambda item: (int(item.get("knowledge_order") or 0), str(item.get("title") or "")))
        first = ordered[0]
        source_title = _source_title_for_entry(first)
        domain = str(first.get("domain") or "未分类")
        summaries = list(dict.fromkeys(str(item.get("source_summary") or "").strip() for item in ordered))
        summaries = [item for item in summaries if item]
        source_target = paths["root"] / Path(source_relative)
        if not summaries:
            existing_summary = _existing_source_summary(source_target)
            if existing_summary:
                summaries.append(existing_summary)
        concept_links: dict[str, str] = {}
        for entry in ordered:
            concept_links[str(entry["obsidian_relative_path"])] = str(entry.get("title") or "未命名知识")
        lines = [
            "---",
            "type: source",
            f"title: {_yaml_string(source_title)}",
            f"video_id: {_yaml_string(str(first.get('video_id') or ''))}",
            f"video_fingerprint: {_yaml_string(str(first.get('video_fingerprint') or ''))}",
            f"video: {_yaml_string(str(first.get('video_relative_path') or ''))}",
            f"domain: {_yaml_string(domain)}",
            "generated_by: LocalTranscriber",
            "---",
            "",
            f"# {source_title}",
            "",
        ]
        if summaries:
            lines.extend(["\n\n".join(summaries), ""])
        lines.extend(["## 知识分支", ""])
        for concept_relative, title in concept_links.items():
            lines.append(f"- {_wiki_link(concept_relative, title)}")
        lines.extend(["", "## 知识与视频证据", ""])
        for entry in ordered:
            concept_link = _wiki_link(str(entry["obsidian_relative_path"]), str(entry.get("title") or "未命名知识"))
            lines.extend(
                [
                    f"### {concept_link}",
                    "",
                    str(entry.get("content") or ""),
                    "",
                    f"> [!quote] {format_timestamp(float(entry.get('evidence_start') or 0))}–{format_timestamp(float(entry.get('evidence_end') or 0))}",
                    f"> {str(entry.get('evidence_text') or '')}",
                    "",
                ]
            )
        _atomic_write_text(source_target, "\n".join(lines).rstrip() + "\n")

    for concept_relative, group in concept_groups.items():
        if affected_concepts is not None and concept_relative not in affected_concepts:
            continue
        ordered = sorted(
            group,
            key=lambda item: (
                str(item.get("domain") or "").casefold(),
                _source_title_for_entry(item).casefold(),
                float(item.get("evidence_start") or 0),
            ),
        )
        concept_record = concepts_by_id.get(str(ordered[0].get("concept_id") or ""), {})
        title = str(
            concept_record.get("canonical_name")
            or ordered[0].get("concept_name")
            or ordered[0].get("title")
            or Path(concept_relative).stem
        )
        aliases = [str(item) for item in concept_record.get("aliases", []) if str(item).strip()]
        domains = sorted({str(item.get("domain") or "未分类") for item in ordered}, key=str.casefold)
        lines = [
            "---",
            "type: concept",
            f"title: {_yaml_string(title)}",
            f"domains: {json.dumps(domains, ensure_ascii=False)}",
            f"aliases: {json.dumps(aliases, ensure_ascii=False)}",
            "generated_by: LocalTranscriber",
            "---",
            "",
            f"# {title}",
            "",
            "这个概念由以下可信视频证据持续汇聚。",
            "",
        ]
        distinct_claims: list[str] = []
        for entry in ordered:
            content = str(entry.get("content") or "").strip()
            if content and content not in distinct_claims:
                distinct_claims.append(content)
        if distinct_claims:
            lines.extend(["## 当前知识", ""])
            lines.extend(f"- {content}" for content in distinct_claims)
            lines.extend(["", "## 来源证据", ""])
        for entry in ordered:
            source_title = _source_title_for_entry(entry)
            source_link = _wiki_link(str(entry["source_obsidian_relative_path"]), source_title)
            lines.extend(
                [
                    f"## 来自 {source_link}",
                    "",
                    str(entry.get("content") or ""),
                    "",
                    f"> [!quote] {format_timestamp(float(entry.get('evidence_start') or 0))}–{format_timestamp(float(entry.get('evidence_end') or 0))}",
                    f"> {str(entry.get('evidence_text') or '')}",
                    "",
                ]
            )
        _atomic_write_text(paths["root"] / Path(concept_relative), "\n".join(lines).rstrip() + "\n")
    _render_wiki_navigation(paths["obsidian"], entries, concepts_by_id)
    return entries


def _remove_generated_obsidian_page(root: Path, relative: str) -> None:
    paths = space_paths(root)
    vault = paths["obsidian"].resolve()
    target = (paths["root"] / Path(relative)).resolve()
    try:
        target.relative_to(vault)
    except ValueError:
        return
    if not target.is_file():
        return
    try:
        content = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return
    if "generated_by: LocalTranscriber" in content:
        target.unlink(missing_ok=True)


def _render_obsidian_incremental(
    root: Path,
    previous_entries: Iterable[dict[str, Any]],
    next_entries: Iterable[dict[str, Any]],
    changed_video_id: str,
) -> list[dict[str, Any]]:
    previous = _assign_obsidian_paths(previous_entries)
    current = _assign_obsidian_paths(next_entries)
    affected_sources = {
        str(item.get("source_obsidian_relative_path") or "")
        for item in [*previous, *current]
        if str(item.get("video_id") or "") == changed_video_id
    }
    affected_concepts = {
        str(item.get("obsidian_relative_path") or "")
        for item in [*previous, *current]
        if str(item.get("video_id") or "") == changed_video_id
    }
    affected_domains = {
        str(item.get("domain") or "未分类").strip() or "未分类"
        for item in [*previous, *current]
        if str(item.get("video_id") or "") == changed_video_id
    }
    previous_by_id = {str(item.get("knowledge_id") or ""): item for item in previous}
    for item in current:
        old = previous_by_id.get(str(item.get("knowledge_id") or ""))
        if old is None:
            continue
        old_source = str(old.get("source_obsidian_relative_path") or "")
        new_source = str(item.get("source_obsidian_relative_path") or "")
        old_concept = str(old.get("obsidian_relative_path") or "")
        new_concept = str(item.get("obsidian_relative_path") or "")
        if old_source != new_source:
            affected_sources.update((old_source, new_source))
        if old_concept != new_concept:
            affected_concepts.update((old_concept, new_concept))
        if old_source != new_source or old_concept != new_concept:
            affected_domains.update(
                (
                    str(old.get("domain") or "未分类").strip() or "未分类",
                    str(item.get("domain") or "未分类").strip() or "未分类",
                )
            )
    affected_sources.discard("")
    affected_concepts.discard("")
    rendered = _render_obsidian_wiki(
        root,
        current,
        affected_sources=affected_sources,
        affected_concepts=affected_concepts,
        affected_domains=affected_domains,
    )
    current_sources = {str(item.get("source_obsidian_relative_path") or "") for item in rendered}
    current_concepts = {str(item.get("obsidian_relative_path") or "") for item in rendered}
    current_domains = {str(item.get("domain") or "未分类").strip() or "未分类" for item in rendered}
    for relative in affected_sources - current_sources:
        _remove_generated_obsidian_page(root, relative)
    for relative in affected_concepts - current_concepts:
        _remove_generated_obsidian_page(root, relative)
    for domain in affected_domains - current_domains:
        relative = f"{OBSIDIAN_FOLDER}/{OBSIDIAN_DOMAIN_FOLDER}/{_safe_component(domain)}.md"
        _remove_generated_obsidian_page(root, relative)
    return rendered


def _render_wiki_navigation(
    vault: Path,
    entries: list[dict[str, Any]],
    concepts_by_id: dict[str, dict[str, Any]],
) -> None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        key = str(entry.get("concept_id") or entry.get("obsidian_relative_path") or entry.get("title") or "")
        groups.setdefault(key, []).append(entry)
    lines = [
        "---",
        "type: index",
        "generated_by: LocalTranscriber",
        "---",
        "",
        "# 知识索引",
        "",
        "这里是当前知识空间的自动导航。分类只用于辅助浏览，不限制全局检索。",
        "",
    ]
    by_domain: dict[str, list[tuple[str, str, int]]] = {}
    for key, group in groups.items():
        first = group[0]
        concept = concepts_by_id.get(str(first.get("concept_id") or ""), {})
        title = str(concept.get("canonical_name") or first.get("concept_name") or first.get("title") or "未命名知识")
        relative = str(first.get("obsidian_relative_path") or "")
        aliases = [str(item) for item in concept.get("aliases", []) if str(item).strip()]
        label = title + (f"（别名：{'、'.join(aliases[:5])}）" if aliases else "")
        domains = [str(item) for item in concept.get("domains", []) if str(item).strip()]
        if not domains:
            domains = sorted({str(item.get("domain") or "未分类") for item in group})
        for domain in domains or ["未分类"]:
            by_domain.setdefault(domain, []).append((title, relative, len(group)))
        concept["_index_label"] = label
    for domain in sorted(by_domain, key=str.casefold):
        lines.extend([f"## {domain}", ""])
        for title, relative, count in sorted(by_domain[domain], key=lambda item: item[0].casefold()):
            lines.append(f"- {_wiki_link(relative, title)} · {count} 条可信知识")
        lines.append("")
    _atomic_write_text(vault / WIKI_INDEX_NAME, "\n".join(lines).rstrip() + "\n")


def _append_wiki_log(root: Path, source_title: str, domain: str, units: list[dict[str, Any]]) -> None:
    target = space_paths(root)["obsidian"] / WIKI_LOG_NAME
    existing = target.read_text(encoding="utf-8-sig") if target.is_file() else "# 知识增长日志\n\n"
    marker = f"source_id: {units[0].get('video_id')}" if units else ""
    if marker and marker in existing:
        return
    lines = [
        f"## [{datetime.now().astimezone().date().isoformat()}] ingest | {source_title}",
        "",
        f"- 领域：{domain or '未分类'}",
        f"- 新增或更新知识：{len(units)} 条",
    ]
    titles = list(dict.fromkeys(str(item.get("title") or "") for item in units if str(item.get("title") or "").strip()))
    if titles:
        lines.append(f"- 涉及概念：{'、'.join(titles[:30])}")
    if marker:
        lines.append(f"- {marker}")
    lines.extend(["", ""])
    _atomic_write_text(target, existing.rstrip() + "\n\n" + "\n".join(lines))


def _upgrade_legacy_knowledge(root: Path, raw_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach stable concepts, claims, and evidence locators to pre-V2 index rows without model calls."""
    if not raw_entries:
        return []
    paths = space_paths(root)
    concepts = load_concepts(root)
    concept_lookup = _concept_lookup(concepts)
    entries: list[dict[str, Any]] = []
    evidence_by_source: dict[str, list[dict[str, Any]]] = {}
    for raw in raw_entries:
        entry = dict(raw)
        video_id = str(entry.get("video_id") or "")
        concept = _resolve_concept(
            concepts,
            str(entry.get("concept_name") or entry.get("title") or "未命名知识"),
            entry.get("aliases", []),
            str(entry.get("domain") or "未分类"),
            video_id,
            concept_lookup,
        )
        entry["concept_id"] = str(concept["concept_id"])
        entry["concept_name"] = str(concept.get("canonical_name") or entry.get("title") or "未命名知识")
        entry["aliases"] = list(concept.get("aliases") or [])
        entry["title"] = entry["concept_name"]
        segment_seed = hashlib.sha256(
            f"{entry.get('evidence_start')}|{entry.get('evidence_end')}|{entry.get('evidence_text')}".encode("utf-8")
        ).hexdigest()[:16]
        evidence_id = str((entry.get("evidence_ids") or [""])[0] or _evidence_id(video_id, f"legacy-{segment_seed}"))
        entry["evidence_ids"] = list(dict.fromkeys([*list(entry.get("evidence_ids") or []), evidence_id]))
        locator = {
            "type": "video_time",
            "start": round(float(entry.get("evidence_start") or 0), 3),
            "end": round(float(entry.get("evidence_end") or entry.get("evidence_start") or 0), 3),
        }
        if not entry.get("source_refs"):
            entry["source_refs"] = [
                {
                    "source_id": video_id,
                    "evidence_id": evidence_id,
                    "segment_id": f"legacy-{segment_seed}",
                    "locator": locator,
                }
            ]
        entry["claim_id"] = str(entry.get("claim_id") or _claim_id(str(concept["concept_id"]), str(entry.get("content") or "")))
        evidence_by_source.setdefault(video_id, []).append(
            {
                "schema_version": 1,
                "evidence_id": evidence_id,
                "source_id": video_id,
                "source_type": "video",
                "segment_id": f"legacy-{segment_seed}",
                "text": str(entry.get("evidence_text") or entry.get("content") or ""),
                "locator": locator,
                "video_relative_path": str(entry.get("video_relative_path") or ""),
                "source_title": str(entry.get("source_title") or Path(str(entry.get("video_relative_path") or "视频")).stem),
                "domain": str(entry.get("domain") or "未分类"),
                "verification_status": "trusted_index",
            }
        )
        entries.append(entry)

    write_concepts(root, concepts)
    claims: dict[str, dict[str, Any]] = {}
    for entry in entries:
        claim_id = str(entry["claim_id"])
        claim = claims.setdefault(
            claim_id,
            {
                "schema_version": 1,
                "claim_id": claim_id,
                "concept_id": str(entry.get("concept_id") or ""),
                "statement": str(entry.get("content") or ""),
                "origin": "asserted",
                "status": "active",
                "source_ids": [],
                "evidence_ids": [],
            },
        )
        claim["source_ids"] = list(dict.fromkeys([*claim["source_ids"], str(entry.get("video_id") or "")]))
        claim["evidence_ids"] = list(dict.fromkeys([*claim["evidence_ids"], *list(entry.get("evidence_ids") or [])]))
        claim["updated_at"] = iso_now()
    _write_jsonl(paths["claims"], claims.values(), "claim_id")
    for source_id, evidence_units in evidence_by_source.items():
        if not source_id:
            continue
        directory = _source_directory(root, source_id)
        directory.mkdir(parents=True, exist_ok=True)
        existing = _load_jsonl(directory / "evidence-units.jsonl", "evidence_id")
        merged = {str(item["evidence_id"]): item for item in [*evidence_units, *existing] if item.get("evidence_id")}
        _write_jsonl(directory / "evidence-units.jsonl", merged.values(), "evidence_id")
        source_file = directory / "source.json"
        if not source_file.exists():
            first = evidence_units[0]
            _atomic_write_text(
                source_file,
                json.dumps(
                    {
                        "schema_version": 1,
                        "source_id": source_id,
                        "source_type": "video",
                        "title": first["source_title"],
                        "domain": first["domain"],
                        "video_relative_path": first["video_relative_path"],
                        "migration_status": "legacy_index_only",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
    return entries


def _entry_needs_legacy_migration(entry: dict[str, Any]) -> bool:
    return not (
        entry.get("concept_id")
        and entry.get("claim_id")
        and isinstance(entry.get("evidence_ids"), list)
        and isinstance(entry.get("source_refs"), list)
    )


def _ensure_legacy_migrated(root: Path, raw_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metadata = _load_metadata(root)
    if int(metadata.get("legacy_migration_version") or 0) >= LEGACY_MIGRATION_VERSION:
        return [dict(item) for item in raw_entries]
    if any(_entry_needs_legacy_migration(item) for item in raw_entries):
        entries = _upgrade_legacy_knowledge(root, raw_entries)
        write_index(root, entries)
    else:
        entries = [dict(item) for item in raw_entries]
    metadata["legacy_migration_version"] = LEGACY_MIGRATION_VERSION
    metadata["legacy_migrated_at"] = iso_now()
    _write_metadata(root, metadata)
    return entries


def _projection_digest(entries: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in sorted((dict(item) for item in entries), key=lambda item: str(item.get("knowledge_id") or "")):
        digest.update(json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _projection_sentinels_exist(root: Path, entries: list[dict[str, Any]]) -> bool:
    paths = space_paths(root)
    required = [paths["obsidian"] / OBSIDIAN_MAP_NAME, paths["obsidian"] / WIKI_INDEX_NAME]
    if entries:
        assigned = _assign_obsidian_paths(entries)
        first = assigned[0]
        required.extend(
            (
                paths["root"] / Path(str(first.get("source_obsidian_relative_path") or "")),
                paths["root"] / Path(str(first.get("obsidian_relative_path") or "")),
            )
        )
    return all(path.is_file() for path in required)


def _all_projection_pages_exist(root: Path, entries: list[dict[str, Any]]) -> bool:
    if not entries:
        return True
    paths = space_paths(root)
    assigned = _assign_obsidian_paths(entries)
    required = {
        paths["obsidian"] / OBSIDIAN_MAP_NAME,
        paths["obsidian"] / WIKI_INDEX_NAME,
        *(
            paths["root"] / Path(str(item.get("source_obsidian_relative_path") or ""))
            for item in assigned
        ),
        *(
            paths["root"] / Path(str(item.get("obsidian_relative_path") or ""))
            for item in assigned
        ),
        *(
            paths["obsidian"] / OBSIDIAN_DOMAIN_FOLDER / f"{_safe_component(str(item.get('domain') or '未分类'))}.md"
            for item in assigned
        ),
    }
    return all(path.is_file() for path in required)


def inspect_space_integrity(root: Path, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    values = [dict(item) for item in (entries if entries is not None else load_index(root))]
    metadata = _load_metadata(root)
    migration_required = (
        int(metadata.get("legacy_migration_version") or 0) < LEGACY_MIGRATION_VERSION
        and any(_entry_needs_legacy_migration(item) for item in values)
    )
    digest = _projection_digest(values)
    projection_version_current = (
        int(metadata.get("obsidian_projection_version") or 0) >= OBSIDIAN_PROJECTION_VERSION
    )
    projection_stale = not (
        projection_version_current
        and (
            not values
            or (
                str(metadata.get("obsidian_projection_digest") or "") == digest
                and _projection_sentinels_exist(root, values)
            )
        )
    )
    return {
        "ok": not migration_required and not projection_stale,
        "migration_required": migration_required,
        "projection_stale": projection_stale,
        "knowledge_count": len(values),
    }


def reconcile_space_metadata(root: Path, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    values = [dict(item) for item in (entries if entries is not None else load_index(root))]
    metadata = _load_metadata(root)
    changed = False
    if (
        int(metadata.get("legacy_migration_version") or 0) < LEGACY_MIGRATION_VERSION
        and not any(_entry_needs_legacy_migration(item) for item in values)
    ):
        metadata["legacy_migration_version"] = LEGACY_MIGRATION_VERSION
        metadata["legacy_migrated_at"] = iso_now()
        changed = True
    digest = _projection_digest(values)
    projection_recorded = (
        int(metadata.get("obsidian_projection_version") or 0) >= OBSIDIAN_PROJECTION_VERSION
        and str(metadata.get("obsidian_projection_digest") or "") == digest
    )
    if not projection_recorded and _all_projection_pages_exist(root, values):
        metadata.update(
            obsidian_projection_version=OBSIDIAN_PROJECTION_VERSION,
            obsidian_projection_digest=digest,
            obsidian_projected_at=iso_now(),
            knowledge_count=len(values),
        )
        changed = True
    if changed:
        _write_metadata(root, metadata)
    return inspect_space_integrity(root, values)


def _mark_projection_current(root: Path, entries: list[dict[str, Any]]) -> None:
    metadata = _load_metadata(root)
    metadata.update(
        obsidian_projection_version=OBSIDIAN_PROJECTION_VERSION,
        obsidian_projection_digest=_projection_digest(entries),
        obsidian_projected_at=iso_now(),
        knowledge_count=len(entries),
    )
    _write_metadata(root, metadata)


def rebuild_obsidian_wiki(root: Path, *, force: bool = False) -> dict[str, int]:
    initialize_space(root)
    entries = _ensure_legacy_migrated(root, load_index(root))
    if not entries:
        return {"knowledge_count": 0, "source_count": 0, "concept_count": 0}
    assigned = _assign_obsidian_paths(entries)
    if not force and not inspect_space_integrity(root, assigned)["projection_stale"]:
        return {
            "knowledge_count": len(assigned),
            "source_count": len({str(item.get("source_obsidian_relative_path") or "") for item in assigned}),
            "concept_count": len({str(item.get("obsidian_relative_path") or "") for item in assigned}),
        }
    rendered = _render_obsidian_wiki(root, assigned)
    write_index(root, rendered)
    _mark_projection_current(root, rendered)
    return {
        "knowledge_count": len(rendered),
        "source_count": len({str(item.get("source_obsidian_relative_path") or "") for item in rendered}),
        "concept_count": len({str(item.get("obsidian_relative_path") or "") for item in rendered}),
    }


def _trusted_segments(verified_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(verified_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("可信结果格式无效")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("可信结果缺少分段")
    segments = []
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict) or raw.get("knowledge_ready") is not True:
            continue
        text = str(raw.get("final_text") or "").strip()
        if not text:
            continue
        segments.append(
            {
                "id": str(raw.get("id", index)),
                "start": float(raw.get("start", 0.0)),
                "end": float(raw.get("end", raw.get("start", 0.0))),
                "text": text,
            }
        )
    if not segments:
        raise ValueError("没有通过可信校对且可用于知识生成的片段")
    return payload, segments


def _segment_chunks(segments: list[dict[str, Any]], max_chars: int = 12_000) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for segment in segments:
        next_size = len(str(segment["text"])) + 80
        if current and size + next_size > max_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append(segment)
        size += next_size
    if current:
        chunks.append(current)
    return chunks


def publish_verified(
    root: Path,
    video: Path,
    verified_path: Path,
    client: Any,
    domain_hint: str = "",
    raw_transcript_path: Path | None = None,
) -> dict[str, Any]:
    paths = space_paths(root)
    initialize_space(root)
    video_path = video.expanduser().resolve()
    try:
        video_relative = video_path.relative_to(paths["root"]).as_posix()
    except ValueError as exc:
        raise ValueError("发布视频必须位于当前知识空间内") from exc
    video_fingerprint = fingerprint(video_path)
    video_id = f"video-{video_fingerprint[:24]}"
    verified_payload, segments = _trusted_segments(verified_path)
    allowed = {item["id"]: item for item in segments}
    units: list[dict[str, Any]] = []
    confirmed_domain = re.sub(r"\s+", " ", str(domain_hint or "").strip())[:80]
    domain = confirmed_domain or "未分类"
    wiki_title = video_path.stem
    summaries: list[str] = []
    evidence_units = _persist_trusted_source(
        root,
        video_id=video_id,
        video_fingerprint=video_fingerprint,
        video_relative_path=video_relative,
        source_title=wiki_title,
        domain=domain,
        verified_payload=verified_payload,
        trusted_segments=segments,
        raw_transcript_path=raw_transcript_path,
    )
    evidence_by_segment = {str(item["segment_id"]): item for item in evidence_units}
    for chunk_index, chunk in enumerate(_segment_chunks(segments), start=1):
        chunk_text = " ".join(str(item.get("text") or "") for item in chunk)
        existing_concepts = _existing_concept_context(root, chunk_text)
        result, _usage = client.complete_json(
            """你把经过可信校对的视频时间线编译成可持续增长、可追溯的知识。不得使用常识补充来源中没有的结论。
已有概念与当前内容含义相同时，必须复用已有概念的 canonical_name；分类不确定不影响知识生成。
每个 knowledge_point 必须引用当前输入中一个或多个 segment_ids。aliases 只填写同义名称，relations 只填写当前证据明确支持的概念关系。
只返回 JSON：
{"domain":"可为空的简洁领域","wiki_title":"文档标题","summary":"本段摘要","knowledge_points":[{"title":"规范概念或知识主题","aliases":["同义名称"],"content":"清晰的原子结论与必要解释","segment_ids":["0"],"relations":[{"type":"related_to","target":"相关概念"}]}]}。
不要返回 Markdown。""",
            {
                "video": video_path.name,
                "confirmed_domain": confirmed_domain or None,
                "chunk": chunk_index,
                "segments": chunk,
                "existing_concepts": existing_concepts,
            },
            max_tokens=5000,
        )
        if chunk_index == 1:
            if not confirmed_domain:
                domain = str(result.get("domain") or domain).strip() or domain
            wiki_title = str(result.get("wiki_title") or wiki_title).strip() or wiki_title
        summary = str(result.get("summary") or "").strip()
        if summary:
            summaries.append(summary)
        raw_units = result.get("knowledge_points")
        if not isinstance(raw_units, list):
            raise ValueError("模型未返回 knowledge_points 数组")
        chunk_ids = {item["id"] for item in chunk}
        for raw in raw_units:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            content = str(raw.get("content") or "").strip()
            evidence_ids = [str(item) for item in raw.get("segment_ids", [])]
            evidence_ids = list(dict.fromkeys(item for item in evidence_ids if item in chunk_ids and item in allowed))
            if title and content and evidence_ids:
                aliases = [str(item).strip() for item in raw.get("aliases", []) if str(item).strip()]
                relations = [
                    {
                        "type": str(item.get("type") or "related_to").strip() or "related_to",
                        "target": str(item.get("target") or "").strip(),
                    }
                    for item in raw.get("relations", [])
                    if isinstance(item, dict) and str(item.get("target") or "").strip()
                ]
                units.append(
                    {
                        "title": title,
                        "aliases": aliases,
                        "content": content,
                        "segment_ids": evidence_ids,
                        "relations": relations,
                    }
                )
    if not units:
        raise ValueError("模型没有生成带视频证据的知识点")

    indexed = _ensure_legacy_migrated(root, load_index(root))
    projection_was_current = not reconcile_space_metadata(root, indexed)["projection_stale"]
    previous_entries = [dict(item) for item in indexed]
    existing_for_video = [item for item in indexed if str(item.get("video_id")) == video_id]
    if existing_for_video:
        source_relative = _source_path_for_entry(existing_for_video[0])
    else:
        source_relative = f"{OBSIDIAN_FOLDER}/{_safe_component(domain)}/{_safe_component(wiki_title)}.md"
        occupied = {
            _source_path_for_entry(item).casefold()
            for item in indexed
            if str(item.get("video_id")) != video_id
        }
        if source_relative.casefold() in occupied:
            source_relative = (
                f"{OBSIDIAN_FOLDER}/{_safe_component(domain)}/"
                f"{_safe_component(wiki_title)}-{video_fingerprint[:8]}.md"
            )
    entries = [item for item in indexed if str(item.get("video_id")) != video_id]
    source_summary = "\n\n".join(summaries).strip()
    concepts = load_concepts(root)
    concept_lookup = _concept_lookup(concepts)
    relation_records = [item for item in load_relations(root) if str(item.get("source_id") or "") != video_id]
    for order, unit in enumerate(units, start=1):
        concept = _resolve_concept(
            concepts,
            unit["title"],
            unit.get("aliases", []),
            domain,
            video_id,
            concept_lookup,
        )
        canonical_title = str(concept.get("canonical_name") or unit["title"])
        evidence = [allowed[item] for item in unit["segment_ids"]]
        durable_evidence = [evidence_by_segment[item] for item in unit["segment_ids"] if item in evidence_by_segment]
        start = min(float(item["start"]) for item in evidence)
        end = max(float(item["end"]) for item in evidence)
        evidence_text = " ".join(str(item["text"]) for item in evidence)
        claim_id = _claim_id(str(concept["concept_id"]), unit["content"])
        knowledge_id = "knowledge-" + hashlib.sha256(
            f"{video_id}|{claim_id}".encode("utf-8")
        ).hexdigest()[:24]
        entry = {
                "schema_version": SCHEMA_VERSION,
                "knowledge_id": knowledge_id,
                "claim_id": claim_id,
                "concept_id": concept["concept_id"],
                "concept_name": canonical_title,
                "aliases": list(concept.get("aliases") or []),
                "domain": domain,
                "title": canonical_title,
                "content": unit["content"],
                "video_id": video_id,
                "video_fingerprint": video_fingerprint,
                "video_relative_path": video_relative,
                "source_title": wiki_title,
                "source_summary": source_summary,
                "source_obsidian_relative_path": source_relative,
                "knowledge_order": order,
                "evidence_start": round(start, 3),
                "evidence_end": round(end, 3),
                "evidence_text": evidence_text,
                "evidence_ids": [item["evidence_id"] for item in durable_evidence],
                "source_refs": [
                    {
                        "source_id": video_id,
                        "evidence_id": item["evidence_id"],
                        "segment_id": item["segment_id"],
                        "locator": item["locator"],
                    }
                    for item in durable_evidence
                ],
                "updated_at": iso_now(),
            }
        entries.append(entry)
        for relation in unit.get("relations", []):
            target = _resolve_concept(concepts, relation["target"], [], domain, video_id, concept_lookup)
            relation_type = re.sub(r"[^a-z0-9_:-]", "", str(relation.get("type") or "related_to").casefold()) or "related_to"
            relation_id = "relation-" + hashlib.sha256(
                f"{concept['concept_id']}|{relation_type}|{target['concept_id']}|{video_id}".encode("utf-8")
            ).hexdigest()[:24]
            relation_records.append(
                {
                    "schema_version": 1,
                    "relation_id": relation_id,
                    "from_concept_id": concept["concept_id"],
                    "type": relation_type,
                    "to_concept_id": target["concept_id"],
                    "source_id": video_id,
                    "evidence_ids": [item["evidence_id"] for item in durable_evidence],
                    "updated_at": iso_now(),
                }
            )
    write_concepts(root, concepts)
    claims_by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        claim_id = str(entry.get("claim_id") or _claim_id(str(entry.get("concept_id") or _concept_key(str(entry.get("title") or ""))), str(entry.get("content") or "")))
        claim = claims_by_id.setdefault(
            claim_id,
            {
                "schema_version": 1,
                "claim_id": claim_id,
                "concept_id": str(entry.get("concept_id") or ""),
                "statement": str(entry.get("content") or ""),
                "origin": "asserted",
                "status": "active",
                "source_ids": [],
                "evidence_ids": [],
            },
        )
        claim["source_ids"] = list(dict.fromkeys([*claim["source_ids"], str(entry.get("video_id") or "")]))
        claim["evidence_ids"] = list(dict.fromkeys([*claim["evidence_ids"], *list(entry.get("evidence_ids") or [])]))
        claim["updated_at"] = iso_now()
        entry["claim_id"] = claim_id
    _write_jsonl(paths["claims"], claims_by_id.values(), "claim_id")
    dedup_relations = {str(item["relation_id"]): item for item in relation_records if item.get("relation_id")}
    _write_jsonl(paths["relations"], dedup_relations.values(), "relation_id")
    entries = _render_obsidian_incremental(root, previous_entries, entries, video_id)
    write_index(root, entries)
    if projection_was_current:
        _mark_projection_current(root, entries)
    else:
        metadata = _load_metadata(root)
        metadata["obsidian_partial_update_at"] = iso_now()
        _write_metadata(root, metadata)
    _persist_trusted_source(
        root,
        video_id=video_id,
        video_fingerprint=video_fingerprint,
        video_relative_path=video_relative,
        source_title=wiki_title,
        domain=domain,
        verified_payload=verified_payload,
        trusted_segments=segments,
        raw_transcript_path=raw_transcript_path,
    )
    _append_wiki_log(root, wiki_title, domain, [dict(item, video_id=video_id) for item in units])
    obsidian_path = paths["root"] / Path(source_relative)
    return {
        "video_id": video_id,
        "domain": domain,
        "wiki_title": wiki_title,
        "wiki": str(obsidian_path),
        "knowledge_count": len(units),
    }


def _query_terms(value: str) -> set[str]:
    normalized = re.sub(r"\s+", "", value.casefold())
    terms = {item for item in re.findall(r"[a-z0-9_+-]{2,}|[\u3400-\u9fff]{2,}", normalized)}
    terms.update(normalized[index : index + 2] for index in range(max(0, len(normalized) - 1)))
    return {item for item in terms if item}


def _search_score(question: str, *, title: str, aliases: str, body: str, source: str = "") -> int:
    query_terms = _query_terms(question)
    if not query_terms:
        return 0
    title_terms = _query_terms(title)
    alias_terms = _query_terms(aliases)
    body_terms = _query_terms(body)
    source_terms = _query_terms(source)
    score = (
        len(query_terms & title_terms) * 12
        + len(query_terms & alias_terms) * 10
        + len(query_terms & source_terms) * 4
        + len(query_terms & body_terms) * 2
    )
    folded = question.casefold().strip()
    if folded and folded in title.casefold():
        score += 40
    if folded and folded in aliases.casefold():
        score += 30
    if folded and folded in body.casefold():
        score += 20
    return score


def search_index(root: Path, query: str, limit: int = 12) -> list[dict[str, Any]]:
    question = query.strip()
    if not question:
        raise ValueError("请输入要检索的知识")
    paths = space_paths(root)
    scored: list[tuple[int, dict[str, Any]]] = []
    entries = load_index(root)
    direct_concepts: set[str] = set()
    entry_scores: dict[str, int] = {}
    covered_evidence_ids: set[str] = set()
    for entry in entries:
        title = str(entry.get("title") or "")
        content = str(entry.get("content") or "")
        domain = str(entry.get("domain") or "")
        evidence = str(entry.get("evidence_text") or "")
        aliases = " ".join(str(item) for item in entry.get("aliases", []) if str(item).strip())
        score = _search_score(
            question,
            title=title,
            aliases=aliases,
            body=f"{content} {evidence} {domain}",
            source=str(entry.get("source_title") or ""),
        )
        if score <= 0:
            continue
        entry_scores[str(entry.get("knowledge_id") or "")] = score
        concept_id = str(entry.get("concept_id") or "")
        if concept_id:
            direct_concepts.add(concept_id)
        covered_evidence_ids.update(str(item) for item in entry.get("evidence_ids", []) if str(item))

    if direct_concepts:
        related: set[str] = set()
        for relation in load_relations(root):
            source_id = str(relation.get("from_concept_id") or "")
            target_id = str(relation.get("to_concept_id") or "")
            if source_id in direct_concepts and target_id:
                related.add(target_id)
            if target_id in direct_concepts and source_id:
                related.add(source_id)
        for entry in entries:
            knowledge_id = str(entry.get("knowledge_id") or "")
            if str(entry.get("concept_id") or "") in related and knowledge_id not in entry_scores:
                entry_scores[knowledge_id] = 3

    for entry in entries:
        score = entry_scores.get(str(entry.get("knowledge_id") or ""), 0)
        if score <= 0:
            continue
        item = dict(entry)
        item["score"] = score
        item["record_type"] = "knowledge"
        item["video_path"] = str((paths["root"] / str(item.get("video_relative_path", ""))).resolve())
        item["video_available"] = Path(item["video_path"]).is_file()
        item["obsidian_path"] = str((paths["root"] / str(item.get("obsidian_relative_path", ""))).resolve())
        scored.append((score, item))

    for evidence in load_evidence_units(root):
        evidence_id = str(evidence.get("evidence_id") or "")
        if not evidence_id or evidence_id in covered_evidence_ids:
            continue
        text = str(evidence.get("text") or "")
        title = str(evidence.get("source_title") or "可信视频原文")
        score = _search_score(
            question,
            title=title,
            aliases="",
            body=f"{text} {evidence.get('domain') or ''}",
            source=title,
        )
        if score <= 0:
            continue
        locator = evidence.get("locator") if isinstance(evidence.get("locator"), dict) else {}
        video_relative = str(evidence.get("video_relative_path") or "")
        video_path = str((paths["root"] / video_relative).resolve())
        item = {
            "schema_version": 1,
            "knowledge_id": evidence_id,
            "evidence_id": evidence_id,
            "record_type": "trusted_evidence",
            "title": title,
            "content": text,
            "domain": str(evidence.get("domain") or "未分类"),
            "video_id": str(evidence.get("source_id") or ""),
            "video_relative_path": video_relative,
            "video_path": video_path,
            "video_available": Path(video_path).is_file(),
            "evidence_start": float(locator.get("start") or 0),
            "evidence_end": float(locator.get("end") or locator.get("start") or 0),
            "evidence_text": text,
            "obsidian_relative_path": "",
            "obsidian_path": "",
            "score": score,
        }
        scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("title", ""))))
    return [item for _score, item in scored[: max(1, min(int(limit), 50))]]


def _bounded_conversation_context(conversation: Iterable[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Keep only a small, plain-text context window for the current chat session."""
    normalized: list[dict[str, str]] = []
    for item in conversation or []:
        if not isinstance(item, dict) or item.get("error"):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        normalized.append({"role": role, "content": content[:1200]})
    return normalized[-6:]


def _contextual_search_query(question: str, conversation: list[dict[str, str]]) -> str:
    previous_questions = [item["content"][:500] for item in conversation if item["role"] == "user"][-3:]
    return "\n".join([*previous_questions, question]).strip()


def answer_question(
    root: Path,
    question: str,
    client: Any,
    limit: int = 8,
    conversation: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    context = _bounded_conversation_context(conversation)
    hits = search_index(root, _contextual_search_query(question, context), limit=limit)
    if not hits:
        return {"answer": "当前知识空间没有找到能够支撑这个问题的知识证据。", "citations": []}
    evidence_payload = [
        {
            "knowledge_id": item["knowledge_id"],
            "title": item["title"],
            "content": item["content"],
            "evidence_text": item["evidence_text"],
            "video": Path(item["video_path"]).name,
            "start": item["evidence_start"],
            "end": item["evidence_end"],
        }
        for item in hits
    ]
    result, _usage = client.complete_json(
        """你只能依据给定的本地视频知识证据回答问题。不得用模型常识补充证据中没有的事实。
conversation_context 只用于理解当前问题中的指代和承接关系，不能作为事实证据。
回答要直接、清晰，并选择真正支撑回答的 knowledge_ids。只返回 JSON：
{"answer":"回答正文","knowledge_ids":["knowledge-..."]}。没有足够证据时明确说明。""",
        {"conversation_context": context, "question": question, "evidence": evidence_payload},
        max_tokens=3000,
    )
    answer = str(result.get("answer") or "").strip()
    requested_ids = [str(item) for item in result.get("knowledge_ids", [])]
    allowed = {str(item["knowledge_id"]): item for item in hits}
    citations = [allowed[item] for item in dict.fromkeys(requested_ids) if item in allowed]
    if not answer:
        raise ValueError("模型没有返回回答")
    if not citations:
        return {"answer": "当前证据不足，无法给出可核对的回答。", "citations": []}
    return {"answer": answer, "citations": citations}


def relink_video(root: Path, video_id: str, source: Path) -> dict[str, Any]:
    entries = load_index(root)
    related = [item for item in entries if str(item.get("video_id")) == video_id]
    if not related:
        raise KeyError("知识索引中没有这个视频")
    expected = str(related[0].get("video_fingerprint") or "")
    source_path = source.expanduser().resolve()
    if fingerprint(source_path) != expected:
        raise ValueError("所选文件不是原视频，文件指纹不一致")
    copied = copy_video(root, source_path)
    for item in entries:
        if str(item.get("video_id")) == video_id:
            item["video_relative_path"] = copied["relative_path"]
            item["updated_at"] = iso_now()
    write_index(root, entries)
    return copied


def create_task(root: Path, sources: Iterable[str | Path]) -> dict[str, Any]:
    initialize_space(root)
    videos = discover_videos(sources)
    if not videos:
        raise ValueError("没有发现支持的视频或音频")
    task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    task_dir = space_paths(root)["work"] / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=False)
    now = iso_now()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "status": "waiting",
        "created_at": now,
        "updated_at": now,
        "videos": [
            {
                **item,
                "status": "waiting",
                "stage": "copy",
                "progress": 0.0,
                "message": "等待开始",
            }
            for item in videos
        ],
    }
    write_task(root, payload)
    return payload


def task_path(root: Path, task_id: str) -> Path:
    if not re.fullmatch(r"task-[A-Za-z0-9-]+", task_id):
        raise ValueError("任务 ID 无效")
    return space_paths(root)["work"] / "tasks" / task_id / "task.json"


def write_task(root: Path, payload: dict[str, Any]) -> Path:
    target = task_path(root, str(payload.get("task_id") or ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    value = dict(payload)
    value["updated_at"] = iso_now()
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def load_task(root: Path, task_id: str) -> dict[str, Any]:
    target = task_path(root, task_id)
    return json.loads(target.read_text(encoding="utf-8-sig"))


def load_latest_resumable_task(root: Path) -> dict[str, Any] | None:
    tasks_root = space_paths(root)["work"] / "tasks"
    if not tasks_root.is_dir():
        return None
    candidates = sorted(tasks_root.glob("task-*/task.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("status") not in {
            "running", "interrupted", "cancelled", "failed", "needs_attention"
        }:
            continue
        if payload.get("status") == "running":
            payload["status"] = "interrupted"
            payload["status_text"] = "上次任务未正常结束，可以从已保存的阶段继续"
            for stage in payload.get("stages", []):
                if isinstance(stage, dict) and stage.get("status") == "running":
                    stage["status"] = "waiting"
                    stage["message"] = "等待从断点继续"
            for video in payload.get("videos", []):
                if isinstance(video, dict) and video.get("status") == "processing":
                    video["status"] = "interrupted"
            write_task(root, payload)
        return payload
    return None


def clean_task_work(root: Path, task_id: str) -> bool:
    paths = space_paths(root)
    target = task_path(root, task_id).parent.resolve()
    tasks_root = (paths["work"] / "tasks").resolve()
    if target.parent != tasks_root or target == paths["root"] or not target.name.startswith("task-"):
        raise ValueError("拒绝清理不安全的目录")
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
