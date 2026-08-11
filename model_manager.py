from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from app_config import (
    MODEL_REPOSITORIES,
    SUPPORTED_MODELS,
    installed_models,
    load_config,
    model_paths,
    save_config,
)


MINIMUM_FREE_BYTES = 2 * 1024**3


def install_model(model_name: str) -> Path:
    if model_name not in SUPPORTED_MODELS:
        raise ValueError(f"不支持的模型：{model_name}")

    config = load_config()
    destination = model_paths(config)[model_name]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (destination / "model.bin").is_file() and (destination / "config.json").is_file():
        print(f"模型已存在：{destination}")
    else:
        free_bytes = shutil.disk_usage(destination.parent).free
        if free_bytes < MINIMUM_FREE_BYTES:
            raise RuntimeError("模型目录可用空间不足 2GB，请释放空间后重试。")
        print(f"正在下载 {model_name}，首次安装约需 1.5GB，请保持网络连接…")
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=MODEL_REPOSITORIES[model_name],
            local_dir=destination,
            cache_dir=str(config["hf_cache_dir"]),
            allow_patterns=[
                "config.json",
                "preprocessor_config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.*",
            ],
        )

    config["default_model"] = model_name
    save_config(config)
    print(f"默认模型：{model_name}")
    print(f"模型目录：{destination}")
    return destination


def show_status() -> None:
    config = load_config()
    available = installed_models(config)
    print(f"默认模型：{config['default_model']}")
    print(f"模型目录：{config['model_root']}")
    print("已安装模型：" + (", ".join(available) if available else "无"))


def main() -> int:
    parser = argparse.ArgumentParser(description="LocalTranscriber 本地模型管理")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install", help="下载模型并设为默认模型")
    install_parser.add_argument("--model", choices=SUPPORTED_MODELS, required=True)
    subparsers.add_parser("status", help="查看模型安装状态")
    args = parser.parse_args()

    if args.command == "install":
        install_model(args.model)
    else:
        show_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
