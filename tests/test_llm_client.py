from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.request import Request

from llm_client import OpenAICompatibleClient, normalize_local_base_url, normalize_openai_base_url, parse_json_object


class LlmClientTests(unittest.TestCase):
    def test_local_endpoint_only_accepts_loopback(self) -> None:
        self.assertEqual(normalize_local_base_url("http://127.0.0.1:1234/v1/"), "http://127.0.0.1:1234/v1")
        self.assertEqual(normalize_local_base_url("http://localhost:1234/v1"), "http://localhost:1234/v1")
        with self.assertRaises(ValueError):
            normalize_local_base_url("https://example.com/v1")

    def test_qwen_json_parser_ignores_thinking_marker_and_fence(self) -> None:
        self.assertEqual(parse_json_object('</think>\n```json\n{"answer":"ok"}\n```'), {"answer": "ok"})

    def test_remote_compatible_endpoint_requires_https(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://api.deepseek.com/v1/"),
            "https://api.deepseek.com/v1",
        )
        self.assertEqual(
            normalize_openai_base_url("https://relay.example.com/openai/v1"),
            "https://relay.example.com/openai/v1",
        )
        with self.assertRaises(ValueError):
            normalize_openai_base_url("http://relay.example.com/v1")
        with self.assertRaises(ValueError):
            normalize_openai_base_url("https://relay.example.com/v1/chat/completions")

    def test_remote_compatible_client_uses_standard_bearer_header(self) -> None:
        client = OpenAICompatibleClient(
            base_url="https://relay.example.com/v1",
            model="vendor-model-name",
            api_key="plain-secret",
            allow_remote=True,
        )
        self.assertEqual(client.request_headers()["Authorization"], "Bearer plain-secret")
        self.assertEqual(client.model, "vendor-model-name")

    def test_connection_uses_real_chat_completions_path(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read():
                return b'{"choices":[{"message":{"content":"{\\\"review_complete\\\":true,\\\"corrections\\\":[]}"}}],"usage":{"total_tokens":8}}'

        captured: list[Request] = []

        def fake_urlopen(request, timeout):
            captured.append(request)
            self.assertEqual(timeout, 180.0)
            return FakeResponse()

        client = OpenAICompatibleClient(
            base_url="https://relay.example.com/openai/v1",
            model="any-compatible-model",
            api_key="plain-secret",
            allow_remote=True,
        )
        with patch("llm_client.urlopen", side_effect=fake_urlopen):
            usage = client.test_chat_completion()
        self.assertEqual(usage["total_tokens"], 8)
        self.assertEqual(captured[0].full_url, "https://relay.example.com/openai/v1/chat/completions")
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer plain-secret")
        body = __import__("json").loads(captured[0].data.decode("utf-8"))
        self.assertEqual(body["max_tokens"], 1024)
        self.assertIn("whole_file_review", body["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
