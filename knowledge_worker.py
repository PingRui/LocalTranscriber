from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app_config import SUPPORTED_MODELS
from llm_client import OpenAICompatibleClient
from task_hotwords import TaskHotwordDiscovery
from transcribe import DEFAULT_MODEL, load_model, prepare_task_hotwords


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LocalTranscriber 视频知识任务辅助进程")
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="预转录样本并生成待确认专业词汇")
    analyze.add_argument("video", type=Path)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--model", choices=SUPPORTED_MODELS, default=DEFAULT_MODEL)
    analyze.add_argument("--language", default="auto")
    analyze.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    analyze.add_argument("--api-base-url", required=True)
    analyze.add_argument("--api-model", required=True)
    analyze.add_argument("--sample-seconds", type=float, default=180.0)
    analyze.add_argument("--known-domains", type=Path)
    analyze.add_argument("--gui-events", action="store_true")
    return parser.parse_args()


def analyze(args: argparse.Namespace) -> int:
    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    api_key = os.environ.get("KNOWLEDGE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("没有提供 OpenAI 兼容接口 API Key")
    client = OpenAICompatibleClient(
        base_url=args.api_base_url,
        model=args.api_model,
        api_key=api_key,
        allow_remote=True,
    )
    known_domains: list[dict] = []
    if args.known_domains and args.known_domains.is_file():
        try:
            payload = json.loads(args.known_domains.read_text(encoding="utf-8-sig"))
            if isinstance(payload, list):
                known_domains = [item for item in payload if isinstance(item, dict)]
        except (OSError, UnicodeError, json.JSONDecodeError):
            known_domains = []
    model, _device, _compute_type = load_model(args.model, args.device, args.gui_events)
    discovery = TaskHotwordDiscovery(
        client,
        args.output.expanduser().resolve(),
        discovery_seconds=args.sample_seconds,
        min_chars=80,
        known_domains=known_domains,
    )
    language = None if str(args.language).lower() == "auto" else str(args.language)
    prepare_task_hotwords([video], model, language, discovery, args.gui_events)
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "analyze":
        return analyze(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
