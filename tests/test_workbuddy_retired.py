"""Retired WorkBuddy routes must never contact the legacy upstream."""
import http.client
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gateway_runtime import export_connections
from gateway_server import Handler, Server, State
from handoff_summary import list_summary_routes
from support import engine, fixture, isolated, server
from sync_transaction import TransactionError
from workbuddy import provider_config


class RetiredWorkBuddyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hits = []
        hits = self.hits

        class Vendor(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                hits.append(self.path)
                self.send_response(500); self.end_headers()

        self.vendor = ThreadingHTTPServer(("127.0.0.1", 0), Vendor)
        threading.Thread(target=self.vendor.serve_forever, daemon=True).start()
        self.addCleanup(self.vendor.server_close)
        self.addCleanup(self.vendor.shutdown)

        self.gateway = Server(("127.0.0.1", 0), Handler)
        self.directory = self.root / "gateway"
        self.directory.mkdir()
        # A stale provider file from an older installation must also fail
        # closed, even if runtime export is bypassed.
        legacy = {"id": "retired-pi", "kind": "workbuddy-ai", "key": "legacy-local-key",
                  "models": ["model"], "chat": f"http://127.0.0.1:{self.vendor.server_port}/v1"}
        ordinary = {"id": "ordinary", "key": "normal-key", "models": ["other"],
                    "chat": f"http://127.0.0.1:{self.vendor.server_port}/v1"}
        (self.directory / "providers.json").write_text(json.dumps({"providers": [legacy, ordinary]}), encoding="utf-8")
        (self.directory / "hub-connections.json").write_text(json.dumps({"retired-pi": {"agent": "pi", "provider": "workbuddy-ai"},
                                                                            "ordinary": {"agent": "pi", "provider": "ordinary"}}), encoding="utf-8")
        self.gateway.state = State(self.directory, "admin")
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.gateway.shutdown)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                           headers={"Authorization": "Bearer legacy-local-key", "Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_old_dedicated_path_fails_closed(self):
        for method, path in (("GET", "/workbuddy/pi/v1/models"),
                             ("POST", "/workbuddy/pi/v1/chat/completions")):
            status, data = self.request(method, path, {"model": "model"} if method == "POST" else None)
            self.assertEqual(status, 410)
            self.assertIn("已取消", data["error"]["message"])
        self.assertEqual(self.hits, [])

    def test_old_namespaced_route_fails_closed_and_is_not_listed(self):
        status, models = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([row["id"] for row in models["data"]], ["ordinary/other"])
        status, data = self.request("POST", "/v1/chat/completions", {"model": "retired-pi/model", "messages": []})
        self.assertEqual(status, 410)
        self.assertIn("已取消", data["error"]["message"])
        self.assertEqual(self.hits, [])

    def test_export_and_summary_filter_legacy_kind_without_mutating_config(self):
        cfg = {"gateway": {"port": self.gateway.server_port}, "providers": {}}
        cfg["providers"]["workbuddy-ai"] = provider_config(cfg, [{"id": "model"}])
        original = json.dumps(cfg, sort_keys=True)
        rows, mapping = export_connections(cfg)
        self.assertEqual(rows["providers"], [])
        self.assertEqual(mapping, {})
        self.assertEqual(list_summary_routes(cfg), [])
        self.assertEqual(json.dumps(cfg, sort_keys=True), original)


class RetiredWorkBuddySelectionTests(unittest.TestCase):
    targets = ("claude", "pi", "dsh", "dsh-desktop", "codex", "opencode", "zcode")

    def config(self):
        cfg = fixture()
        cfg["providers"]["workbuddy-ai"] = provider_config(cfg, [{"id": "old-model"}])
        cfg["sync"]["targets"]["codex"] = {"enabled": True, "mode": "third-party"}
        cfg["sync"]["targets"]["zcode"]["mode"] = "third-party"
        cfg["sync"]["targets"]["opencode"] = {"enabled": True}
        return cfg

    def test_old_connection_is_unavailable_but_ordinary_connections_remain_ready(self):
        cfg = self.config()
        original = deepcopy(cfg)
        legacy = cfg["providers"]["workbuddy-ai"]
        ordinary = cfg["providers"]["deepseek"]
        for target in self.targets:
            with self.subTest(target=target):
                self.assertFalse(engine.agent_connection_ready(legacy, target))
                self.assertTrue(engine.agent_connection_ready(ordinary, target))
        self.assertEqual(cfg, original)

    def test_retired_override_blocks_fallback_to_default_across_agents(self):
        for target in self.targets:
            with self.subTest(target=target):
                cfg = self.config()
                canonical = engine.canonical_target(target)
                cfg["modelSync"]["overrides"] = {canonical: {"provider": "workbuddy-ai", "model": "old-model"}}
                self.assertEqual(engine.agent_model_tiers(cfg, target), {})
                self.assertIsNone(engine.resolve_agent_model(cfg, target))
                with self.assertRaisesRegex(ValueError, "WorkBuddy.*已停用|已停用.*WorkBuddy"):
                    engine.assert_no_retired_workbuddy_selection(cfg, target)

    def test_retired_default_blocks_only_when_no_higher_choice_is_usable(self):
        for target in self.targets:
            with self.subTest(target=target):
                cfg = self.config()
                cfg["modelSync"]["default"] = {"provider": "workbuddy-ai", "model": "old-model"}
                self.assertEqual(engine.agent_model_tiers(cfg, target), {})
                with self.assertRaisesRegex(ValueError, "WorkBuddy"):
                    engine.assert_no_retired_workbuddy_selection(cfg, target)
                canonical = engine.canonical_target(target)
                cfg["modelSync"]["overrides"] = {canonical: {"provider": "deepseek", "model": "deepseek/model-1"}}
                resolved = engine.resolve_agent_model(cfg, target)
                self.assertEqual(resolved["provider"], "deepseek")
                engine.assert_no_retired_workbuddy_selection(cfg, target)

    def test_claude_retired_main_and_secondary_tiers_fail_closed(self):
        cfg = self.config()
        cfg["modelSync"]["overrides"] = {"claude": {
            "provider": "deepseek", "model": "deepseek/model-1",
            "tiers": {"model": {"provider": "workbuddy-ai", "model": "old-model"}}}}
        self.assertNotIn("model", engine.agent_model_tiers(cfg, "claude"))
        with self.assertRaisesRegex(ValueError, "WorkBuddy"):
            engine.assert_no_retired_workbuddy_selection(cfg, "claude")
        cfg["modelSync"]["overrides"]["claude"]["tiers"] = {
            "haiku": {"provider": "workbuddy-ai", "model": "old-model"}}
        self.assertEqual(engine.resolve_agent_model(cfg, "claude")["provider"], "deepseek")
        self.assertNotIn("haiku", engine.agent_model_tiers(cfg, "claude"))
        with self.assertRaisesRegex(ValueError, "haiku.*WorkBuddy"):
            engine.assert_no_retired_workbuddy_selection(cfg, "claude")
        # An explicit valid main tier outranks the retired platform/default choice.
        cfg["modelSync"]["overrides"]["claude"] = {
            "provider": "workbuddy-ai", "model": "old-model",
            "tiers": {"model": {"provider": "deepseek", "model": "deepseek/model-1"}}}
        self.assertEqual(engine.resolve_agent_model(cfg, "claude")["provider"], "deepseek")
        engine.assert_no_retired_workbuddy_selection(cfg, "claude")

    def test_official_modes_do_not_use_or_block_retired_modelsync(self):
        cfg = self.config()
        cfg["modelSync"]["default"] = {"provider": "workbuddy-ai", "model": "old-model"}
        for target in ("codex", "zcode"):
            cfg["sync"]["targets"][target]["mode"] = "official"
            engine.assert_no_retired_workbuddy_selection(cfg, target)

    def test_claude_secondary_block_precedes_preview_and_direct_write(self):
        with isolated() as home:
            cfg = self.config()
            cfg["modelSync"]["overrides"] = {"claude": {
                "provider": "deepseek", "model": "deepseek/model-1",
                "tiers": {"haiku": {"provider": "workbuddy-ai", "model": "old-model"}}}}
            server.HUB_CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
            native = home / ".claude/settings.json"
            native.parent.mkdir(parents=True)
            native.write_text('{"env":{"ANTHROPIC_MODEL":"keep"}}', encoding="utf-8")
            before = {str(path.relative_to(home)): path.read_bytes()
                      for path in home.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(TransactionError, "haiku.*WorkBuddy"):
                engine.Hub().preview_push(["claude"])
            with self.assertRaisesRegex(ValueError, "haiku.*WorkBuddy"):
                engine.Hub().push_claude()
            after = {str(path.relative_to(home)): path.read_bytes()
                     for path in home.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_preview_and_direct_push_reject_without_touching_config_or_credentials(self):
        with isolated() as home:
            cfg = self.config()
            cfg["modelSync"]["default"] = {"provider": "workbuddy-ai", "model": "old-model"}
            server.HUB_CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
            auth = home / ".agent-hub/data/gateway/config/agent-hub/workbuddy-auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"access":"fake-test-token","refresh":"fake-refresh"}', encoding="utf-8")
            native = (".pi/agent/settings.json", ".dsh/settings.yaml", ".claude/settings.json",
                      ".codex/config.toml", ".config/opencode/opencode.json", ".zcode/v2/provider_config.json")
            for rel in native:
                path = home / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("native test sentinel", encoding="utf-8")

            def files():
                return {str(path.relative_to(home)): path.read_bytes()
                        for path in home.rglob("*") if path.is_file()}

            before = files()
            for target in ("claude", "pi", "dsh", "codex", "opencode", "zcode"):
                with self.subTest(target=target, mode="preview"):
                    with self.assertRaisesRegex(TransactionError, "WorkBuddy"):
                        engine.Hub().preview_push([target])
                    self.assertEqual(files(), before)
                with self.subTest(target=target, mode="direct"):
                    with self.assertRaisesRegex(ValueError, "WorkBuddy"):
                        getattr(engine.Hub(), "push_" + target)()
                    self.assertEqual(files(), before)

    def test_removed_api_routes_and_discovery_do_not_change_old_data(self):
        with isolated() as home:
            cfg = self.config()
            cfg["modelSync"]["default"] = {"provider": "workbuddy-ai", "model": "old-model"}
            server.HUB_CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
            auth = home / ".agent-hub/data/gateway/config/agent-hub/workbuddy-auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"access":"fake-test-token","refresh":"fake-refresh"}', encoding="utf-8")
            before_config, before_auth = server.HUB_CONFIG.read_bytes(), auth.read_bytes()
            for method, path in (("GET", "/api/workbuddy"), ("POST", "/api/workbuddy/login"),
                                 ("DELETE", "/api/workbuddy/login"), ("POST", "/api/workbuddy/connect"),
                                 ("POST", "/api/workbuddy/logout")):
                with self.subTest(method=method, path=path):
                    status, _ = server.dispatch_api(method, path)
                    self.assertEqual(status, 404)
            with patch.object(server, "discover_models", side_effect=AssertionError("unexpected discovery")):
                status, payload = server.dispatch_api("GET", "/api/providers/workbuddy-ai/discover")
            self.assertEqual(status, 400)
            self.assertIn("已取消", payload["error"])
            # The read-only models page must still load so the user can change selection.
            status, view = server.dispatch_api("GET", "/api/modelsync")
            self.assertEqual(status, 200)
            self.assertIn("WorkBuddy", view["retiredSelections"]["pi"])
            self.assertIn("WorkBuddy", view["retiredSelections"]["claude"])
            self.assertEqual(server.HUB_CONFIG.read_bytes(), before_config)
            self.assertEqual(auth.read_bytes(), before_auth)

    def test_user_can_replace_retired_choice_through_modelsync_api(self):
        with isolated() as home:
            cfg = self.config()
            cfg["modelSync"]["default"] = {"provider": "workbuddy-ai", "model": "old-model"}
            server.HUB_CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
            auth = home / ".agent-hub/data/gateway/config/agent-hub/workbuddy-auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"access":"fake-test-token"}', encoding="utf-8")
            auth_before = auth.read_bytes()
            status, response = server.dispatch_api("PUT", "/api/modelsync", body={"modelSync": {
                "default": {"provider": "deepseek", "model": "deepseek/model-1"}}})
            self.assertEqual(status, 200)
            self.assertEqual(response["resolved"]["pi"]["provider"], "deepseek")
            self.assertEqual(response["retiredSelections"], {})
            saved = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
            self.assertEqual(saved["providers"]["workbuddy-ai"], cfg["providers"]["workbuddy-ai"])
            self.assertEqual(auth.read_bytes(), auth_before)


if __name__ == "__main__":
    unittest.main()
