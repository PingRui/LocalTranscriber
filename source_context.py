from __future__ import annotations

import argparse
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LocalTranscriber/1.0"
MAX_PAGE_BYTES = 5 * 1024 * 1024
TERM_LIMIT = 48
CONTEXT_SCHEMA_VERSION = 2


def _download_text(url: str, timeout: float = 15.0) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
    with urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_PAGE_BYTES + 1)
        if len(data) > MAX_PAGE_BYTES:
            raise ValueError("来源页面过大，已停止读取")
        charset = response.headers.get_content_charset() or "utf-8"
    return data.decode(charset, errors="replace")


def _youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/", 1)[0] or None
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        match = re.match(r"/(?:shorts|embed|live)/([^/?#]+)", parsed.path)
        return match.group(1) if match else None
    return None


def normalize_source_url(url: str) -> tuple[str, str]:
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("来源网址必须是有效的 http 或 https 地址")
    video_id = _youtube_video_id(value)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}", "youtube"
    clean = parsed._replace(fragment="")
    return urlunparse(clean), parsed.netloc.lower()


def _json_string(page: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}":"((?:\\.|[^"\\])*)"', page)
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return ""


def _meta_content(page: str, name: str) -> str:
    patterns = (
        rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, page, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def extract_terms(*values: str) -> list[str]:
    text = "\n".join(value for value in values if value)
    matches: list[tuple[int, str]] = []
    patterns = (
        r"\b[A-Z][A-Z0-9]*(?:[-/][A-Z0-9]+)+\b",
        r"\b[A-Z][A-Z0-9]{1,}\b",
        r"\b[A-Z][A-Za-z’'.+-]*(?:[ \t]+[A-Z][A-Za-z0-9’'.+-]*){0,3}\b",
    )
    for pattern in patterns:
        matches.extend((match.start(), match.group(0).strip(" .,:;()[]")) for match in re.finditer(pattern, text))
    candidates = [candidate for _start, candidate in sorted(matches, key=lambda item: item[0])]

    rejected = {
        "a", "an", "and", "at", "before", "but", "chapter", "chapters", "check", "disclaimer",
        "before", "brought", "command", "engineers", "episode", "follow", "for", "from", "get", "head", "here", "how", "i", "if", "in", "interpretability", "learning", "linkedin", "links", "listen",
        "manager", "more", "note", "of", "on", "or", "our", "product", "radically", "research", "resources", "subscribe", "the", "x",
        "she", "this", "to", "use", "visit", "watch", "we", "what", "when", "where", "why", "with", "you",
    }
    rejected_starts = {"before", "brought", "check", "follow", "how", "in", "make", "the", "this", "what", "where", "why"}
    seen: set[str] = set()
    terms: list[str] = []
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip()
        candidate = re.sub(r"[’']s$", "", candidate)
        key = candidate.casefold()
        first_word = key.split(" ", 1)[0].rstrip("’'s")
        if not candidate or key in rejected or first_word in rejected_starts or key in seen or len(candidate) > 64:
            continue
        if re.fullmatch(r"B0[A-Z0-9]{7,}", candidate):
            continue
        if len(candidate.split()) > 2 and re.search(r"\b(?:To|Of|The)\b", candidate):
            continue
        if len(candidate) == 1 and not candidate.isupper():
            continue
        seen.add(key)
        terms.append(candidate)
        if len(terms) >= TERM_LIMIT:
            break
    return terms


def _fetch_youtube(url: str) -> dict[str, str]:
    oembed_url = "https://www.youtube.com/oembed?" + urlencode({"url": url, "format": "json"})
    oembed = json.loads(_download_text(oembed_url))
    page = _download_text(url)
    return {
        "title": str(oembed.get("title") or _meta_content(page, "og:title")),
        "author": str(oembed.get("author_name") or _json_string(page, "ownerChannelName")),
        "description": _json_string(page, "shortDescription") or _meta_content(page, "og:description"),
    }


def _fetch_generic(url: str) -> dict[str, str]:
    page = _download_text(url)
    title = _meta_content(page, "og:title")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.IGNORECASE | re.DOTALL)
        title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip() if match else ""
    return {
        "title": title,
        "author": _meta_content(page, "author"),
        "description": _meta_content(page, "og:description") or _meta_content(page, "description"),
    }


def load_source_context(url: str, cache_path: Path, refresh: bool = False) -> dict[str, object]:
    normalized_url, platform = normalize_source_url(url)
    if cache_path.is_file() and not refresh:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("source_url") == normalized_url
                and cached.get("schema_version") == CONTEXT_SCHEMA_VERSION
                and not cached.get("error")
            ):
                cached["cache_hit"] = True
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    try:
        details = _fetch_youtube(normalized_url) if platform == "youtube" else _fetch_generic(normalized_url)
        description = details["description"]
        intro = description.split("\n\n*In our", 1)[0]
        sponsors = ""
        sponsor_match = re.search(r"\*Brought to you by:\*(.*?)(?:\n\n|\Z)", description, flags=re.DOTALL)
        if sponsor_match:
            sponsors = sponsor_match.group(1)
        references = ""
        reference_match = re.search(r"\*Referenced:\*(.*?)(?:\n\n\*Recommended books|\Z)", description, flags=re.DOTALL)
        if reference_match:
            references = reference_match.group(1)
        identity_lines = "\n".join(
            line for line in description.splitlines() if "Where to find " in line or "LinkedIn:" in line
        )
        people = "\n".join(
            match.group(1)
            for pattern in (
                r"•\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})(?:[’']s|\s+on\s+X|\s*\()",
                r"\|\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})(?:\s*\(|\s*:)",
            )
            for match in re.finditer(pattern, references)
        )
        primary_people = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b", details["title"])
        people_names = list(primary_people)
        people_names.extend(line for line in people.splitlines() if line)
        people_names = list(dict.fromkeys(people_names))
        cleaned_parts = [re.sub(r"https?://\S+", "", part) for part in (intro, identity_lines, people, references, sponsors)]
        priority_terms = extract_terms(details["title"], details["author"], *cleaned_parts[:3])
        remaining_terms = extract_terms(*cleaned_parts[3:])
        terms = []
        seen_terms: set[str] = set()
        for term in (*priority_terms, *remaining_terms):
            key = term.casefold()
            if key not in seen_terms:
                seen_terms.add(key)
                terms.append(term)
            if len(terms) >= 36:
                break
        prompt_parts = [
            f"节目标题：{details['title']}" if details["title"] else "",
            f"说话者或频道：{details['author']}" if details["author"] else "",
        ]
        result: dict[str, object] = {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "source_url": normalized_url,
            "platform": platform,
            **details,
            "terms": terms,
            "people": people_names,
            "primary_people": primary_people,
            "initial_prompt": "\n".join(part for part in prompt_parts if part)[:1800],
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "cache_hit": False,
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    except Exception as exc:
        return {
            "source_url": normalized_url,
            "platform": platform,
            "title": "",
            "author": "",
            "description": "",
            "terms": [],
            "people": [],
            "primary_people": [],
            "initial_prompt": "",
            "error": f"{type(exc).__name__}: {exc}",
            "cache_hit": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="提取视频来源页面的转写上下文")
    parser.add_argument("url")
    parser.add_argument("--cache", default="source-context.json")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    result = load_source_context(args.url, Path(args.cache).expanduser().resolve(), args.refresh)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
