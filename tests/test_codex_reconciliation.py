"""Explicit-preview recovery from native Codex edits; synthetic files only."""
import copy
import json
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from support import isolated, engine, server
from codex_config import compile_config, FIELDS
from test_codex_official import BASE, PROVIDER, SELECTION
import sync_transaction as transaction


class CodexReconciliationTests(unittest.TestCase):
    def test_explicit_preview_can_replace_each_changed_managed_field(self):
        third, state = compile_config(BASE, None, "third-party", PROVIDER, SELECTION)
        variants = [
            third.replace('model = "third-party-model"', 'model = "manual-model"'),
            third.replace('model_reasoning_effort = "high"', 'model_reasoning_effort = "low"'),
            third.replace('model_reasoning_effort = "high"\n', ''),
            third.replace('model_provider = "agent_hub"', 'model_provider = "custom"'),
        ]
        for changed in variants:
            with self.subTest(changed=changed):
                original_state = copy.deepcopy(state)
                with self.assertRaisesRegex(ValueError, "外部"):
                    compile_config(changed, state, "third-party", PROVIDER, SELECTION)
                warnings = []
                output, next_state = compile_config(changed, state, "third-party", PROVIDER,
                    SELECTION, reconcile=True, warnings=warnings)
                self.assertEqual(tomllib.loads(output), tomllib.loads(third))
                self.assertEqual(next_state, state)
                self.assertEqual(state, original_state)
                self.assertEqual(len(warnings), 1)
                self.assertNotIn("manual-model", warnings[0])

    def test_added_effort_can_be_reconciled_and_then_official_restored(self):
        third, state = compile_config(BASE, None, "third-party", PROVIDER, {"model": "third-party-model"})
        changed = 'model_reasoning_effort = "low"\n' + third
        official, inactive = compile_config(changed, state, "official", reconcile=True)
        self.assertEqual(tomllib.loads(official), tomllib.loads(BASE))
        self.assertFalse(inactive["active"])

    def test_external_owned_provider_edit_requires_explicit_preview(self):
        third, state = compile_config(BASE, None, "third-party", PROVIDER, SELECTION)
        changed = third.replace("fixture.example", "external.example")
        for mode in ("third-party", "official"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "外部"):
                    compile_config(changed, state, mode, PROVIDER, SELECTION)
                warnings = []
                output, _ = compile_config(changed, state, mode, PROVIDER, SELECTION,
                                           reconcile=True, warnings=warnings)
                self.assertIn("model_providers.agent_hub", warnings[0])
                self.assertNotIn("external.example", warnings[0])
                self.assertEqual(tomllib.loads(output), tomllib.loads(third if mode == "third-party" else BASE))

    def test_native_official_switch_preserves_new_baseline_with_or_without_owned_table(self):
        third, state = compile_config(BASE, None, "third-party", PROVIDER, SELECTION)
        for provider_root in ('model_provider = "openai"\n', ''):
            changed = third.replace('model_provider = "agent_hub"\n', provider_root)
            changed = changed.replace('model = "third-party-model"', 'model = "new-official-model"')
            changed = changed.replace('model_reasoning_effort = "high"', 'model_reasoning_effort = "low"')
            for keep_table in (True, False):
                live = changed if keep_table else changed.split('[model_providers.agent_hub]')[0]
                with self.subTest(root=provider_root, keep_table=keep_table):
                    restored, inactive = compile_config(live, state, "official", reconcile=True)
                    parsed = tomllib.loads(restored)
                    self.assertEqual(parsed["model"], "new-official-model")
                    self.assertEqual(parsed["model_reasoning_effort"], "low")
                    self.assertEqual(parsed.get("model_provider", "openai"), "openai")
                    self.assertFalse(inactive["active"])
                    self.assertNotIn("agent_hub", parsed["model_providers"])
                    # Going straight from a native official switch to another
                    # Hub provider also captures that newer official baseline.
                    new, active = compile_config(live, state, "third-party", PROVIDER,
                                                 SELECTION, reconcile=True)
                    final, _ = compile_config(new, active, "official")
                    self.assertEqual(tomllib.loads(final), parsed)

    def test_rebaselined_official_parent_table_presence_round_trips(self):
        for initial_has_parent in (True, False):
            initial = 'model = "official"\n' + ('[model_providers]\n' if initial_has_parent else '')
            old, state = compile_config(initial, None, "third-party", PROVIDER, SELECTION)
            owned = '[model_providers.agent_hub]' + old.split('[model_providers.agent_hub]')[1]
            for new_has_parent in (True, False):
                for keep_owned in (True, False):
                    for explicit_openai in (True, False):
                        native = 'model = "new-official"\n'
                        if explicit_openai:
                            native += 'model_provider = "openai"\n'
                        if new_has_parent:
                            native += '[model_providers]\n'
                        live = native + (owned if keep_owned else '')
                        with self.subTest(initial=initial_has_parent, new=new_has_parent,
                                          owned=keep_owned, explicit=explicit_openai):
                            third, updated = compile_config(live, state, "third-party", PROVIDER,
                                                            SELECTION, reconcile=True)
                            self.assertEqual(updated["officialProviderTablePresent"], new_has_parent)
                            restored, _ = compile_config(third, updated, "official")
                            self.assertEqual(tomllib.loads(restored), tomllib.loads(native))

    def test_parent_header_can_change_while_hub_provider_stays_active(self):
        for initial_has_parent in (True, False):
            initial = 'model = "official"\n' + ('[model_providers]\n' if initial_has_parent else '')
            third, state = compile_config(initial, None, "third-party", PROVIDER, SELECTION)
            live = (third.replace('[model_providers]\n', '') if initial_has_parent else
                    third.replace('[model_providers.agent_hub]', '[model_providers]\n[model_providers.agent_hub]'))
            for reconcile in (True, False):
                with self.subTest(initial=initial_has_parent, reconcile=reconcile):
                    restored, _ = compile_config(live, state, "official", reconcile=reconcile)
                    expected = {"model": "official"}
                    if not initial_has_parent:
                        expected["model_providers"] = {}
                    self.assertEqual(tomllib.loads(restored), expected)

    def test_reconciled_a_b_official_cycle_preserves_unrelated_user_changes(self):
        first, state = compile_config(BASE, None, "third-party", PROVIDER, SELECTION)
        changed = first.replace('model = "third-party-model"', 'model = "native-change"')
        changed = changed.replace('notify = ["my-hook"]', 'notify = ["new-hook"]')
        provider_b = dict(PROVIDER, name="Provider B", baseUrl="https://second.example/v1")
        second, state = compile_config(changed, state, "third-party", provider_b,
                                       {"model": "claude-through-responses"}, reconcile=True)
        self.assertEqual(tomllib.loads(second)["model"], "claude-through-responses")
        final, _ = compile_config(second, state, "official")
        expected = tomllib.loads(BASE)
        expected["notify"] = ["new-hook"]
        self.assertEqual(tomllib.loads(final), expected)
        self.assertIn("# keep comment", final)

    def test_reconciliation_never_claims_an_unowned_table(self):
        collision = BASE + '[model_providers.agent_hub]\nname="external-owner"\n'
        with self.assertRaisesRegex(ValueError, "已存在"):
            compile_config(collision, None, "third-party", PROVIDER, SELECTION, reconcile=True)

    def test_invalid_ownership_or_official_state_still_rejected(self):
        third, state = compile_config(BASE, None, "third-party", PROVIDER, SELECTION)
        changed = third.replace('model = "third-party-model"', 'model = "native-change"')
        for key, value in (("providerHash", "invalid"), ("managed", None), ("official", {})):
            invalid = dict(state, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                compile_config(changed, invalid, "official", reconcile=True)

    def test_reconciliation_cannot_bypass_official_auth_route_guard(self):
        third, state = compile_config(BASE, None, "third-party", PROVIDER, SELECTION)
        for suffix, prefix in (('\n[model_providers.openai]\nbase_url="https://external.example/v1"\n', ''),
                               ('', 'forced_login_method="api"\n')):
            for mode in ("official", "third-party"):
                live = prefix + third.replace('model_provider = "agent_hub"', 'model_provider = "openai"') + suffix
                with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "外部"):
                    compile_config(live, state, mode, PROVIDER, SELECTION, reconcile=True)


class CodexConnectionAuthTests(unittest.TestCase):
    def test_missing_and_null_auth_fields_inherit_but_empty_fields_clear(self):
        provider = {"apiKey": "synthetic-default", "openaiApiKey": "synthetic-legacy",
                    "apiKeyEnv": "SYNTHETIC_DEFAULT_KEY", "agents": {"codex": {}}}
        for override in ({}, {"apiKey": None, "apiKeyEnv": None}, {"apiKey": "", "apiKeyEnv": ""}):
            with self.subTest(override=override):
                provider["agents"]["codex"] = override
                conn = engine.agent_connection(provider, "codex")
                cleared = override.get("apiKey") == ""
                self.assertEqual(conn["apiKey"], "" if cleared else "synthetic-legacy")
                self.assertEqual(conn["apiKeyEnv"], "" if cleared else "SYNTHETIC_DEFAULT_KEY")

    def test_explicit_empty_auth_cannot_fall_back_to_default_during_compile(self):
        with isolated() as home:
            directory = home / ".codex"
            directory.mkdir()
            config = directory / "config.toml"
            config.write_text(BASE)
            cfg = json.loads(engine.HUB_CONFIG.read_text())
            cfg["sync"]["targets"] = {"codex": {"enabled": True, "mode": "third-party"}}
            provider = cfg["providers"]["deepseek"]
            provider.update(apiKey="synthetic-default", openaiApiKey="synthetic-legacy",
                            apiKeyEnv="SYNTHETIC_DEFAULT_KEY")
            provider["agents"]["codex"].update(apiKey="", apiKeyEnv="")
            engine.save_json(engine.HUB_CONFIG, cfg)
            with self.assertRaisesRegex(transaction.TransactionError, "apiKey"):
                server.api_preview({"targets": ["codex"]})
            self.assertEqual(config.read_text(), BASE)
            self.assertFalse((directory / ".agent-hub-state.json").exists())

    def test_explicit_empty_env_allows_agent_literal_and_missing_env_still_inherits(self):
        provider = dict(PROVIDER, apiKey="synthetic-default", apiKeyEnv="SYNTHETIC_DEFAULT_KEY",
                        agents={"codex": {"apiKey": "synthetic-agent", "apiKeyEnv": ""}})
        for clear_env in (True, False):
            if not clear_env:
                provider["agents"]["codex"].pop("apiKeyEnv")
            conn = engine.agent_connection(provider, "codex")
            normalized = dict(provider, apiKey=conn["apiKey"], openaiApiKey=conn["apiKey"],
                              apiKeyEnv=conn["apiKeyEnv"])
            rendered, _ = compile_config(BASE, None, "third-party", normalized, SELECTION)
            block = tomllib.loads(rendered)["model_providers"]["agent_hub"]
            if clear_env:
                self.assertEqual(block["experimental_bearer_token"], "synthetic-agent")
                self.assertNotIn("env_key", block)
            else:
                self.assertEqual(block["env_key"], "SYNTHETIC_DEFAULT_KEY")
                self.assertNotIn("experimental_bearer_token", block)


class CodexPreviewReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        transaction._PLANS.clear()
        self.addCleanup(transaction._PLANS.clear)
        self.root = self.home / ".codex"
        self.root.mkdir()
        self.path = self.root / "config.toml"
        self.sidecar = self.root / ".agent-hub-state.json"
        self.path.write_text(BASE)
        self.cfg = json.loads(engine.HUB_CONFIG.read_text())
        self.cfg["sync"]["targets"] = {"codex": {"enabled": True, "mode": "third-party"}}
        engine.save_json(engine.HUB_CONFIG, self.cfg)
        initial = server.api_preview({"targets": ["codex"]})
        server.api_apply({"planId": initial["planId"]})
        self.managed = self.path.read_text()
        self.path.write_text(self.managed.replace('model = "deepseek/model-1"', 'model = "native-change"'))

    def test_refreshing_automatic_preview_alone_cannot_resolve_stale_snapshot(self):
        before = (self.path.read_bytes(), self.sidecar.read_bytes())
        for _ in range(2):
            with self.assertRaisesRegex(transaction.TransactionError, "外部"):
                transaction.preview(engine.Hub(), ["codex"])
        self.assertEqual((self.path.read_bytes(), self.sidecar.read_bytes()), before)

    def test_explicit_api_preview_is_read_only_then_apply_reconciles(self):
        before = (self.path.read_bytes(), self.sidecar.read_bytes())
        plan = server.api_preview({"targets": ["codex"]})
        self.assertEqual((self.path.read_bytes(), self.sidecar.read_bytes()), before)
        self.assertIn("model", " ".join(plan["warnings"]))
        self.assertNotIn("native-change", json.dumps(plan))
        self.assertNotIn("test-not-a-real-key", json.dumps(plan))
        self.assertTrue(server.api_apply({"planId": plan["planId"]})["ok"])
        self.assertEqual(self.path.read_text(), self.managed)
        # Repaired ownership can subsequently be used by ordinary push.
        self.assertTrue(all("✓" in result for result in engine.Hub().push_all(["codex"])))

    def test_legacy_sync_and_watch_push_still_refuse_external_edits(self):
        before = (self.path.read_bytes(), self.sidecar.read_bytes())
        self.assertFalse(server._sync({"targets": ["codex"]})["ok"])
        self.assertFalse(server._sync({"targets": ["codex"]}, detect=True)["ok"])
        self.assertEqual((self.path.read_bytes(), self.sidecar.read_bytes()), before)

    def test_changes_after_reconciliation_preview_reject_apply_then_fresh_preview_works(self):
        for field in FIELDS:
            with self.subTest(field=field):
                plan = server.api_preview({"targets": ["codex"]})
                text = self.path.read_text()
                value = tomllib.loads(text)[field]
                self.path.write_text(text.replace(f'{field} = "{value}"', f'{field} = "changed-after-preview"'))
                before = (self.path.read_bytes(), self.sidecar.read_bytes())
                with self.assertRaisesRegex(transaction.TransactionError, "stale"):
                    server.api_apply({"planId": plan["planId"]})
                self.assertEqual((self.path.read_bytes(), self.sidecar.read_bytes()), before)
                fresh = server.api_preview({"targets": ["codex"]})
                self.assertTrue(server.api_apply({"planId": fresh["planId"]})["ok"])

    def test_provider_table_edit_after_preview_rejects_apply(self):
        plan = server.api_preview({"targets": ["codex"]})
        self.path.write_text(self.path.read_text().replace("api.deepseek.com", "changed.example"))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(transaction.TransactionError, "stale"):
            server.api_apply({"planId": plan["planId"]})
        self.assertEqual(self.path.read_bytes(), before)

    def test_sidecar_edit_after_preview_rejects_apply(self):
        plan = server.api_preview({"targets": ["codex"]})
        self.sidecar.write_text(self.sidecar.read_text() + "\n")
        before = (self.path.read_bytes(), self.sidecar.read_bytes())
        with self.assertRaisesRegex(transaction.TransactionError, "stale"):
            server.api_apply({"planId": plan["planId"]})
        self.assertEqual((self.path.read_bytes(), self.sidecar.read_bytes()), before)

    def test_reconciliation_never_opens_native_auth_or_named_profiles(self):
        for name in ("auth.json", "personal.config.toml"):
            (self.root / name).write_text("synthetic sentinel must never be read")
        original_open = Path.open
        def guarded(path, *args, **kwargs):
            if path.parent == self.root and path.name in ("auth.json", "personal.config.toml"):
                raise AssertionError("Native auth or profile was touched")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            plan = server.api_preview({"targets": ["codex"]})
            server.api_apply({"planId": plan["planId"]})
        for name in ("auth.json", "personal.config.toml"):
            self.assertEqual((self.root / name).read_text(), "synthetic sentinel must never be read")


if __name__ == "__main__":
    unittest.main()
