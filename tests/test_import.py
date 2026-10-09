"""反向同步：从各客户端原生配置读取供应商（导入候选）并写入 Hub。"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from support import isolated, engine, server


class ImportScanTests(unittest.TestCase):
    def setUp(self):
        context = isolated()
        self.home = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def config(self) -> dict:
        return json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))

    def test_unknown_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不支持从"):
            engine.Hub().import_candidates("mcode")

    def test_pi_reader_finds_user_added_gateway_only(self):
        models = self.home / ".pi" / "agent" / "models.json"
        models.parent.mkdir(parents=True)
        models.write_text(json.dumps({"providers": {
            # Hub 已管理（fixture 里的 deepseek）：按名称跳过
            "deepseek": {"name": "DeepSeek", "baseUrl": "https://api.deepseek.com",
                         "api": "openai-completions", "apiKey": "test-not-a-real-key", "models": []},
            "user-gateway": {"name": "User Gateway", "baseUrl": "https://user.example/v1",
                             "api": "openai-responses", "apiKey": "user-key",
                             "models": [{"id": "u-1", "name": "U1", "contextWindow": 200000,
                                         "input": ["text", "image"], "reasoning": True}]},
        }}), encoding="utf-8")
        candidates = engine.Hub().import_candidates("pi")
        self.assertEqual([c["id"] for c in candidates], ["user-gateway"])
        only = candidates[0]
        self.assertEqual((only["api"], only["apiKey"]), ("openai-responses", "user-key"))
        self.assertEqual([m["id"] for m in only["models"]], ["u-1"])
        self.assertEqual(only["models"][0]["contextWindow"], 200000)

    def test_dsh_reader_resolves_credentials_and_keeps_env_reference(self):
        dsh = self.home / ".dsh"
        dsh.mkdir(parents=True)
        (dsh / "settings.yaml").write_text(
            "llm-pi-ai:\n  providers:\n"
            "    deepseek:\n      displayName: DeepSeek\n      baseURL: https://api.deepseek.com\n"
            "      apiKeyEnv: MCODE_DEEPSEEK_API_KEY\n      api: openai-completions\n"
            "      models:\n        - id: deepseek/model-1\n"
            "    my-vendor:\n      displayName: My Vendor\n      baseURL: https://vendor.example/v1\n"
            "      apiKeyEnv: MY_VENDOR_API_KEY\n      api: openai-completions\n"
            "      models:\n        - id: v-1\n          contextWindow: 256000\n"
            "    no-cred:\n      displayName: No Cred\n      baseURL: https://nocred.example/v1\n"
            "      apiKeyEnv: MISSING_KEY\n      api: openai-completions\n", encoding="utf-8")
        (dsh / ".credentials.yaml").write_text(
            "version: 1\nrefs:\n  MY_VENDOR_API_KEY: vendor-secret\n", encoding="utf-8")
        candidates = engine.Hub().import_candidates("dsh")
        self.assertEqual([c["id"] for c in candidates], ["my-vendor", "no-cred"])
        by_id = {c["id"]: c for c in candidates}
        self.assertEqual(by_id["my-vendor"]["apiKey"], "vendor-secret")
        self.assertEqual(by_id["my-vendor"]["apiKeyEnv"], "")
        self.assertEqual([m["id"] for m in by_id["my-vendor"]["models"]], ["v-1"])
        # 凭据缺失时保留 apiKeyEnv 引用，导入后补齐密钥即可使用
        self.assertEqual(by_id["no-cred"]["apiKey"], "")
        self.assertEqual(by_id["no-cred"]["apiKeyEnv"], "MISSING_KEY")

    def test_codex_reader_skips_managed_blocks(self):
        codex = self.home / ".codex"
        codex.mkdir(parents=True)
        (codex / "config.toml").write_text(
            'model = "gpt-x"\nmodel_provider = "openai"\n\n'
            "[model_providers.agent_hub]\nname = \"Hub\"\nbase_url = \"https://hub.example/v1\"\n"
            "wire_api = \"responses\"\nexperimental_bearer_token = \"hub-key\"\n\n"
            "[model_providers.mirror]\nname = \"Mirror Gateway\"\nbase_url = \"https://mirror.example/v1\"\n"
            "wire_api = \"responses\"\nexperimental_bearer_token = \"mirror-key\"\n\n"
            "[model_providers.chat-only]\nname = \"Chat Only\"\nbase_url = \"https://chat.example/v1\"\n"
            "env_key = \"MIRROR_TOKEN\"\n", encoding="utf-8")
        candidates = engine.Hub().import_candidates("codex")
        self.assertEqual([c["id"] for c in candidates], ["mirror-gateway", "chat-only"])
        by_id = {c["id"]: c for c in candidates}
        self.assertEqual(by_id["mirror-gateway"]["api"], "openai-responses")
        self.assertEqual(by_id["mirror-gateway"]["apiKey"], "mirror-key")
        with patch.dict(os.environ, {"MIRROR_TOKEN": "env-resolved"}):
            resolved = engine.Hub().import_candidates("codex")
        env_entry = next(c for c in resolved if c["id"] == "chat-only")
        self.assertEqual(env_entry["apiKey"], "env-resolved")
        self.assertEqual(env_entry["api"], "openai-completions")
        # env_key 无法解析时保留引用
        unresolved = next(c for c in candidates if c["id"] == "chat-only")
        self.assertEqual((unresolved["apiKey"], unresolved["apiKeyEnv"]), ("", "MIRROR_TOKEN"))

    def test_claude_reader_reports_unmanaged_active_line(self):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://anthropic.example",
            "ANTHROPIC_AUTH_TOKEN": "line-key",
        }}), encoding="utf-8")
        candidates = engine.Hub().import_candidates("claude")
        self.assertEqual([c["id"] for c in candidates], ["claude-code"])
        self.assertEqual(candidates[0]["api"], "anthropic-messages")
        # 官方线路与 Hub 已管理的线路都不出现
        for base, token in (("https://api.anthropic.com", "official"),
                            ("https://api.deepseek.com/anthropic", "test-not-a-real-key")):
            settings.write_text(json.dumps({"env": {
                "ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": token}}), encoding="utf-8")
            self.assertEqual(engine.Hub().import_candidates("claude"), [])


class ImportApplyTests(unittest.TestCase):
    def setUp(self):
        context = isolated()
        self.home = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def config(self) -> dict:
        return json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))

    def test_apply_persists_provider_with_env_reference(self):
        result = server.api_import_apply("pi", {"items": [{
            "id": "user-gateway", "name": "User Gateway", "baseUrl": "https://user.example/v1",
            "apiKey": "user-key", "api": "openai-responses",
            "models": [{"id": "u-1", "input": ["text"], "contextWindow": 128000, "maxTokens": 32768}],
        }]})
        self.assertEqual(result["imported"], ["user-gateway"])
        provider = self.config()["providers"]["user-gateway"]
        self.assertEqual(provider["baseUrl"], "https://user.example/v1")
        self.assertEqual([m["id"] for m in provider["models"]], ["u-1"])

        result = server.api_import_apply("dsh", {"items": [{
            "id": "no-cred", "name": "No Cred", "baseUrl": "https://nocred.example/v1",
            "apiKey": "", "apiKeyEnv": "MISSING_KEY", "api": "openai-completions", "models": [],
        }]})
        self.assertEqual(result["imported"], ["no-cred"])
        self.assertEqual(self.config()["providers"]["no-cred"]["apiKeyEnv"], "MISSING_KEY")

    def test_scan_endpoint_validates_target(self):
        with self.assertRaises(ValueError):
            server.api_import_scan("omp")


if __name__ == "__main__":
    unittest.main()
