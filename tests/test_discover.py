"""模型检测按连接（key）执行：同一供应商不同 Agent 可以拉出不同模型。"""
import unittest
from unittest.mock import patch

from discover import discover_models


class DiscoverTests(unittest.TestCase):
    def test_default_connection_uses_openai_style(self):
        provider = {"baseUrl": "https://plan.example/v1", "apiKey": "test-key"}
        with patch("discover._http_get_json", return_value={"data": [{"id": "plan-model"}]}) as get:
            models = discover_models(provider)
        self.assertEqual([m["id"] for m in models], ["plan-model"])
        get.assert_called_once_with("https://plan.example/v1/models",
                                    {"Authorization": "Bearer test-key"})

    def test_claude_connection_uses_anthropic_style(self):
        provider = {"baseUrl": "https://plan.example/v1", "apiKey": "openai-key",
                    "api": "openai-completions",
                    "agents": {"claude": {"baseUrl": "https://plan.example/v1",
                                          "api": "anthropic-messages",
                                          "apiKey": "anthropic-key"}}}
        with patch("discover._http_get_json", return_value={"data": [{"id": "claude-model"}]}) as get:
            models = discover_models(provider, "claude")
        self.assertEqual([m["id"] for m in models], ["claude-model"])
        get.assert_called_once_with("https://plan.example/v1/models",
                                    {"x-api-key": "anthropic-key", "anthropic-version": "2023-06-01"})

    def test_same_provider_different_key_returns_different_models(self):
        provider = {"baseUrl": "https://plan.example/v1", "apiKey": "openai-key",
                    "api": "openai-completions",
                    "agents": {"claude": {"baseUrl": "https://plan.example/v1",
                                          "api": "anthropic-messages",
                                          "apiKey": "anthropic-key"},
                               "codex": {"baseUrl": "https://plan.example/v1",
                                         "api": "openai-responses",
                                         "apiKey": "codex-key"}}}

        def fake_http(url, headers, timeout=15.0):
            if "x-api-key" in headers:
                return {"data": [{"id": "claude-opus-5-5"}]}
            return {"data": [{"id": "gpt-6-astra"}, {"id": "gpt-6-sol"}]}

        with patch("discover._http_get_json", side_effect=fake_http):
            claude = discover_models(provider, "claude")
            codex = discover_models(provider, "codex")
            default = discover_models(provider)
        self.assertEqual([m["id"] for m in claude], ["claude-opus-5-5"])
        self.assertEqual([m["id"] for m in codex], ["gpt-6-astra", "gpt-6-sol"])
        self.assertEqual([m["id"] for m in default], ["gpt-6-astra", "gpt-6-sol"])

    def test_anthropic_only_endpoint_with_v1(self):
        provider = {"baseUrl": "", "anthropicBaseUrl": "https://plan.example/v1", "apiKey": "test-key"}
        with patch("discover._http_get_json", return_value={"data": [{"id": "plan-model"}]}) as get:
            models = discover_models(provider)
        self.assertEqual([m["id"] for m in models], ["plan-model"])
        get.assert_called_once_with("https://plan.example/v1/models",
                                    {"x-api-key": "test-key", "anthropic-version": "2023-06-01"})

    def test_anthropic_only_endpoint_without_v1(self):
        provider = {"anthropicBaseUrl": "https://plan.example/anthropic", "apiKey": "test-key"}
        with patch("discover._http_get_json", return_value={"models": ["plan-model"]}) as get:
            models = discover_models(provider)
        self.assertEqual([m["id"] for m in models], ["plan-model"])
        self.assertEqual(get.call_args.args[0], "https://plan.example/anthropic/v1/models")

    def test_missing_key_is_reported(self):
        provider = {"baseUrl": "https://plan.example/v1", "apiKey": ""}
        with self.assertRaisesRegex(ValueError, "apiKey"):
            discover_models(provider)


if __name__ == "__main__":
    unittest.main()
