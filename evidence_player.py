from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import webview


def build_html(video: Path, *, start: float, end: float, title: str) -> str:
    uri = video.resolve().as_uri()
    safe_title = html.escape(title or video.name)
    safe_filename = html.escape(video.name)
    start_value = max(0.0, float(start))
    end_value = max(start_value, float(end))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{safe_title}</title>
  <style>
    html, body {{ margin: 0; height: 100%; background: #111827; color: #f8fafc; font-family: system-ui, sans-serif; }}
    body {{ display: grid; grid-template-rows: auto 1fr; }}
    header {{ padding: 14px 18px; background: #0f172a; border-bottom: 1px solid #334155; }}
    strong, span {{ display: block; }}
    span {{ margin-top: 4px; color: #94a3b8; font-size: 13px; }}
    main {{ min-height: 0; display: grid; place-items: center; padding: 12px; }}
    video {{ width: 100%; height: 100%; max-height: calc(100vh - 84px); background: #000; }}
  </style>
</head>
<body>
  <header><strong>{safe_title}</strong><span>{safe_filename} · {start_value:.1f}s — {end_value:.1f}s</span></header>
  <main><video id="player" controls autoplay preload="metadata"></video></main>
  <script>
    const player = document.getElementById('player');
    player.src = {json.dumps(uri)};
    player.addEventListener('loadedmetadata', () => {{
      player.currentTime = {start_value};
      player.play().catch(() => {{}});
    }}, {{ once: true }});
  </script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="打开 LocalTranscriber 视频证据")
    parser.add_argument("--file", default="")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--end", type=float, default=0)
    parser.add_argument("--title", default="视频证据")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    video = (Path(args.file).expanduser() if args.file else Path(__file__)).resolve()
    if not args.file and not args.smoke_test:
        parser.error("--file is required unless --smoke-test is used")
    if not video.is_file():
        raise FileNotFoundError(video)
    page = build_html(video, start=args.start, end=args.end, title=args.title)
    if args.smoke_test:
        if video.as_uri() not in page or "currentTime" not in page:
            raise RuntimeError("播放器页面生成失败")
        print("EVIDENCE_PLAYER_SMOKE_OK")
        return
    webview.create_window(
        "LocalTranscriber 视频证据",
        html=page,
        width=960,
        height=650,
        min_size=(720, 480),
        background_color="#111827",
    )
    webview.start(debug=False, private_mode=True)


if __name__ == "__main__":
    main()
