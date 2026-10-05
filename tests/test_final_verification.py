"""Independent combined-adapter checks, using synthetic configuration only."""
import json
import shutil
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from support import isolated, engine, server
import sync_transaction as transaction


class CombinedAdapterVerificationTests(unittest.TestCase):
    def setUp(self):
        context = isolated()
        self.home = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        transaction._PLANS.clear()
        self.addCleanup(transaction._PLANS.clear)
        self.codex = self.home / ".codex" / "config.toml"
        self.codex.parent.mkdir()
        self.codex.write_text('model = "official-original"\nnotify = ["user-hook"]\n')
        self.pi = self.home / ".pi" / "agent"
        self.pi.mkdir(parents=True)
        (self.pi / "settings.json").write_text(json.dumps({"theme": "user-theme"}))
        (self.pi / "auth.json").write_text(json.dumps({
            "unmanaged": {"type": "oauth", "token": "synthetic-unmanaged-token"}}))
        self.cfg = json.loads(engine.HUB_CONFIG.read_text())
        self.cfg["providers"] = {"gateway": {
            "name": "Shared gateway", "enabled": True,
            "baseUrl": "https://default.example/v1", "api": "openai-completions",
            "apiKey": "synthetic-default-key", "models": [{"id": "default-model"}],
            "agents": {
                "pi": {"baseUrl": "https://pi.example/v1", "apiKey": "synthetic-pi-key",
                       "models": [{"id": "anthropic/stealth/space-bunny-alpha[1m]"},
                                  {"id": "another-brand/model-two"}]},
                "codex": {"baseUrl": "https://responses.example/v1", "api": "openai-responses",
                          "apiKey": "synthetic-codex-key",
                          "models": [{"id": "claude-through-responses"}]},
            },
        }}
        self.cfg["modelSync"] = {
            "default": {"provider": "gateway", "model": "default-model"},
            "overrides": {
                "pi": {"provider": "gateway", "model": "anthropic/stealth/space-bunny-alpha[1m]"},
                "codex": {"provider": "gateway", "model": "claude-through-responses", "reasoningEffort": "ultra"},
            },
        }
        self.cfg["sync"]["targets"] = {
            "pi": {"enabled": True}, "codex": {"enabled": True, "mode": "third-party"}}
        engine.save_json(engine.HUB_CONFIG, self.cfg)

    def native_bytes(self):
        return {path.relative_to(self.home).as_posix(): path.read_bytes()
                for root in (self.pi, self.codex.parent) for path in root.glob("*") if path.is_file()}

    def test_combined_preview_apply_uses_each_connection_and_keeps_model_brands(self):
        before = self.native_bytes()
        plan = server.api_preview({"targets": ["codex", "pi"]})
        self.assertEqual(self.native_bytes(), before)
        for secret in ("synthetic-default-key", "synthetic-pi-key", "synthetic-codex-key",
                       "synthetic-unmanaged-token"):
            self.assertNotIn(secret, json.dumps(plan))
        self.assertTrue(server.api_apply({"planId": plan["planId"]})["ok"])
        codex = tomllib.loads(self.codex.read_text())
        self.assertEqual(codex["model"], "claude-through-responses")
        self.assertEqual(codex["model_reasoning_effort"], "ultra")
        self.assertEqual(codex["notify"], ["user-hook"])
        route = codex["model_providers"]["agent_hub"]
        self.assertEqual(route["base_url"], "https://responses.example/v1")
        self.assertEqual(route["wire_api"], "responses")
        self.assertEqual(route["experimental_bearer_token"], "synthetic-codex-key")
        self.assertFalse(route["requires_openai_auth"])
        pi = json.loads((self.pi / "models.json").read_text())["providers"]["gateway"]
        self.assertEqual(pi["apiKey"], "synthetic-pi-key")
        self.assertEqual(pi["baseUrl"], "https://pi.example/v1")
        self.assertEqual(pi["api"], "openai-completions")
        self.assertEqual([m["id"] for m in pi["models"]], [
            "anthropic/stealth/space-bunny-alpha[1m]", "another-brand/model-two"])
        settings = json.loads((self.pi / "settings.json").read_text())
        self.assertEqual(settings["theme"], "user-theme")
        self.assertEqual(settings["defaultModel"], "anthropic/stealth/space-bunny-alpha[1m]")
        self.assertEqual(settings["enabledModels"], [
            "gateway/anthropic/stealth/space-bunny-alpha[1m]", "gateway/another-brand/model-two"])
        self.assertEqual(json.loads((self.pi / "auth.json").read_text())["unmanaged"],
                         {"type": "oauth", "token": "synthetic-unmanaged-token"})
        with self.assertRaisesRegex(transaction.TransactionError, "missing or expired"):
            server.api_apply({"planId": plan["planId"]})

    def test_late_pi_scope_failure_does_not_write_staged_codex_output(self):
        (self.pi / "settings.json").write_text(json.dumps({"enabledModels": "invalid-scope"}))
        before = self.native_bytes()
        with self.assertRaisesRegex(transaction.TransactionError, "pi"):
            server.api_preview({"targets": ["codex", "pi"]})
        self.assertEqual(self.native_bytes(), before)
        self.assertFalse((self.codex.parent / ".agent-hub-state.json").exists())
        self.assertFalse(transaction._PLANS)

    def test_compiler_source_change_after_preview_invalidates_apply(self):
        copied = self.home / "copied-engine"
        copied.mkdir()
        original = Path(engine.__file__)
        for name in ("hub.py", "codex_config.py"):
            shutil.copyfile(original.with_name(name), copied / name)
        with patch.object(engine, "__file__", str(copied / "hub.py")):
            plan = server.api_preview({"targets": ["codex"]})
            compiler = copied / "codex_config.py"
            compiler.write_text(compiler.read_text() + "\n# Synthetic code change after preview.\n")
            before = self.native_bytes()
            with self.assertRaisesRegex(transaction.TransactionError, "stale"):
                server.api_apply({"planId": plan["planId"]})
            self.assertEqual(self.native_bytes(), before)


if __name__ == "__main__":
    unittest.main()
