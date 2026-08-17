from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_LOCAL_MODEL = "qwen3-4b-proofreader"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def normalize_local_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("本地模型地址只允许 http://127.0.0.1、http://localhost 或 http://[::1]")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("本地模型地址格式无效")
    return base_url


def normalize_openai_base_url(value: str) -> str:
    """Validate an OpenAI-compatible API base URL without tying it to a vendor."""
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("API 地址必须是完整的 http:// 或 https:// 地址")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("远程 API 必须使用 HTTPS；HTTP 只允许本机地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API 地址不能包含账号、密码、查询参数或锚点")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("请填写 API 基础地址，例如 https://example.com/v1，不要包含 /chat/completions")
    return base_url


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    text = re.sub(r"^<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"^</think>\s*", "", text)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    value, end = json.JSONDecoder().raw_decode(text)
    if text[end:].strip() not in {"", '"'}:
        raise ValueError("模型 JSON 后存在额外内容")
    if not isinstance(value, dict):
        raise ValueError("模型结果不是 JSON 对象")
    return value


@dataclass
class OpenAICompatibleClient:
    base_url: str = DEFAULT_LOCAL_BASE_URL
    model: str = DEFAULT_LOCAL_MODEL
    api_key: str = ""
    allow_remote: bool = False
    timeout: float = 180.0

    def __post_init__(self) -> None:
        self.base_url = (
            normalize_openai_base_url(self.base_url)
            if self.allow_remote
            else normalize_local_base_url(self.base_url)
        )
        self.api_key = self.api_key.strip()
        if not self.model.strip():
            raise ValueError("模型名称不能为空")

    def request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def models(self) -> list[str]:
        request = Request(f"{self.base_url}/models", headers=self.request_headers())
        try:
            with urlopen(request, timeout=min(self.timeout, 10.0)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"无法连接 OpenAI 兼容模型服务：{self.base_url}"
            ) from exc
        values = payload.get("data", []) if isinstance(payload, dict) else []
        return [str(item.get("id")) for item in values if isinstance(item, dict) and item.get("id")]

    def ensure_model(self) -> None:
        available = self.models()
        if self.model not in available:
            raise RuntimeError(f"模型不可用：{self.model}；接口返回的模型：{available or '无'}")

    def test_chat_completion(self) -> dict[str, Any]:
        """Exercise a representative structured request, not just a tiny ping."""
        payload, usage = self.complete_json(
            """你正在进行转录校对接口能力测试。检查所有测试片段，只返回 JSON：
{"review_complete":true,"corrections":[]}。不要返回 Markdown 或额外解释。""",
            {
                "test": "whole_file_review",
                "category": "营养健康",
                "hotwords": ["胰岛素", "葡萄糖"],
                "segments": [
                    {"id": "0", "start": 0, "end": 5, "text": "胰岛素参与血糖调节。"},
                    {"id": "1", "start": 5, "end": 10, "text": "这是接口结构化输出测试。"},
                ],
            },
            max_tokens=1024,
        )
        if payload.get("review_complete") is not True or not isinstance(payload.get("corrections"), list):
            raise RuntimeError("接口已响应，但模型未完成整文件校对格式测试")
        return usage

    def complete_json(
        self,
        system_prompt: str,
        user_payload: object,
        max_tokens: int = 4096,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        user_content = user_payload if isinstance(user_payload, str) else json.dumps(user_payload, ensure_ascii=False)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "stream": False,
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self.request_headers(),
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                content = str(payload["choices"][0]["message"].get("content") or "")
                return parse_json_object(content), dict(payload.get("usage") or {})
            except HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", errors="replace")
                if exc.code in {401, 403}:
                    last_error = RuntimeError("API Key 无效、缺失或没有访问权限")
                else:
                    last_error = RuntimeError(f"模型接口请求失败（HTTP {exc.code}）：{detail}")
            except (URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
        raise RuntimeError(f"模型接口连续三次未返回有效 JSON：{last_error}")
