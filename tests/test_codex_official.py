"""Synthetic-only native-login-preserving Codex switching tests."""
import copy
import json
import tomllib
import unittest
from unittest.mock import patch
from pathlib import Path
from support import isolated, engine, server
from codex_config import compile_config

BASE = '''# keep comment
model = "official-model"
model_reasoning_effort = "medium"
notify = ["my-hook"]
[model_providers.custom]
name = "untouched"
base_url = "https://untouched.example/v1"
[mcp_servers.example]
command = "safe-fixture"
'''
PROVIDER = {"name": "Fixture gateway", "baseUrl": "https://fixture.example/v1", "apiKeyEnv": "FIXTURE_KEY", "codexWireApi": "responses"}
SELECTION = {"model": "third-party-model", "reasoningEffort": "high"}

class CodexRoundTripTests(unittest.TestCase):
    def test_official_third_party_a_b_official(self):
        first, state = compile_config(BASE, None, "third-party", PROVIDER, SELECTION)
        self.assertNotIn("apiKey", json.dumps(state))
        parsed = tomllib.loads(first)
        self.assertEqual(parsed["model_provider"], "agent_hub")
        self.assertFalse(parsed["model_providers"]["agent_hub"]["requires_openai_auth"])
        provider_b = dict(PROVIDER, name="B", baseUrl="https://other.example/v1")
        second, state = compile_config(first, state, "third-party", provider_b, {"model":"model-b"})
        self.assertEqual(tomllib.loads(second)["model_reasoning_effort"], "high")
        restored, state = compile_config(second, state, "official")
        self.assertEqual(tomllib.loads(restored), tomllib.loads(BASE))
        self.assertFalse(state["active"])
        self.assertIn("# keep comment", restored)
        # Official changes between cycles are captured afresh.
        updated = restored.replace('"official-model"', '"new-official-model"')
        third, state = compile_config(updated, state, "third-party", PROVIDER, SELECTION)
        final, _ = compile_config(third, state, "official")
        self.assertEqual(tomllib.loads(final)["model"], "new-official-model")

    def test_preserves_empty_parent_provider_table(self):
        for base in ('model="official"\n[model_providers]\n',
                     'model="official"\n[model_providers]\n[mcp_servers.test]\ncommand="fixture"\n'):
            third, state = compile_config(base,None,"third-party",PROVIDER,SELECTION)
            restored, _ = compile_config(third,state,"official")
            self.assertEqual(tomllib.loads(restored),tomllib.loads(base))

    def test_none_and_global_off_use_codex_none(self):
        for effort in ("none", "off"):
            output, _ = compile_config(BASE,None,"third-party",PROVIDER,dict(SELECTION,reasoningEffort=effort))
            self.assertEqual(tomllib.loads(output)["model_reasoning_effort"],"none")

    def test_absent_default_fields_restored_absent(self):
        third, state = compile_config("# default settings\n", None, "third-party", PROVIDER, SELECTION)
        official, _ = compile_config(third, state, "official")
        self.assertEqual(tomllib.loads(official), {})

    def test_official_first_use_noop(self):
        self.assertEqual(compile_config(BASE, None, "official"), (BASE,None))

    def test_owned_name_collision_never_overwrites(self):
        with self.assertRaisesRegex(ValueError,"已存在"):
            compile_config(BASE + '[model_providers.agent_hub]\nname="mine"\n', None, "third-party", PROVIDER, SELECTION)

    def test_does_not_guess_official_baseline(self):
        with self.assertRaises(ValueError):
            compile_config('model_provider="custom"\n'+BASE,None,"third-party",PROVIDER,SELECTION)

    def test_rejects_chat_and_undeclared_responses(self):
        for wire in ("chat", "unknown", None):
            provider = dict(PROVIDER)
            if wire is None: provider.pop("codexWireApi")
            else: provider["codexWireApi"] = wire
            with self.subTest(wire=wire), self.assertRaisesRegex(ValueError,"Responses"):
                compile_config(BASE,None,"third-party",provider,SELECTION)

    def test_external_owned_provider_edit_rejected(self):
        third, state = compile_config(BASE,None,"third-party",PROVIDER,SELECTION)
        with self.assertRaisesRegex(ValueError,"外部"):
            compile_config(third.replace("fixture.example","external.example"),state,"official")

    def test_external_managed_root_edits_are_rejected(self):
        third, state = compile_config(BASE,None,"third-party",PROVIDER,SELECTION)
        for changed in (third.replace('model = "third-party-model"', 'model = "manual-model"'),
                        third.replace('model_reasoning_effort = "high"', 'model_reasoning_effort = "low"'),
                        third.replace('model_reasoning_effort = "high"\n', '')):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError,"外部"):
                compile_config(changed,state,"official")
        # Unmanaged native fields remain editable during a Hub-managed cycle.
        changed = third.replace('notify = ["my-hook"]', 'notify = ["new-hook"]')
        restored, _ = compile_config(changed,state,"official")
        self.assertEqual(tomllib.loads(restored)["notify"], ["new-hook"])
        without_effort, state = compile_config(BASE,None,"third-party",PROVIDER,{"model":"third-party-model"})
        with self.assertRaisesRegex(ValueError,"外部"):
            compile_config(without_effort.replace('model_reasoning_effort = "medium"', 'model_reasoning_effort = "low"'),state,"official")

    def test_invalid_managed_root_record_is_rejected(self):
        third, state = compile_config(BASE,None,"third-party",PROVIDER,SELECTION)
        state["managed"] = None
        with self.assertRaisesRegex(ValueError,"记录无效"):
            compile_config(third,state,"official")

    def test_legacy_active_state_upgrades_on_next_third_party_sync(self):
        third, state = compile_config(BASE,None,"third-party",PROVIDER,SELECTION)
        state.pop("managed")
        updated, state = compile_config(third,state,"third-party",PROVIDER,SELECTION)
        self.assertEqual(updated,third)
        self.assertIn("managed",state)
        with self.assertRaisesRegex(ValueError,"外部"):
            compile_config(third.replace('model = "third-party-model"', 'model = "manual-model"'),state,"official")

    def test_invalid_toml_and_complex_multiline_fail_closed(self):
        for text in ('model = [', 'model = """a\nb"""\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                compile_config(text,None,"third-party",PROVIDER,SELECTION)

    def test_env_reference_preferred_and_no_token_in_state(self):
        output, state = compile_config(BASE,None,"third-party",dict(PROVIDER,openaiApiKey="synthetic-secret"),SELECTION)
        self.assertNotIn("synthetic-secret",output)
        self.assertNotIn("synthetic-secret",json.dumps(state))

    def test_official_restore_rejects_external_auth_route_overrides(self):
        third, state = compile_config(BASE,None,"third-party",PROVIDER,SELECTION)
        for changed in (third+'\n[model_providers.openai]\nbase_url="https://external.example/v1"\n',
                        'forced_login_method="api"\n'+third):
            with self.assertRaisesRegex(ValueError,"外部"):
                compile_config(changed,state,"official")

    def test_jsonc_comments_inside_strings_are_preserved(self):
        text = '{"url":"https://example.test/*literal*/", "quoted":"\\\"//safe" /* comment */}'
        parsed = json.loads(engine.strip_json_comments(text))
        self.assertEqual(parsed["url"],"https://example.test/*literal*/")
        self.assertEqual(parsed["quoted"],'"//safe')

    def test_legacy_profiles_fail_closed(self):
        with self.assertRaisesRegex(ValueError,"profile"):
            compile_config(BASE+'[profiles.fast]\nmodel="legacy"\n',None,"third-party",PROVIDER,SELECTION)

    def test_monitoring_ignores_legacy_codex_auth_file_entries(self):
        with isolated() as home:
            cfg=json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
            cfg["sync"]["targets"]={"codex":{"enabled":True,"files":[str(home/".codex"/"auth.json")]}}
            engine.save_json(server.HUB_CONFIG,cfg)
            directory=home/".codex"
            directory.mkdir()
            (directory/"auth.json").write_text("do not inspect", encoding="utf-8")
            original=Path.open
            def guarded(path,*args,**kwargs):
                if path == directory/"auth.json":
                    raise AssertionError("Monitoring read native auth")
                return original(path,*args,**kwargs)
            with patch.object(Path,"open",guarded):
                h=engine.Hub()
                h.record_hashes()
                h.changed_files()
            self.assertNotIn(str(directory/"auth.json"),h.state["hashes"])

    def test_cli_push_reports_failure_and_does_not_mark_synced(self):
        from types import SimpleNamespace
        with isolated():
            h=engine.Hub()
            with patch.object(h,"push_all",return_value=["  ✗ codex: invalid fixture"]), patch.object(h,"save_state") as save:
                self.assertEqual(engine.cmd_push(h,SimpleNamespace(targets=["codex"])),1)
                save.assert_not_called()

    def test_direct_adapter_never_opens_native_auth_or_named_profiles(self):
        with isolated() as home:
            directory=home/".codex"
            directory.mkdir()
            (directory/"config.toml").write_text(BASE, encoding="utf-8")
            (directory/"auth.json").write_text("fake-auth-must-not-be-read", encoding="utf-8")
            (directory/"personal.config.toml").write_text("named profile untouched", encoding="utf-8")
            cfg=json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
            cfg["sync"]["targets"]={"codex":{"enabled":True,"mode":"third-party"}}
            cfg["providers"]["deepseek"]["codexWireApi"]="responses"
            engine.save_json(server.HUB_CONFIG,cfg)
            original_open=Path.open
            def guarded(path,*args,**kwargs):
                if path.name in ("auth.json","personal.config.toml"):
                    raise AssertionError("Native auth or named profile was touched")
                return original_open(path,*args,**kwargs)
            with patch.object(Path,"open",guarded):
                engine.Hub().push_codex()
                cfg["sync"]["targets"]["codex"]["mode"]="official"
                engine.save_json(server.HUB_CONFIG,cfg)
                engine.Hub().push_codex()
            self.assertEqual(tomllib.loads((directory/"config.toml").read_text(encoding="utf-8")),tomllib.loads(BASE))
            self.assertEqual((directory/"auth.json").read_text(encoding="utf-8"),"fake-auth-must-not-be-read")

if __name__ == "__main__": unittest.main()
