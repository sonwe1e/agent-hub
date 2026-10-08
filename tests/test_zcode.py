"""ZCode adapter and modelSync integration tests."""
from __future__ import annotations

import copy
import json
import unittest

from support import isolated, engine, server


FIREWORK_ZCODE_ID = "a4d4d39c-5404-48db-b49a-e46d34df4525"


def zcode_document() -> dict:
    return {
        "schemaVersion": 1,
        "config": {
            "providerConfigRules": {"providerRules": [
                {"providerId": FIREWORK_ZCODE_ID, "providerName": "火山", "config": {
                    "group": "standard-personal",
                    "access": {"type": "api-key", "apiKey": "old-key"},
                    "api": {"type": "openai-responses", "baseUrl": "https://old.example/v1"},
                    "personalModelIds": ["old-managed"],
                    "modelOrder": ["old-managed"],
                }},
                {"providerId": "manual-provider", "providerName": "Manual", "config": {
                    "personalModelIds": ["manual-model"],
                    "modelOrder": ["manual-model"],
                }},
            ]},
            "modelConfigRules": {"providerModelRules": [
                {"modelId": "old-managed", "providerId": FIREWORK_ZCODE_ID,
                 "config": {"properties": {"contextWindow": 1}}},
                {"modelId": "built-in-model", "providerId": FIREWORK_ZCODE_ID,
                 "config": {"properties": {"contextWindow": 2}}},
                {"modelId": "manual-model", "providerId": "manual-provider",
                 "config": {"properties": {"contextWindow": 3}}},
            ], "manualProviderModelRules": []},
            "providerOrder": [FIREWORK_ZCODE_ID, "manual-provider"],
            "defaultModelSelection": {
                "providerId": "manual-provider", "modelId": "manual-model",
                "options": {"reasoningLevel": "high"},
            },
        },
    }


class ZCodeTests(unittest.TestCase):
    def setUp(self):
        context = isolated()
        self.home = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.path = self.home / ".zcode" / "v2" / "provider_config.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(zcode_document()), encoding="utf-8")
        # 本类测试关注目录与连接语义：固定 third-party，使 modelSync 解析出的
        # 默认选择照旧写入（official/third-party 门控见 ZCodeModeTests）。
        cfg = self.config()
        cfg["sync"]["targets"]["zcode"]["mode"] = "third-party"
        self.save(cfg)
        self.state_path = self.home / ".zcode" / "v2" / ".agent-hub-state.json"

    def config(self) -> dict:
        return json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))

    def save(self, cfg: dict) -> None:
        engine.save_json(server.HUB_CONFIG, cfg)

    def read_zcode(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_zcode_connection_and_model_tier_use_agent_catalog(self):
        cfg = self.config()
        provider = cfg["providers"]["openrouter"]
        provider["agents"] = {
            "zcode": {"models": [{"id": "zcode-only", "name": "ZCode only"}]},
        }
        cfg["modelSync"] = {"overrides": {"zcode": {
            "provider": "openrouter", "model": "zcode-only",
        }}}
        self.assertEqual([m["id"] for m in engine.agent_connection(provider, "zcode")["models"]],
                         ["zcode-only"])
        self.assertEqual([m["id"] for m in engine.agent_model_options(cfg, "zcode")["openrouter"]],
                         ["zcode-only"])
        tier = engine.agent_model_tiers(cfg, "zcode")["agent"]
        self.assertEqual((tier["provider"], tier["model"]), ("openrouter", "zcode-only"))
        self.assertTrue(tier["known"])

    def test_push_writes_connection_models_and_default_selection(self):
        cfg = self.config()
        cfg["providers"]["firework"]["agents"] = {
            "zcode": {"models": [
                {"id": "zcode-only", "name": "ZCode only", "contextWindow": 256000,
                 "input": ["text", "image"]},
            ]},
        }
        cfg["modelSync"] = {"overrides": {"zcode": {
            "provider": "firework", "model": "zcode-only", "reasoningEffort": "high",
        }}}
        self.save(cfg)
        engine.Hub().push_zcode()
        data = self.read_zcode()["config"]
        rule = next(r for r in data["providerConfigRules"]["providerRules"]
                    if r["providerId"] == FIREWORK_ZCODE_ID)
        self.assertEqual(rule["config"]["personalModelIds"], ["zcode-only"])
        self.assertEqual(rule["config"]["modelOrder"], ["zcode-only"])
        self.assertEqual(rule["config"]["api"]["type"], "openai-responses")
        self.assertEqual(data["defaultModelSelection"], {
            "providerId": FIREWORK_ZCODE_ID, "modelId": "zcode-only",
            "options": {"reasoningLevel": "high"},
        })
        model_rule = next(r for r in data["modelConfigRules"]["providerModelRules"]
                          if r["providerId"] == FIREWORK_ZCODE_ID and r["modelId"] == "zcode-only")
        self.assertEqual(model_rule["config"]["properties"]["contextWindow"], 256000)
        self.assertTrue(model_rule["config"]["properties"]["inputFormat"]["supportsImage"])
        # 旧的 Hub 个人模型规则收回，但非 Hub 内置规则仍保留。
        self.assertFalse(any(r["providerId"] == FIREWORK_ZCODE_ID and r["modelId"] == "old-managed"
                             for r in data["modelConfigRules"]["providerModelRules"]))
        self.assertTrue(any(r["providerId"] == FIREWORK_ZCODE_ID and r["modelId"] == "built-in-model"
                            for r in data["modelConfigRules"]["providerModelRules"]))

    def test_top_level_models_are_not_used_for_zcode_override(self):
        cfg = self.config()
        cfg["providers"]["openrouter"]["models"] = [{"id": "top-level-only"}]
        cfg["providers"]["openrouter"]["agents"] = {
            "zcode": {"models": [{"id": "zcode-only"}]},
        }
        cfg["modelSync"] = {"overrides": {"zcode": {
            "provider": "openrouter", "model": "zcode-only",
        }}}
        self.save(cfg)
        engine.Hub().push_zcode()
        rule = next(r for r in self.read_zcode()["config"]["providerConfigRules"]["providerRules"]
                    if r["providerId"] == "openrouter")
        self.assertEqual(rule["config"]["personalModelIds"], ["zcode-only"])
        self.assertEqual(self.read_zcode()["config"]["defaultModelSelection"], {
            "providerId": "openrouter", "modelId": "zcode-only",
        })

    def test_no_modelsync_preserves_existing_zcode_selection(self):
        cfg = self.config()
        cfg.pop("modelSync", None)
        self.save(cfg)
        before = copy.deepcopy(self.read_zcode()["config"]["defaultModelSelection"])
        engine.Hub().push_zcode()
        self.assertEqual(self.read_zcode()["config"]["defaultModelSelection"], before)

    def test_unknown_zcode_model_is_rejected_before_write(self):
        cfg = self.config()
        cfg["modelSync"] = {"overrides": {"zcode": {
            "provider": "firework", "model": "not-in-zcode-library",
        }}}
        self.save(cfg)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "不在 firework 的 zcode 模型库"):
            engine.Hub().push_zcode()
        self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_protocol_overrides_native_api_and_uses_zcode_credentials(self):
        for protocol, native_protocol in (
            ("openai-completions", "openai-chat-completions"),
            ("openai-chat-completions", "openai-chat-completions"),
            ("openai-responses", "openai-responses"),
            ("anthropic-messages", "anthropic-messages"),
        ):
            with self.subTest(protocol=protocol):
                # 每轮都回到未接管状态：原生文档与托管状态文件一起重置
                self.path.write_text(json.dumps(zcode_document()), encoding="utf-8")
                self.state_path.unlink(missing_ok=True)
                server.api_update_provider("firework", {"agents": {"zcode": {
                    "baseUrl": "https://zcode.example/api",
                    "apiKey": "zcode-test-key",
                    "api": protocol,
                    "models": [{"id": "zcode-only"}],
                }}})
                engine.Hub().push_zcode()
                rule = next(r for r in self.read_zcode()["config"]["providerConfigRules"]["providerRules"]
                            if r["providerId"] == FIREWORK_ZCODE_ID)
                self.assertEqual(rule["config"]["api"], {
                    "type": native_protocol, "baseUrl": "https://zcode.example/api",
                })
                self.assertEqual(rule["config"]["access"]["apiKey"], "zcode-test-key")
                self.assertEqual(rule["config"]["personalModelIds"], ["zcode-only"])

        before = server.HUB_CONFIG.read_bytes()
        with self.assertRaisesRegex(ValueError, "agents.zcode.api"):
            server.api_update_provider("firework", {"agents": {"zcode": {"api": "unsupported"}}})
        self.assertEqual(server.HUB_CONFIG.read_bytes(), before)

    def test_existing_native_provider_id_is_reused_in_default_selection(self):
        data = self.read_zcode()
        native_id = "user-rekeyed-volcano"
        data["config"]["providerConfigRules"]["providerRules"][0]["providerId"] = native_id
        data["config"]["providerOrder"][0] = native_id
        for rule in data["config"]["modelConfigRules"]["providerModelRules"]:
            if rule["providerId"] == FIREWORK_ZCODE_ID:
                rule["providerId"] = native_id
        self.path.write_text(json.dumps(data), encoding="utf-8")
        cfg = self.config()
        model = cfg["providers"]["firework"]["models"][0]["id"]
        cfg["modelSync"] = {"overrides": {"zcode": {"provider": "firework", "model": model}}}
        self.save(cfg)

        engine.Hub().push_zcode()
        data = self.read_zcode()["config"]
        volcano_rules = [r for r in data["providerConfigRules"]["providerRules"]
                         if r["providerName"] == "火山"]
        self.assertEqual(len(volcano_rules), 1)
        self.assertEqual(volcano_rules[0]["providerId"], native_id)
        self.assertEqual(data["defaultModelSelection"], {"providerId": native_id, "modelId": model})
        self.assertNotIn(FIREWORK_ZCODE_ID, data["providerOrder"])
        self.assertFalse(any(r["providerId"] == native_id and r["modelId"] == "old-managed"
                             for r in data["modelConfigRules"]["providerModelRules"]))

    def test_explicit_empty_zcode_catalog_removes_previous_personal_models(self):
        cfg = self.config()
        cfg["providers"]["firework"]["agents"] = {"zcode": {"models": []}}
        cfg.pop("modelSync", None)
        self.save(cfg)

        engine.Hub().push_zcode()
        data = self.read_zcode()["config"]
        rule = next(r for r in data["providerConfigRules"]["providerRules"]
                    if r["providerId"] == FIREWORK_ZCODE_ID)
        self.assertEqual(rule["config"]["personalModelIds"], [])
        self.assertEqual(rule["config"]["modelOrder"], [])
        volcano_models = [r["modelId"] for r in data["modelConfigRules"]["providerModelRules"]
                          if r["providerId"] == FIREWORK_ZCODE_ID]
        self.assertEqual(volcano_models, ["built-in-model"])
        self.assertEqual(data["defaultModelSelection"], zcode_document()["config"]["defaultModelSelection"])

    def test_models_sync_api_exposes_and_accepts_zcode(self):
        cfg = self.config()
        cfg["providers"]["openrouter"]["agents"] = {
            "zcode": {"models": [{"id": "zcode-only"}]},
        }
        self.save(cfg)
        result = server.api_modelsync()
        self.assertIn("zcode", result["resolved"])
        self.assertEqual([m["id"] for m in result["optionsByTarget"]["zcode"]["openrouter"]],
                         ["zcode-only"])
        result = server.api_update_modelsync({"modelSync": {"overrides": {
            "zcode": {"provider": "openrouter", "model": "zcode-only"},
        }}})
        self.assertEqual(result["modelSync"]["overrides"]["zcode"]["model"], "zcode-only")

    def test_zcode_only_provider_needs_no_default_endpoint_or_key(self):
        body = {"id": "zcode-plan", "name": "ZCode Plan", "baseUrl": "", "apiKey": "",
                "targets": ["zcode"], "agents": {"zcode": {
                    "baseUrl": "https://zcode.example/api", "apiKey": "zcode-test-key",
                    "api": "anthropic-messages", "models": [{"id": "zcode-only"}],
                }}}
        server.api_add_provider(body)
        provider = self.config()["providers"]["zcode-plan"]
        self.assertEqual(provider["baseUrl"], "")
        self.assertEqual(provider["apiKey"], "")
        self.assertEqual(engine.agent_connection(provider, "zcode")["apiKey"], "zcode-test-key")
        self.assertIn("zcode-plan", engine.Hub().providers_for("zcode"))
        self.assertNotIn("zcode-plan", engine.Hub().providers_for("pi"))

        before = server.HUB_CONFIG.read_bytes()
        with self.assertRaisesRegex(ValueError, "baseUrl"):
            server.api_update_provider("zcode-plan", {"agents": {"zcode": {"apiKey": "zcode-test-key"}}})
        self.assertEqual(server.HUB_CONFIG.read_bytes(), before)

    def test_first_edit_of_inherited_zcode_models_keeps_default_catalog_unchanged(self):
        cfg = self.config()
        cfg["providers"]["openrouter"]["agents"]["zcode"] = {"api": "openai-responses"}
        default_models = copy.deepcopy(cfg["providers"]["openrouter"]["models"])
        for operation in ("add", "update"):
            with self.subTest(operation=operation):
                self.save(cfg)
                if operation == "add":
                    server.api_add_model("openrouter", {"id": "zcode-only", "agent": "zcode"})
                else:
                    server.api_update_model("openrouter", default_models[0]["id"], {
                        "name": "ZCode-specific name", "agent": "zcode",
                    })
                provider = self.config()["providers"]["openrouter"]
                self.assertEqual(provider["models"], default_models)
                own_models = provider["agents"]["zcode"]["models"]
                if operation == "add":
                    self.assertEqual(own_models[:-1], default_models)
                    self.assertEqual(own_models[-1]["id"], "zcode-only")
                else:
                    self.assertEqual(own_models[0]["name"], "ZCode-specific name")
                    self.assertEqual(own_models[1:], default_models[1:])


class ZCodeModeTests(unittest.TestCase):
    """official / third-party 双模式：快照、恢复、外部编辑 fail-closed 与 reconcile。

    不预设 mode（缺省按 official 处理），逐测试显式切换。
    """

    def setUp(self):
        context = isolated()
        self.home = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.path = self.home / ".zcode" / "v2" / "provider_config.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(zcode_document()), encoding="utf-8")
        self.state_path = self.home / ".zcode" / "v2" / ".agent-hub-state.json"

    def config(self) -> dict:
        return json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))

    def save(self, cfg: dict) -> None:
        engine.save_json(server.HUB_CONFIG, cfg)

    def read_zcode(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def read_state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def set_mode(self, mode: str) -> None:
        cfg = self.config()
        cfg["sync"]["targets"]["zcode"]["mode"] = mode
        self.save(cfg)

    def takeover(self) -> dict:
        """切到 third-party 并同步一次，返回 Hub 写入的默认选择。"""
        self.set_mode("third-party")
        engine.Hub().push_zcode()
        return self.read_zcode()["config"]["defaultModelSelection"]

    def test_absent_mode_defaults_to_official_and_preserves_selection(self):
        original = copy.deepcopy(zcode_document()["config"]["defaultModelSelection"])
        engine.Hub().push_zcode()
        self.assertEqual(self.read_zcode()["config"]["defaultModelSelection"], original)
        self.assertFalse(self.state_path.exists())

    def test_official_mode_still_maintains_hub_catalog(self):
        self.set_mode("official")
        engine.Hub().push_zcode()
        rule = next(r for r in self.read_zcode()["config"]["providerConfigRules"]["providerRules"]
                    if r["providerId"] == FIREWORK_ZCODE_ID)
        self.assertEqual(rule["config"]["access"]["apiKey"], "test-not-a-real-key")
        self.assertEqual(rule["config"]["personalModelIds"],
                         [f"firework/model-{i + 1}" for i in range(7)])
        self.assertFalse(self.state_path.exists())

    def test_third_party_takeover_snapshots_selection_and_writes_hub_choice(self):
        chosen = self.takeover()
        self.assertEqual(chosen, {"providerId": "deepseek", "modelId": "deepseek/model-1",
                                  "options": {"reasoningLevel": "medium"}})
        state = self.read_state()
        self.assertTrue(state["active"])
        self.assertEqual(state["official"], {
            "present": True,
            "value": copy.deepcopy(zcode_document()["config"]["defaultModelSelection"]),
        })
        self.assertEqual(state["selectionHash"], engine.fingerprint(chosen))

    def test_takeover_without_selection_snapshots_absent_and_official_removes_key(self):
        data = self.read_zcode()
        data["config"].pop("defaultModelSelection")
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.takeover()
        self.assertEqual(self.read_state()["official"], {"present": False, "value": None})
        self.set_mode("official")
        engine.Hub().push_zcode()
        self.assertNotIn("defaultModelSelection", self.read_zcode()["config"])
        self.assertEqual(self.read_state(), {"version": 1, "active": False})

    def test_third_party_repeat_sync_is_stable(self):
        self.takeover()
        document = self.path.read_bytes()
        state = self.state_path.read_bytes()
        engine.Hub().push_zcode()
        self.assertEqual(self.path.read_bytes(), document)
        self.assertEqual(self.state_path.read_bytes(), state)

    def test_external_selection_change_fails_closed_then_reconciles(self):
        self.takeover()
        data = self.read_zcode()
        data["config"]["defaultModelSelection"] = {"providerId": "manual-provider",
                                                   "modelId": "manual-model"}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "外部更改"):
            engine.Hub().push_zcode()
        self.assertEqual(self.path.read_bytes(), before)
        warnings: list[str] = []
        engine.Hub().push_zcode(reconcile=True, warnings=warnings)
        self.assertTrue(any("ZCode" in w for w in warnings))
        self.assertEqual(self.read_zcode()["config"]["defaultModelSelection"], {
            "providerId": "deepseek", "modelId": "deepseek/model-1",
            "options": {"reasoningLevel": "medium"},
        })

    def test_official_restores_snapshot_and_keeps_hub_catalog(self):
        self.takeover()
        self.set_mode("official")
        engine.Hub().push_zcode()
        data = self.read_zcode()["config"]
        self.assertEqual(data["defaultModelSelection"],
                         zcode_document()["config"]["defaultModelSelection"])
        self.assertEqual(self.read_state(), {"version": 1, "active": False})
        rule = next(r for r in data["providerConfigRules"]["providerRules"]
                    if r["providerId"] == FIREWORK_ZCODE_ID)
        self.assertEqual(rule["config"]["access"]["apiKey"], "test-not-a-real-key")

    def test_official_accepts_external_switch_as_new_baseline(self):
        self.takeover()
        manual = {"providerId": "manual-provider", "modelId": "manual-model"}
        data = self.read_zcode()
        data["config"]["defaultModelSelection"] = manual
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.set_mode("official")
        warnings: list[str] = []
        engine.Hub().push_zcode(warnings=warnings)
        # 用户已自行改选：保留当前选择，不复活旧快照
        self.assertEqual(self.read_zcode()["config"]["defaultModelSelection"], manual)
        self.assertEqual(self.read_state(), {"version": 1, "active": False})
        self.assertTrue(any("外部更改" in w for w in warnings))

    def test_invalid_mode_is_rejected(self):
        self.set_mode("bogus")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "ZCode mode"):
            engine.Hub().push_zcode()
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_state_is_rejected(self):
        self.takeover()
        state = self.read_state()
        state["official"] = "garbage"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "托管记录无效"):
            engine.Hub().push_zcode()
        self.assertEqual(self.path.read_bytes(), before)

    def test_unrecoverable_state_version_is_rejected(self):
        self.state_path.write_text(json.dumps({"version": 2, "active": True}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "恢复记录无效"):
            engine.Hub().push_zcode()


if __name__ == "__main__":
    unittest.main()
