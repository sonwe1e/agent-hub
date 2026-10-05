from __future__ import annotations

import json
import threading
import unittest
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

    def test_claude_provider_requires_compatibility(self):
        with self.assertRaises(ValueError):
            server.api_update_targets({"targets": {"claude": {"activeProvider": "stepfun"}}})
        server.api_update_targets({"targets": {"claude": {"activeProvider": "deepseek"}}})

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

    def test_custom_provider_declarative_fields_round_trip(self):
        """自定义供应商：claudeEnv / claudeSettings / targets 经 API 增减后原样保留。"""
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
        self.assertEqual(p["claudeEnv"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(p["claudeSettings"]["effortLevel"], "high")
        self.assertEqual(p["claudeSettings"]["permissions"], {"allow": []})
        self.assertEqual(p["targets"], ["claude"])
        # 更新：可改，也可清空
        server.api_update_provider("tokenfree", {"claudeSettings": {"effortLevel": "low"}})
        p = engine.Hub().providers["tokenfree"]
        self.assertEqual(p["claudeSettings"], {"effortLevel": "low"})
        self.assertEqual(p["claudeEnv"]["ENABLE_TOOL_SEARCH"], "true")  # 未声明的字段保留
        # 非法值被拒绝且不落盘
        with self.assertRaises(ValueError):
            server.api_update_provider("tokenfree", {"claudeEnv": "nope"})
        with self.assertRaises(ValueError):
            server.api_update_provider("tokenfree", {"targets": ["bogus"]})
        self.assertEqual(engine.Hub().providers["tokenfree"]["claudeEnv"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(engine.Hub().providers["tokenfree"]["targets"], ["claude"])

    def test_custom_provider_can_become_active_claude_provider(self):
        """只有声明了 Anthropic 端点且启用的自定义供应商才能成为 claude 的 active。"""
        server.api_add_provider({
            "id": "tokenfree", "name": "TokenFree", "baseUrl": "https://api.tokenfree.shop",
            "apiKey": "sk-test-not-a-real-key", "anthropicBaseUrl": "https://api.tokenfree.shop",
            "targets": ["claude"],
        })
        server.api_update_targets({"targets": {"claude": {"activeProvider": "tokenfree"}}})
        self.assertEqual(engine.Hub().cfg["sync"]["targets"]["claude"]["activeProvider"], "tokenfree")
        # 没有 Anthropic 端点的不行
        server.api_add_provider({"id": "plain", "name": "Plain", "baseUrl": "https://a.example.com/v1",
                                 "apiKey": "sk-test"})
        with self.assertRaises(ValueError):
            server.api_update_targets({"targets": {"claude": {"activeProvider": "plain"}}})

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
