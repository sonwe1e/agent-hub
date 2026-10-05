from __future__ import annotations

import json
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from support import isolated, engine, server


def fake_push_all(h, only=None):
    results = []
    seen = set()
    for name, target in h.cfg.get("sync", {}).get("targets", {}).items():
        if not target.get("enabled", False) or (only is not None and name not in only):
            continue
        canonical = target.get("aliasOf") or name
        if canonical in seen:
            continue
        seen.add(canonical)
        try:
            getattr(h, "push_" + canonical.replace("-", "_"))()
            results.append(f"✓ {canonical}: 配置已写入")
        except Exception as exc:
            results.append(f"✗ {canonical}: {exc}")
    return results



class APITests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def config(self):
        return json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))

    def model(self, mid="test/model", **extra):
        return {"id": mid, "name": "Test model", "contextWindow": 128000,
                "maxTokens": 32768, "reasoning": False, "input": ["text"], **extra}

    def adapters(self):
        stack = ExitStack()
        for name in ("pi", "mcode", "zcode", "dsh", "claude", "cc_switch", "omp"):
            stack.enter_context(patch.object(engine.Hub, f"push_{name}"))
        stack.enter_context(patch.object(engine.Hub, "push_all", autospec=True, side_effect=fake_push_all))
        self.addCleanup(stack.close)
        return stack

    def http(self, method, path, body=None, headers=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", data=data,
                                         method=method, headers=headers or {"Content-Type": "application/json"})
        try:
            try:
                response = urllib.request.urlopen(request)
            except urllib.error.HTTPError as e:
                response = e
            with response:
                raw = response.read().decode()
                return response.code, json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()

    def test_provider_validation_is_atomic(self):
        before = server.HUB_CONFIG.read_bytes()
        with self.assertRaises(ValueError):
            server.api_add_provider({"id": "new", "name": "New", "baseUrl": "not-a-url", "apiKey": "fake"})
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_provider_update_preserves_models(self):
        original = self.config()["providers"]["deepseek"]["models"]
        server.api_update_provider("deepseek", {"displayName": "Changed"})
        self.assertEqual(original, self.config()["providers"]["deepseek"]["models"])
        self.assertTrue(list((self.home / ".agent-hub/backups").glob("*")))

    def test_provider_invalid_update_does_not_write(self):
        before = server.HUB_CONFIG.read_bytes()
        with self.assertRaises(ValueError):
            server.api_update_provider("deepseek", {"baseUrl": "ftp://example.com"})
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_readded_provider_clears_tombstone(self):
        config = self.config()
        config["removedProviders"]["new"] = {"reason": "old"}
        engine.save_json(server.HUB_CONFIG, config)
        server.api_add_provider({"id": "new", "name": "New", "baseUrl": "https://example.com/v1", "apiKey": "fake"})
        self.assertNotIn("new", self.config()["removedProviders"])

    def test_continuous_saves_keep_separate_backups(self):
        server.api_update_provider("deepseek", {"displayName": "First"})
        server.api_update_provider("deepseek", {"displayName": "Second"})
        self.assertEqual(len(list((self.home / ".agent-hub/backups").glob("*"))), 2)

    def test_bad_provider_id_type_returns_validation_error(self):
        with self.assertRaises(ValueError):
            server.api_add_provider({"id": 123})

    def test_model_update_preserves_advanced_fields(self):
        server.api_update_model("deepseek", "deepseek/model-1", {"name": "Renamed", "reasoning": False})
        model = self.config()["providers"]["deepseek"]["models"][0]
        self.assertEqual(model["cost"], {"input": 0.5})
        self.assertEqual(model["compat"], {"custom": True})
        self.assertFalse(model["reasoning"])

    def test_model_rename_collision_is_atomic(self):
        before = server.HUB_CONFIG.read_bytes()
        with self.assertRaises(ValueError):
            server.api_update_model("deepseek", "deepseek/model-1", {"id": "deepseek/model-2"})
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_missing_model_cannot_update_another_by_body_id(self):
        with self.assertRaises(ValueError):
            server.api_update_model("deepseek", "missing", {"id": "deepseek/model-1"})

    def test_invalid_model_numbers_rejected(self):
        for n in (0, -1, 1.5, True, None, "foo"):
            with self.subTest(n=n), self.assertRaises(ValueError):
                server.api_add_model("deepseek", self.model(contextWindow=n))

    def test_invalid_input_rejected(self):
        for inp in ([], None, [1], [""]):
            with self.subTest(inp=inp), self.assertRaises(ValueError):
                server.api_add_model("deepseek", self.model(input=inp))

    def test_duplicate_model_rejected(self):
        with self.assertRaises(ValueError):
            server.api_add_model("deepseek", self.model("deepseek/model-1"))

    def test_batch_delete_count(self):
        result = server.api_batch_delete_models("deepseek", {"ids": ["deepseek/model-1", "deepseek/model-2", "missing"]})
        self.assertEqual(result["removed"], 2)
        self.assertEqual(len(self.config()["providers"]["deepseek"]["models"]), 5)

    def test_import_preserves_existing_tuning(self):
        original = self.config()["providers"]["deepseek"]["models"][0]
        result = server.api_import_models("deepseek", {"models": [self.model("deepseek/model-1", contextWindow=999), self.model()]})
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.config()["providers"]["deepseek"]["models"][0], original)

    def test_empty_or_duplicate_import_rejected(self):
        for models in ([], [self.model(), self.model()]):
            with self.assertRaises(ValueError):
                server.api_import_models("deepseek", {"models": models})

    def test_large_replace_requires_confirmation(self):
        with self.assertRaises(ValueError):
            server.api_import_models("deepseek", {"mode": "replace", "models": [self.model()]})
        server.api_import_models("deepseek", {"mode": "replace", "models": [self.model()], "confirm": True})
        self.assertEqual(len(self.config()["providers"]["deepseek"]["models"]), 1)

    def test_target_alias_follows_parent(self):
        server.api_update_targets({"targets": {"dsh": {"enabled": False}}})
        targets = self.config()["sync"]["targets"]
        self.assertFalse(targets["dsh"]["enabled"])
        self.assertFalse(targets["dsh-desktop"]["enabled"])

    def test_unknown_readonly_and_alias_target_rejected(self):
        # codex 从 v2 起是正式写入目标（只写 model_provider/model 与 custom 段），
        # 不再是只读目标；别名目标与未知名字仍然拒绝。
        for name in ("unknown", "dsh-desktop"):
            before = server.HUB_CONFIG.read_bytes()
            with self.assertRaises(ValueError):
                server.api_update_targets({"targets": {"pi": {"enabled": False}, name: {"enabled": True}}})
            self.assertEqual(before, server.HUB_CONFIG.read_bytes())
        server.api_update_targets({"targets": {"codex": {"enabled": False}}})
        self.assertFalse(self.config()["sync"]["targets"]["codex"]["enabled"])
        server.api_update_targets({"targets": {"codex": {"enabled": True}}})
        self.assertTrue(self.config()["sync"]["targets"]["codex"]["enabled"])

    def test_claude_provider_requires_a_claude_connection(self):
        """没有 Anthropic 连接的供应商不能作为 Claude 的可用供应商。"""
        server.api_add_provider({"id": "plain", "name": "Plain", "baseUrl": "https://a.example.com/v1",
                                 "apiKey": "sk-test"})
        self.assertNotIn("plain", server.api_status()["anthropicCapable"])
        server.api_add_provider({"id": "claude-plan", "name": "Claude Plan",
                                 "baseUrl": "", "apiKey": "sk-test",
                                 "anthropicBaseUrl": "https://claude.example/v1",
                                 "targets": ["claude"]})
        self.assertIn("claude-plan", server.api_status()["anthropicCapable"])

    def test_anthropic_connection_excluded_by_targets_is_not_available(self):
        server.api_add_provider({
            "id": "pi-only", "name": "Pi only", "baseUrl": "https://gateway.example/v1",
            "apiKey": "sk-test", "anthropicBaseUrl": "https://gateway.example/anthropic",
            "targets": ["pi"],
        })
        self.assertNotIn("pi-only", server.api_status()["anthropicCapable"])

    def test_openai_base_url_required_for_other_targets(self):
        provider = {
            "id": "claude-plan", "name": "Claude Plan", "apiKey": "sk-test",
            "anthropicBaseUrl": "https://claude.example/v1", "targets": ["claude"],
        }
        for targets in (None, [], ["claude", "pi"]):
            with self.subTest(targets=targets), self.assertRaisesRegex(ValueError, "baseUrl"):
                server.api_add_provider({**provider, "targets": targets})
        with self.assertRaisesRegex(ValueError, "baseUrl"):
            server.api_add_provider({**provider, "anthropicBaseUrl": ""})

    def test_agent_overrides_round_trip_and_validate(self):
        """agents.<agent>：按 Agent 覆盖端点 / 密钥 / 协议 / 模型与透传字段。"""
        body = {
            "id": "tokenfree", "name": "TokenFree", "displayName": "TokenFree",
            "baseUrl": "https://api.tokenfree.shop/v1", "apiKey": "sk-openai",
            "api": "openai-completions",
            "agents": {
                "claude": {"baseUrl": "https://api.tokenfree.shop/v1",
                           "apiKey": "sk-anthropic", "api": "anthropic-messages",
                           "models": [{"id": "claude-opus-5-5", "name": "Opus"}],
                           "env": {"ENABLE_TOOL_SEARCH": "true"},
                           "settings": {"effortLevel": "high"}},
                "codex": {"baseUrl": "https://api.tokenfree.shop/v1",
                          "apiKeyEnv": "TOKENFREE_API_KEY", "api": "openai-responses",
                          "models": [{"id": "gpt-6-astra", "name": "Astra"}]},
            },
        }
        server.api_add_provider(body)
        p = engine.Hub().providers["tokenfree"]
        self.assertEqual(p["agents"]["claude"]["apiKey"], "sk-anthropic")
        self.assertEqual(p["agents"]["codex"]["apiKeyEnv"], "TOKENFREE_API_KEY")
        self.assertEqual([m["id"] for m in p["agents"]["claude"]["models"]], ["claude-opus-5-5"])
        # 连接就绪度：Claude / Codex 各自就绪，pi 用默认连接
        status = server.api_status()
        self.assertIn("tokenfree", status["anthropicCapable"])
        self.assertIn("tokenfree", status["agentCapable"]["codex"])
        self.assertIn("tokenfree", status["agentCapable"]["pi"])
        # 错误的协议 / 结构 / 未知 Agent 被拒绝，且不落盘
        before = server.HUB_CONFIG.read_bytes()
        for bad in ({"claude": {"baseUrl": "https://x.example/v1", "api": "openai-completions"}},
                    {"codex": {"api": "openai-chat-completions"}},
                    {"claude": {"apiKeyEnv": "invalid space"}},
                    {"claude": {"env": {"env": "1"}}},
                    {"claude": {"models": "nope"}},
                    {"claude": "nope"},
                    {"nope": {}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                server.api_update_provider("tokenfree", {"agents": bad})
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())
        # 更新：整体替换（模型随连接一起走，护栏会拦住悄悄丢模型的写入）
        server.api_update_provider("tokenfree", {"agents": {
            "claude": {"baseUrl": "https://api.tokenfree.shop/v1", "api": "anthropic-messages",
                       "apiKey": "sk-anthropic", "models": [{"id": "claude-opus-5-5"}]},
            "codex": {"baseUrl": "https://api.tokenfree.shop/v1", "api": "openai-responses",
                      "apiKey": "sk-codex", "models": [{"id": "gpt-6-astra"}]}}})
        p = engine.Hub().providers["tokenfree"]
        self.assertEqual(set(p["agents"]), {"claude", "codex"})
        self.assertEqual([m["id"] for m in p["agents"]["claude"]["models"]], ["claude-opus-5-5"])
        # 整块替换掉已有连接会丢模型：护栏拒绝，不静默落盘
        before = server.HUB_CONFIG.read_bytes()
        with self.assertRaises(engine.ModelLossError):
            server.api_update_provider("tokenfree", {"agents": {
                "codex": {"api": "openai-responses", "models": [{"id": "gpt-6-astra"}]}}})
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_selected_sync_deduplicates_alias(self):
        self.adapters()
        result = server.api_push({"targets": ["dsh", "dsh-desktop"]})
        self.assertTrue(result["ok"])
        self.assertEqual([d["target"] for d in result["details"]], ["dsh"])
        engine.Hub.push_dsh.assert_called_once()
        engine.Hub.push_pi.assert_not_called()
        self.assertTrue(server.api_status()["pendingSync"])

    def test_sync_empty_disabled_unknown_rejected(self):
        self.adapters()
        for targets in ([], ["codex"], ["unknown"], "pi"):
            with self.assertRaises(ValueError):
                server.api_push({"targets": targets})
        engine.Hub.push_pi.assert_not_called()

    def test_legacy_claude_fields_still_validate_and_round_trip(self):
        """旧扁平字段（claudeEnv / claudeSettings / claudeModels）迁移前仍然可用。"""
        body = {
            "id": "tokenfree", "name": "TokenFree", "displayName": "TokenFree (Anthropic proxy)",
            "baseUrl": "https://api.tokenfree.shop", "apiKey": "sk-test-not-a-real-key",
            "anthropicBaseUrl": "https://api.tokenfree.shop",
            "targets": ["claude"],
            "claudeModels": {"model": "claude-sonnet-4-6", "subagent": "claude-sonnet-4-6"},
            "claudeEnv": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                          "ENABLE_TOOL_SEARCH": "true"},
            "claudeSettings": {"model": "claude-sonnet-4-6", "effortLevel": "high",
                               "permissions": {"allow": []}},
        }
        server.api_add_provider(body)
        p = engine.Hub().providers["tokenfree"]
        self.assertEqual(p["claudeModels"], body["claudeModels"])
        self.assertEqual(p["claudeEnv"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(p["claudeSettings"]["effortLevel"], "high")
        self.assertEqual(p["targets"], ["claude"])
        # 旧字段在读取时映射成 Claude 连接：端点 / 密钥 / 透传都按它解析
        conn = engine.agent_connection(p, "claude")
        self.assertEqual(conn["baseUrl"], "https://api.tokenfree.shop")
        self.assertEqual(conn["api"], "anthropic-messages")
        self.assertEqual(conn["env"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(conn["settings"]["effortLevel"], "high")
        # 更新：可改，也可清空
        server.api_update_provider("tokenfree", {"claudeSettings": {"effortLevel": "low"}})
        p = engine.Hub().providers["tokenfree"]
        self.assertEqual(p["claudeSettings"], {"effortLevel": "low"})
        self.assertEqual(p["claudeEnv"]["ENABLE_TOOL_SEARCH"], "true")  # 未声明的字段保留
        # 非法值被拒绝且不落盘
        before = server.HUB_CONFIG.read_bytes()
        with self.assertRaises(ValueError):
            server.api_update_provider("tokenfree", {"claudeEnv": "nope"})
        with self.assertRaises(ValueError):
            server.api_update_provider("tokenfree", {"targets": ["bogus"]})
        self.assertEqual(engine.Hub().providers["tokenfree"]["claudeEnv"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(engine.Hub().providers["tokenfree"]["targets"], ["claude"])

    def test_openai_only_provider_is_not_claude_ready(self):
        """只有默认 OpenAI 端点、没有声明 Claude 连接的供应商不进 Claude 同步。"""
        server.api_add_provider({"id": "plain", "name": "Plain", "baseUrl": "https://a.example.com/v1",
                                 "apiKey": "sk-test"})
        self.assertNotIn("plain", server.api_status()["anthropicCapable"])
        p = engine.Hub().providers["plain"]
        self.assertEqual(engine.agent_connection(p, "claude")["baseUrl"], "")

    def test_model_crud_is_scoped_to_its_connection(self):
        """模型按连接存放：写入 claude 连接的模型不进默认模型库。"""
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["tokenfree"] = {
            "name": "TokenFree", "baseUrl": "https://gateway.example/v1", "apiKey": "sk-openai",
            "api": "openai-completions", "enabled": True,
            "models": [{"id": "gpt-6-astra", "name": "Astra"}],
            "agents": {"claude": {"baseUrl": "https://gateway.example/v1",
                                  "api": "anthropic-messages", "apiKey": "sk-anthropic",
                                  "models": []}},
        }
        engine.save_json(server.HUB_CONFIG, cfg)
        server.api_add_model("tokenfree", {"id": "claude-opus-5-5", "name": "Opus", "agent": "claude",
                                           "contextWindow": 200000, "maxTokens": 32768,
                                           "input": ["text"], "reasoning": True})
        p = engine.Hub().providers["tokenfree"]
        self.assertEqual([m["id"] for m in p["models"]], ["gpt-6-astra"])
        self.assertEqual([m["id"] for m in p["agents"]["claude"]["models"]], ["claude-opus-5-5"])
        # 删除同样只作用于该连接（不带 agent 就只作用于默认连接）
        with self.assertRaises(ValueError):
            server.api_delete_model("tokenfree", "claude-opus-5-5")
        server.api_delete_model("tokenfree", "claude-opus-5-5", "claude")
        p = engine.Hub().providers["tokenfree"]
        self.assertEqual(p["agents"]["claude"]["models"], [])
        self.assertEqual([m["id"] for m in p["models"]], ["gpt-6-astra"])

    def test_discover_uses_the_selected_connection(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["tokenfree"] = {
            "name": "TokenFree", "baseUrl": "https://gateway.example/v1", "apiKey": "sk-openai",
            "api": "openai-completions", "enabled": True,
            "models": [],
            "agents": {"claude": {"baseUrl": "https://gateway.example/v1",
                                  "api": "anthropic-messages", "apiKey": "sk-anthropic",
                                  "models": []}},
        }
        engine.save_json(server.HUB_CONFIG, cfg)
        calls = []

        def fake_http(url, headers, timeout=15.0):
            calls.append((url, dict(headers)))
            if url.endswith("/v1/models") and "x-api-key" in headers:
                return {"data": [{"id": "claude-opus-5-5"}]}
            return {"data": [{"id": "gpt-6-astra"}]}

        with unittest.mock.patch("discover._http_get_json", side_effect=fake_http):
            claude_models = server.api_discover_models("tokenfree", "claude")["models"]
            default_models = server.api_discover_models("tokenfree")["models"]
        self.assertEqual([m["id"] for m in claude_models], ["claude-opus-5-5"])
        self.assertEqual([m["id"] for m in default_models], ["gpt-6-astra"])
        # Anthropic 连接用 x-api-key，默认连接用 Bearer
        self.assertTrue(any("x-api-key" in headers for _, headers in calls))
        self.assertTrue(any("Authorization" in headers for _, headers in calls))
        # 导入到指定连接
        server.api_import_models("tokenfree", {"agent": "claude",
                                               "models": [{"id": "claude-opus-5-5", "name": "Opus",
                                                           "contextWindow": 200000,
                                                           "maxTokens": 32768,
                                                           "input": ["text"], "reasoning": True}]})
        p = engine.Hub().providers["tokenfree"]
        self.assertEqual([m["id"] for m in p["agents"]["claude"]["models"]], ["claude-opus-5-5"])
        self.assertEqual(p["models"], [])

    def test_full_sync_clears_pending_and_edit_restores_it(self):
        self.adapters()
        self.assertTrue(server.api_push()["ok"])
        self.assertFalse(server.api_status()["pendingSync"])
        server.api_add_model("deepseek", self.model())
        self.assertTrue(server.api_status()["pendingSync"])

    def test_failed_sync_reports_failure_and_keeps_pending(self):
        self.adapters()
        engine.Hub.push_pi.side_effect = RuntimeError("test failure")
        result = server.api_push()
        self.assertFalse(result["ok"])
        self.assertFalse(next(d for d in result["details"] if d["target"] == "pi")["ok"])
        self.assertTrue(server.api_status()["pendingSync"])

    def test_partial_sync_preserves_other_hashes(self):
        self.adapters()
        engine.save_json(engine.HUB_STATE, {"hashes": {"untouched": "old"}})
        server.api_push({"targets": ["pi"]})
        self.assertEqual(engine.load_json(engine.HUB_STATE)["hashes"]["untouched"], "old")

    def test_http_encoded_slash_model_update_and_delete(self):
        code, result = self.http("PUT", "/api/providers/deepseek/models/deepseek%2Fmodel-1", {"name": "HTTP edit"})
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        code, _ = self.http("DELETE", "/api/providers/deepseek/models/deepseek%2Fmodel-1")
        self.assertEqual(code, 200)
        self.assertEqual(len(self.config()["providers"]["deepseek"]["models"]), 6)

    def test_http_non_object_rejected(self):
        code, _ = self.http("POST", "/api/providers", [])
        self.assertEqual(code, 400)

    def test_http_cross_origin_rejected(self):
        code, _ = self.http("POST", "/api/push", {}, {"Origin": "https://untrusted.example"})
        self.assertEqual(code, 403)

    def test_http_static_traversal_rejected(self):
        code, _ = self.http("GET", "/%2e%2e/server.py")
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main()
