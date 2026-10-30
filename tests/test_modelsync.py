# -*- coding: utf-8 -*-
"""多 agent 模型同步（providers.json → modelSync）的引擎回归测试。

覆盖三件事：
1. modelSync 的解析优先级（显式档位 > 旧 claudeModels > 平台覆盖 > provider.agentModel > 全局 > 模型库首项）；
2. 各客户端适配器把解析结果写进各自的真实位置（pi settings.json、dsh cordis.patch.yml + credentials.yaml）；
3. 没有配置 modelSync 时，任何客户端现有模型选择都不许被动过。
"""
from __future__ import annotations

import json
import unittest

from support import isolated, engine, server


def entry(provider: str = "deepseek", model: str = "deepseek/model-1", effort: str | None = None) -> dict:
    return {"provider": provider, "model": model, "reasoningEffort": effort}


def config_without_modelsync() -> dict:
    """fixture 自带 modelSync.default，这几条用例要验证「没配置时不写客户端」。"""
    cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
    cfg.pop("modelSync", None)
    return cfg


class TierResolutionTests(unittest.TestCase):
    """纯解析层：不写任何文件。"""

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.cfg = config_without_modelsync()

    def tiers(self, target: str) -> dict:
        return engine.agent_model_tiers(self.cfg, target)

    def test_global_default_resolves_for_pi_and_dsh(self):
        self.cfg["modelSync"] = {"default": entry("openrouter", "openrouter/model-4", "high")}
        for target in ("pi", "dsh", "dsh-desktop"):
            with self.subTest(target=target):
                resolved = engine.resolve_agent_model(self.cfg, target)
                self.assertEqual(resolved["provider"], "openrouter")
                self.assertEqual(resolved["model"], "openrouter/model-4")
                self.assertEqual(resolved["reasoningEffort"], "high")
                self.assertEqual(resolved["tier"], "agent")

    def test_global_default_resolves_for_claude_when_provider_has_anthropic_endpoint(self):
        # fixture 里只有 deepseek 带 anthropicBaseUrl，所以全局选择用它
        self.cfg["modelSync"] = {"default": entry("deepseek", "deepseek/model-4", "high")}
        resolved = engine.resolve_agent_model(self.cfg, "claude")
        self.assertEqual(resolved["provider"], "deepseek")
        self.assertEqual(resolved["model"], "deepseek/model-4")
        self.assertEqual(resolved["reasoningEffort"], "high")
        self.assertEqual(resolved["tier"], "model")

    def test_claude_only_default_does_not_resolve_for_pi_or_dsh(self):
        self.cfg["providers"]["claude-only"] = {
            "name": "Claude only", "baseUrl": "", "apiKey": "sk-test",
            "anthropicBaseUrl": "https://gateway.example/anthropic",
            "targets": ["claude"], "enabled": True,
            "models": [{"id": "claude-model"}],
        }
        self.cfg["modelSync"] = {"default": entry("claude-only", "claude-model")}
        for target in ("pi", "dsh", "dsh-desktop", "codex"):
            with self.subTest(target=target):
                self.assertIsNone(engine.resolve_agent_model(self.cfg, target))
        self.assertEqual(engine.resolve_agent_model(self.cfg, "claude")["provider"],
                         "claude-only")

    def test_claude_provider_without_connection_is_skipped(self):
        """modelSync 指向的供应商没有 Claude 连接时，Claude Code 不被写入。"""
        self.cfg["providers"]["claude-only"] = {
            "name": "Claude only", "baseUrl": "", "apiKey": "sk-test",
            "anthropicBaseUrl": "https://gateway.example/anthropic",
            "targets": ["claude"], "enabled": True,
            "models": [{"id": "claude-model"}],
        }
        self.cfg["modelSync"] = {"default": entry("stepfun", "stepfun/model-1")}
        self.assertIsNone(engine.resolve_agent_model(self.cfg, "claude"))
        self.assertEqual(engine.agent_model_tiers(self.cfg, "claude"), {})

    def test_global_default_does_not_move_claude_secondary_tiers(self):
        """一个全局设置不该顺手改掉 Claude Code 的 opus / haiku / subagent 档位。"""
        # 全局选的就是当前 Claude 供应商（fixture 的 activeProvider = deepseek）
        self.cfg["modelSync"] = {"default": entry("deepseek", "deepseek/model-4")}
        tiers = self.tiers("claude")
        self.assertEqual(set(tiers), {"model"})
        self.assertEqual(tiers["model"]["model"], "deepseek/model-4")
        self.assertNotIn("haiku", tiers)
        self.assertNotIn("subagent", tiers)
        self.assertNotIn("fable", tiers)

    def test_explicit_tiers_are_written_and_unset_tiers_stay_absent(self):
        self.cfg["modelSync"] = {
            "default": entry(),
            "overrides": {"claude": {
                "provider": "deepseek", "model": "deepseek/model-1",
                "tiers": {"opus": entry("deepseek", "deepseek/model-3")}}}}
        tiers = self.tiers("claude")
        self.assertEqual(tiers["opus"]["provider"], "deepseek")
        self.assertEqual(tiers["opus"]["model"], "deepseek/model-3")
        # 主模型档位跟随平台覆盖
        self.assertEqual(tiers["model"]["model"], "deepseek/model-1")
        # 未显式指定的别名档位不写：不替用户改 Claude Code 的配置
        self.assertNotIn("haiku", tiers)
        self.assertNotIn("subagent", tiers)
        self.assertNotIn("fable", tiers)

    def test_tier_provider_must_have_a_claude_connection(self):
        """档位指向的供应商没有 Claude 连接时，该档位被忽略而不是写坏配置。"""
        self.cfg["providers"]["pi-only"] = {
            "name": "Pi only", "baseUrl": "https://gateway.example/v1",
            "apiKey": "sk-test", "targets": ["pi"], "enabled": True,
            "models": [{"id": "wrong-opus"}],
        }
        self.cfg["modelSync"] = {"overrides": {"claude": {
            "provider": "deepseek", "model": "deepseek/model-1",
            "tiers": {"opus": entry("pi-only", "wrong-opus")},
        }}}
        tiers = self.tiers("claude")
        self.assertNotIn("opus", tiers)

    def test_legacy_claude_models_are_no_longer_read(self):
        """旧字段 claudeModels 不再参与解析（迁移命令负责搬进 modelSync）。"""
        self.cfg["providers"]["deepseek"]["claudeModels"] = {"model": "legacy-model",
                                                            "haiku": "legacy-haiku"}
        self.cfg["modelSync"] = {"overrides": {"claude": {
            "provider": "deepseek", "model": "deepseek/model-1"}}}
        tiers = self.tiers("claude")
        self.assertEqual(set(tiers), {"model"})
        self.assertEqual(tiers["model"]["model"], "deepseek/model-1")

    def test_per_agent_models_catalogs_are_independent(self):
        """同一供应商给不同 Agent 配不同 key 时，模型库按连接各自判断。"""
        self.cfg["providers"]["tokenfree"] = {
            "name": "TokenFree", "baseUrl": "https://gateway.example/v1",
            "apiKey": "sk-openai", "api": "openai-completions", "enabled": True,
            "models": [{"id": "gpt-6-astra"}],
            "agents": {
                "claude": {"baseUrl": "https://gateway.example/v1",
                           "api": "anthropic-messages", "apiKey": "sk-anthropic",
                           "models": [{"id": "claude-opus-5-5"}]},
                "codex": {"baseUrl": "https://gateway.example/v1",
                          "api": "openai-responses", "apiKey": "sk-codex",
                          "models": [{"id": "gpt-6-astra"}]},
            },
        }
        # 全局主模型在默认连接上，pi / codex 认它
        self.cfg["modelSync"] = {"default": entry("tokenfree", "gpt-6-astra")}
        self.assertTrue(engine.resolve_agent_model(self.cfg, "pi")["known"])
        self.assertTrue(engine.resolve_agent_model(self.cfg, "codex")["known"])
        # Claude 连接的模型库里没有 gpt-6-astra：跟随全局时会标记为手填值
        claude = engine.resolve_agent_model(self.cfg, "claude")
        self.assertEqual((claude["provider"], claude["model"]), ("tokenfree", "gpt-6-astra"))
        self.assertFalse(claude["known"])
        # 只有 Anthropic 连接里声明的模型对 Claude 是「已知」的
        self.cfg["modelSync"] = {"overrides": {"claude": entry("tokenfree", "claude-opus-5-5")}}
        self.assertTrue(engine.resolve_agent_model(self.cfg, "claude")["known"])

    def test_provider_agent_model_is_the_fallback_before_model_list(self):
        self.cfg["providers"]["deepseek"]["agentModel"] = "deepseek/model-6"
        self.cfg["modelSync"] = {"overrides": {"dsh": entry("deepseek", "")}}
        # 空 model 的覆盖无效，回退到 provider.agentModel
        self.assertEqual(engine.resolve_agent_model(self.cfg, "dsh")["model"], "deepseek/model-6")
        # 没有 agentModel 时用模型库首项
        self.cfg["providers"]["deepseek"].pop("agentModel")
        self.assertEqual(engine.resolve_agent_model(self.cfg, "dsh")["model"], "deepseek/model-1")

    def test_unconfigured_target_has_no_resolution(self):
        self.assertEqual(engine.agent_model_tiers(self.cfg, "claude"), {})
        self.assertIsNone(engine.resolve_agent_model(self.cfg, "pi"))
        self.assertEqual(engine.agent_model_tiers(self.cfg, "codex"), {})

    def test_dsh_desktop_is_an_alias_of_dsh(self):
        self.cfg["modelSync"] = {"default": entry("openrouter", "openrouter/model-2")}
        self.assertEqual(engine.agent_model_tiers(self.cfg, "dsh-desktop"),
                         engine.agent_model_tiers(self.cfg, "dsh"))

    def test_dsh_subagent_tier_follows_the_agent_tier(self):
        self.cfg["modelSync"] = {"default": entry("stepfun", "stepfun/model-1", "low")}
        tiers = self.tiers("dsh")
        self.assertEqual(tiers["agent"]["model"], "stepfun/model-1")
        self.assertEqual(tiers["subagent"]["model"], "stepfun/model-1")
        self.assertEqual(tiers["subagent"]["reasoningEffort"], "low")

    def test_unknown_model_is_flagged_not_known(self):
        self.cfg["modelSync"] = {"default": entry("deepseek", "hand-typed-model")}
        self.assertFalse(engine.resolve_agent_model(self.cfg, "pi")["known"])

    def test_public_tier_view_hides_historical_tiers(self):
        self.cfg["modelSync"] = {"overrides": {"claude": {
            "provider": "deepseek", "model": "deepseek/model-1",
            "tiers": {"fable": entry("deepseek", "deepseek/model-2"),
                      "opus": entry("deepseek", "deepseek/model-3")}}}}
        internal = engine.agent_model_tiers(self.cfg, "claude")
        public = engine.agent_model_tiers_public(self.cfg, "claude")
        self.assertIn("fable", internal)      # 适配器要靠它写/收回旧别名
        self.assertNotIn("fable", public)     # 界面不把它当目标档位


class PiModelSyncTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.pi_dir = self.home / ".pi" / "agent"
        self.pi_dir.mkdir(parents=True)
        (self.pi_dir / "settings.json").write_text(json.dumps({
            "defaultProvider": "openai", "defaultModel": "gpt-6.1-sol",
            "defaultThinkingLevel": "xhigh", "theme": "dark",
        }), encoding="utf-8")

    def settings(self) -> dict:
        return json.loads((self.pi_dir / "settings.json").read_text(encoding="utf-8"))

    def test_main_model_is_pinned_from_modelsync(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"default": entry("stepfun", "stepfun/model-2", "low")}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_pi()
        settings = self.settings()
        self.assertEqual(settings["defaultProvider"], "stepfun")
        self.assertEqual(settings["defaultModel"], "stepfun/model-2")
        self.assertEqual(settings["defaultThinkingLevel"], "low")
        # 与模型无关的键原样保留
        self.assertEqual(settings["theme"], "dark")

    def test_effort_is_untouched_when_not_configured(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"default": entry("stepfun", "stepfun/model-2", None)}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_pi()
        self.assertEqual(self.settings()["defaultThinkingLevel"], "xhigh")

    def test_without_modelsync_the_user_choice_survives(self):
        engine.save_json(server.HUB_CONFIG, config_without_modelsync())
        engine.Hub().push_pi()
        settings = self.settings()
        self.assertEqual(settings["defaultProvider"], "openai")
        self.assertEqual(settings["defaultModel"], "gpt-6.1-sol")

    def test_provider_not_available_to_pi_is_not_pinned(self):
        """targets 限定 claude 的供应商不能被写成 pi 的默认 provider。"""
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["tokenfree"] = {
            "name": "TokenFree", "baseUrl": "",
            "anthropicBaseUrl": "https://api.tokenfree.shop", "apiKey": "sk-test",
            "api": "openai-completions", "enabled": True, "targets": ["claude"],
            "models": [{"id": "claude-opus-5-5", "name": "opus"}],
        }
        cfg["modelSync"] = {"default": entry("tokenfree", "claude-opus-5-5")}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_pi()
        settings = self.settings()
        self.assertEqual(settings["defaultProvider"], "openai")
        self.assertEqual(settings["defaultModel"], "gpt-6.1-sol")


def dsh_fixture(home) -> None:
    """搭一套最小 dsh 配置：凭据 + 桌面 patch（含无关段落与 llm-pi-ai 路由段）。"""
    dsh = home / ".dsh"
    (dsh / "profiles" / "desktop").mkdir(parents=True)
    (dsh / ".credentials.yaml").write_text(
        "version: 1\nrefs:\n  KEEP_ME: keep-value\n", encoding="utf-8")
    (dsh / "profiles" / "desktop" / "cordis.patch.yml").write_text(
        "- id: locale\n"
        "  name: \"@deepseek-ai/dsh-client-locale\"\n"
        "  config:\n"
        "    preference: zh\n"
        "- id: llm-pi-ai\n"
        "  name: \"@deepseek-ai/dsh-llm-pi-ai\"\n"
        "  config:\n"
        "    providers:\n"
        "      deepseek:\n"
        "        displayName: DeepSeek (API key)\n"
        "        apiKeyEnv: MCODE_DEEPSEEK_API_KEY\n"
        "        api: openai-completions\n"
        "        baseURL: \"https://api.deepseek.com\"\n"
        "        models:\n"
        "          - id: deepseek/model-1\n"
        "            name: DeepSeek Model 1\n"
        "- id: agent-default-model\n"
        "  name: \"@deepseek-ai/dsh-agent-default-model\"\n"
        "  config:\n"
        "    provider: deepseek-account\n"
        "    model: deepseek-flash\n"
        "    reasoningEffort: max\n",
        encoding="utf-8")


class DshModelSyncTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        dsh_fixture(self.home)
        self.patch = self.home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"

    def blocks(self) -> list:
        return engine.parse_yaml(self.patch.read_text(encoding="utf-8"))

    def find(self, block_id: str) -> dict | None:
        return next((b for b in self.blocks()
                     if isinstance(b, dict) and b.get("id") == block_id), None)

    def test_agent_and_subagent_model_are_written(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"default": entry("stepfun", "stepfun/model-2", "high")}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_dsh()
        block = self.find("agent-default-model")
        self.assertEqual(block["config"]["provider"], "stepfun")
        self.assertEqual(block["config"]["model"], "stepfun/model-2")
        self.assertEqual(block["config"]["reasoningEffort"], "high")
        settings = self.find("subagent-model-selection-settings")
        self.assertEqual(settings["name"], "@deepseek-ai/dsh-tool-subagent/model-selection-settings")
        self.assertTrue(settings["config"]["enabled"])
        self.assertEqual(settings["config"]["allowedModels"],
                         [{"provider": "stepfun", "model": "stepfun/model-2"}])

    def test_existing_model_selection_is_kept_without_modelsync(self):
        engine.save_json(server.HUB_CONFIG, config_without_modelsync())
        engine.Hub().push_dsh()
        block = self.find("agent-default-model")
        self.assertEqual(block["config"]["provider"], "deepseek-account")
        self.assertEqual(block["config"]["model"], "deepseek-flash")
        self.assertIsNone(self.find("subagent-model-selection-settings"))

    def test_unrelated_patch_blocks_survive(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"default": entry("deepseek", "deepseek/model-1")}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_dsh()
        locale = self.find("locale")
        self.assertEqual(locale["config"]["preference"], "zh")

    def test_credentials_receive_the_env_names_of_every_routed_provider(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["mygateway"] = {
            "name": "My Gateway", "displayName": "My Gateway",
            "baseUrl": "https://gateway.example.com/v1", "apiKey": "sk-my-gateway",
            "api": "openai-completions", "enabled": True,
            "models": [{"id": "vendor/model-a", "name": "Vendor A"}],
        }
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_dsh()
        creds = (self.home / ".dsh" / ".credentials.yaml").read_text(encoding="utf-8")
        self.assertIn("MYGATEWAY_API_KEY: sk-my-gateway", creds)
        self.assertIn("KEEP_ME: keep-value", creds)          # 其它凭据不动
        self.assertIn("MCODE_DEEPSEEK_API_KEY:", creds)      # 历史名字沿用
        self.assertIn("MYOPENROUTER_API_KEY:", creds)

    def test_new_provider_is_routed_with_its_own_env_name(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["mygateway"] = {
            "name": "My Gateway", "baseUrl": "https://gateway.example.com/v1",
            "apiKey": "sk-my-gateway", "api": "openai-completions", "enabled": True,
            "models": [{"id": "vendor/model-a", "name": "Vendor A"}],
        }
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_dsh()
        providers = self.find("llm-pi-ai")["config"]["providers"]
        self.assertIn("mygateway", providers)
        self.assertEqual(providers["mygateway"]["apiKeyEnv"], "MYGATEWAY_API_KEY")
        self.assertEqual(providers["mygateway"]["baseURL"], "https://gateway.example.com/v1")
        self.assertEqual(providers["mygateway"]["models"][0]["id"], "vendor/model-a")

    def test_dsh_target_restricted_provider_is_not_routed(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["tokenfree"] = {
            "name": "TokenFree", "baseUrl": "",
            "anthropicBaseUrl": "https://api.tokenfree.shop", "apiKey": "sk-test",
            "api": "openai-completions", "enabled": True, "targets": ["claude"],
            "models": [{"id": "claude-opus-5-5"}],
        }
        cfg["modelSync"] = {"default": entry("tokenfree", "claude-opus-5-5")}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_dsh()
        self.assertNotIn("tokenfree", self.find("llm-pi-ai")["config"]["providers"])
        # 全局默认值只适用于 Claude Code，dsh 保留用户原来的选择。
        self.assertEqual(self.find("agent-default-model")["config"]["provider"], "deepseek-account")

    def test_hand_typed_model_does_not_repoint_dsh(self):
        """模型不在库里时不能把 dsh 指向一个它不认识的模型。"""
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"default": entry("deepseek", "not-in-library")}
        engine.save_json(server.HUB_CONFIG, cfg)
        with self.assertRaises(ValueError):
            engine.Hub().push_dsh()
        self.assertEqual(self.find("agent-default-model")["config"]["model"], "deepseek-flash")

    def test_push_is_idempotent(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"default": entry("firework", "firework/model-1")}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_dsh()
        first = self.patch.read_text(encoding="utf-8")
        engine.Hub().push_dsh()
        self.assertEqual(first, self.patch.read_text(encoding="utf-8"))


class MultiAgentPushTests(unittest.TestCase):
    """跨客户端：一次 push 后 pi / dsh / claude 三个客户端指向同一个模型。"""

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        dsh_fixture(self.home)
        pi_dir = self.home / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        (pi_dir / "settings.json").write_text(json.dumps({
            "defaultProvider": "openai", "defaultModel": "gpt-6.1-sol"}), encoding="utf-8")

    def test_one_choice_reaches_every_client(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"default": entry("deepseek", "deepseek/model-1", "medium"),
                            "overrides": {"claude": {
                                "provider": "deepseek", "model": "deepseek/model-1",
                                "tiers": {"haiku": entry("deepseek", "deepseek/model-2"),
                                          "subagent": entry("deepseek", "deepseek/model-3")}}}}
        engine.save_json(server.HUB_CONFIG, cfg)
        hub = engine.Hub()
        hub.push_pi()
        hub.push_dsh()
        hub.push_claude()

        pi_settings = json.loads((self.home / ".pi" / "agent" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(pi_settings["defaultModel"], "deepseek/model-1")
        patch = engine.parse_yaml((self.home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml")
                                  .read_text(encoding="utf-8"))
        agent_model = next(b for b in patch if b.get("id") == "agent-default-model")
        self.assertEqual(agent_model["config"]["model"], "deepseek/model-1")
        env = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))["env"]
        # Claude Code 跟随全局主模型（deepseek 有 Claude 连接）
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek/model-1")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        # modelSync 显式指定的档位照写
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "deepseek/model-2")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "deepseek/model-3")
        # 没指定的档位不写
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env)
        self.assertNotIn("ANTHROPIC_DEFAULT_FABLE_MODEL", env)
        # dsh 的子代理档位：allowedModels 写当前主模型
        settings = next(b for b in patch if b.get("id") == "subagent-model-selection-settings")
        self.assertEqual(settings["config"]["allowedModels"],
                         [{"provider": "deepseek", "model": "deepseek/model-1"}])

    def test_global_choice_does_not_cross_providers_for_claude(self):
        """全局模型属于另一家供应商时，Claude Code 保持现状（不写 ANTHROPIC_*）。"""
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"default": entry("firework", "firework/model-1")}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        self.assertFalse((self.home / ".claude" / "settings.json").exists())

    def test_claude_uses_its_own_connection_key_and_endpoint(self):
        """Claude 连接自带端点与密钥时不与默认连接混用。"""
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"]["agents"]["claude"]["apiKey"] = "sk-anthropic-only"
        cfg["modelSync"] = {"overrides": {"claude": entry("deepseek", "deepseek/model-1")}}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        env = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-anthropic-only")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")

    def test_same_provider_different_key_per_agent(self):
        """同一供应商：Codex 用另一个 key 与另一套模型，Claude 用自己的连接。"""
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["tokenfree"] = {
            "name": "TokenFree", "displayName": "TokenFree", "baseUrl": "https://gateway.example/v1",
            "apiKey": "sk-openai", "api": "openai-completions", "enabled": True,
            "models": [{"id": "gpt-6-astra", "name": "GPT-6 Astra"}],
            "agents": {
                "claude": {"baseUrl": "https://gateway.example/v1", "api": "anthropic-messages",
                           "apiKey": "sk-anthropic", "models": [{"id": "claude-opus-5-5"}]},
                "codex": {"baseUrl": "https://gateway.example/v1", "api": "openai-responses",
                          "apiKeyEnv": "TOKENFREE_API_KEY",
                          "models": [{"id": "gpt-6-astra", "name": "GPT-6 Astra"}]},
            },
        }
        cfg["modelSync"] = {"overrides": {"claude": entry("tokenfree", "claude-opus-5-5"),
                                          "codex": entry("tokenfree", "gpt-6-astra", "high")}}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        env = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-anthropic")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-opus-5-5")

        codex = self.home / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "config.toml").write_text('model = "official-model"\n', encoding="utf-8")
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["sync"]["targets"]["codex"] = {"enabled": True, "mode": "third-party"}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_codex()
        text = (codex / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model = "gpt-6-astra"', text)
        self.assertIn('model_reasoning_effort = "high"', text)
        self.assertIn('env_key = "TOKENFREE_API_KEY"', text)
        self.assertIn('base_url = "https://gateway.example/v1"', text)


class CodexAdapterTests(unittest.TestCase):
    """Codex 适配器：只写 config.toml 的 model_provider / model 与 custom provider 段。"""

    REALISH_CONFIG = (
        'model = "gpt-6-astra"\n'
        'model_reasoning_effort = "xhigh"\n'
        "\n"
        'notify = [ "C:\\\\hooks\\\\turn-ended.exe", "turn-ended" ]\n'
        "\n"
        "[marketplaces.openai-primary-runtime]\n"
        'source_type = "local"\n'
        "source = 'C:\\\\cache\\\\runtime'\n"
        "\n"
        "[mcp_servers.node_repl]\n"
        "args = []\n"
        "command = 'C:\\\\runtime\\\\node_repl.exe'\n"
        "startup_timeout_sec = 120\n"
        "\n"
        '[plugins."computer-use@openai-bundled"]\n'
        "enabled = true\n"
        "\n"
        "# 旧注释\n"
        "[model_providers.custom]\n"
        'name = "custom"\n'
        'base_url = "http://127.0.0.1:15721/v1"\n'
        'wire_api = "responses"\n'
        'experimental_bearer_token = "PROXY_MANAGED"\n'
    )

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        codex_dir = self.home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "config.toml").write_text(self.REALISH_CONFIG, encoding="utf-8")
        self.config_path = codex_dir / "config.toml"
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["sync"]["targets"]["codex"] = {"enabled": True, "mode": "third-party",
                                           "files": ["~/.codex/config.toml"]}
        cfg["providers"]["deepseek"]["openaiApiKey"] = "sk-codex-literal"
        cfg["providers"]["deepseek"]["codexWireApi"] = "responses"
        cfg["modelSync"] = {"overrides": {"codex": entry("deepseek", "deepseek/model-2", "high")}}
        engine.save_json(server.HUB_CONFIG, cfg)

    def text(self) -> str:
        return self.config_path.read_text(encoding="utf-8")

    def test_writes_provider_model_and_custom_block(self):
        engine.Hub().push_codex()
        text = self.text()
        self.assertIn('model_provider = "agent_hub"', text)
        self.assertIn('model = "deepseek/model-2"', text)
        self.assertIn('model_reasoning_effort = "high"', text)
        self.assertIn('base_url = "https://api.deepseek.com"', text)
        self.assertIn("requires_openai_auth = false", text)
        self.assertIn('experimental_bearer_token = "sk-codex-literal"', text)
        self.assertNotIn("ENV_KEY", text)

    def test_hand_typed_model_is_written_with_warning(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"]["overrides"]["codex"]["model"] = "not-in-codex-library"
        engine.save_json(server.HUB_CONFIG, cfg)
        warnings: list[str] = []
        engine.Hub().push_codex(warnings=warnings)
        self.assertIn('model = "not-in-codex-library"', self.text())
        self.assertIn('model_provider = "agent_hub"', self.text())
        self.assertTrue(any("不在 deepseek 的 Codex 连接" in w for w in warnings))

    def test_only_hub_managed_keys_change(self):
        before = self.text()
        engine.Hub().push_codex()
        after = self.text()
        for kept in ('notify = [ "C:\\\\hooks\\\\turn-ended.exe", "turn-ended" ]',
                     "[marketplaces.openai-primary-runtime]",
                     "[mcp_servers.node_repl]",
                     '[plugins."computer-use@openai-bundled"]',
                     "startup_timeout_sec = 120"):
            self.assertIn(kept, before)
            self.assertIn(kept, after)
        self.assertEqual(after.count("model_provider = "), 1)
        self.assertEqual(after.count("[model_providers.custom]"), 1)

    def test_repeat_push_is_idempotent(self):
        engine.Hub().push_codex()
        once = self.text()
        engine.Hub().push_codex()
        self.assertEqual(once, self.text())

    def test_switching_between_env_key_and_literal_drops_the_other(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"].pop("openaiApiKey")
        cfg["providers"]["deepseek"]["apiKeyEnv"] = "DEEPSEEK_API_KEY"
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_codex()
        self.assertIn('env_key = "DEEPSEEK_API_KEY"', self.text())
        self.assertNotIn("experimental_bearer_token", self.text().split("[model_providers.agent_hub]")[1])

        cfg["providers"]["deepseek"]["openaiApiKey"] = "sk-codex-literal"
        cfg["providers"]["deepseek"].pop("apiKeyEnv", None)
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_codex()
        self.assertNotIn("env_key", self.text())
        self.assertIn('experimental_bearer_token = "sk-codex-literal"', self.text())

    def test_wire_api_defaults_to_responses(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"].pop("agents")
        cfg["providers"]["deepseek"]["api"] = "openai-responses"
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_codex()
        self.assertIn('wire_api = "responses"', self.text())

        # Codex 连接只接受 Responses；声明成别的协议直接拒绝，不写文件
        cfg["providers"]["deepseek"]["agents"] = {"codex": {"api": "openai-chat-completions"}}
        engine.save_json(server.HUB_CONFIG, cfg)
        before = self.text()
        with self.assertRaises(ValueError):
            engine.Hub().push_codex()
        self.assertEqual(before, self.text())

    def test_without_codex_modelsync_the_file_is_untouched(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg.pop("modelSync", None)
        engine.save_json(server.HUB_CONFIG, cfg)
        before = self.text()
        with self.assertRaises(ValueError):
            engine.Hub().push_codex()
        self.assertEqual(before, self.text())

    def test_targets_restricted_provider_is_rejected_without_writing(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"]["targets"] = ["claude"]
        engine.save_json(server.HUB_CONFIG, cfg)
        before = self.text()
        with self.assertRaises(ValueError):
            engine.Hub().push_codex()
        self.assertEqual(before, self.text())

    def test_missing_api_key_is_rejected_without_writing(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"].pop("openaiApiKey")
        cfg["providers"]["deepseek"]["apiKey"] = ""
        engine.save_json(server.HUB_CONFIG, cfg)
        before = self.text()
        with self.assertRaises(ValueError):
            engine.Hub().push_codex()
        self.assertEqual(before, self.text())

    def test_missing_base_url_is_rejected_without_writing(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"]["baseUrl"] = ""
        engine.save_json(server.HUB_CONFIG, cfg)
        before = self.text()
        with self.assertRaises(ValueError):
            engine.Hub().push_codex()
        self.assertEqual(before, self.text())

    def test_missing_config_file_is_reported(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        engine.save_json(server.HUB_CONFIG, cfg)
        self.config_path.unlink()
        with self.assertRaises(FileNotFoundError):
            engine.Hub().push_codex()

    def test_push_all_dispatches_codex(self):
        results = engine.Hub().push_all(only=["codex"])
        self.assertTrue(any("codex" in line for line in results), results)
        self.assertTrue(all("✗" not in line for line in results), results)


class DshSelectionGuardTests(unittest.TestCase):
    """dsh 模型选择的外部修改保护与协议翻译。

    与 ZCode/Codex 一致：接管期间 dsh 端被外部改过选择时 fail-closed，
    legacy push 不覆盖；显式 reconcile（工作台切换模型）才重新接管。
    """

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        dsh_fixture(self.home)
        self.dsh = self.home / ".dsh"
        self.patch = self.dsh / "profiles" / "desktop" / "cordis.patch.yml"
        self.state = self.dsh / ".agent-hub-state.json"
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"default": entry("stepfun", "stepfun/model-2", "high")}
        engine.save_json(server.HUB_CONFIG, cfg)

    def blocks(self) -> list:
        return engine.parse_yaml(self.patch.read_text(encoding="utf-8"))

    def find(self, block_id: str) -> dict | None:
        return next((b for b in self.blocks()
                     if isinstance(b, dict) and b.get("id") == block_id), None)

    def rewrite_selection(self, provider: str, model: str) -> None:
        items = self.blocks()
        block = next(b for b in items if b["id"] == "agent-default-model")
        block["config"].update(provider=provider, model=model)
        self.patch.write_text(engine.dump_yaml(items) + "\n", encoding="utf-8")

    def test_chat_completions_api_is_translated_for_dsh(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["xunfri-like"] = {
            "name": "Xunfri", "displayName": "Xunfri",
            "baseUrl": "https://maas.example.com/v2", "apiKey": "test-not-a-real-key",
            "api": "openai-chat-completions", "enabled": True,
            "models": [{"id": "spark-x", "name": "Spark X"}]}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_dsh()
        data = engine.parse_yaml((self.dsh / "settings.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data["llm-pi-ai"]["providers"]["xunfri-like"]["api"],
                         "openai-completions")
        block = next(b for b in self.blocks() if b["id"] == "llm-pi-ai")
        self.assertEqual(block["config"]["providers"]["xunfri-like"]["api"],
                         "openai-completions")

    def test_first_takeover_records_managed_state_and_is_idempotent(self):
        engine.Hub().push_dsh()
        block = self.find("agent-default-model")
        self.assertEqual(block["config"]["provider"], "stepfun")
        saved = engine.load_json(self.state)
        self.assertTrue(saved["managed"])
        self.assertEqual(saved["selectionHash"],
                         engine._dsh_selection_fingerprint(self.blocks()))
        engine.Hub().push_dsh()
        self.assertEqual(saved, engine.load_json(self.state))

    def test_external_selection_change_fails_closed(self):
        engine.Hub().push_dsh()
        self.rewrite_selection("deepseek-account", "deepseek-flash")
        with self.assertRaises(ValueError) as caught:
            engine.Hub().push_dsh()
        self.assertIn("外部更改", str(caught.exception))
        block = self.find("agent-default-model")
        self.assertEqual(block["config"]["provider"], "deepseek-account")
        # legacy push / 网关开关走的预览路径同样拒绝，不会悄悄打回
        with self.assertRaises(Exception) as preview_error:
            engine.Hub().preview_push(["dsh"])
        self.assertIn("外部更改", str(preview_error.exception))

    def test_explicit_reconcile_overwrites_external_edit(self):
        engine.Hub().push_dsh()
        self.rewrite_selection("deepseek-account", "deepseek-flash")
        plan = engine.Hub().preview_push(["dsh"], reconcile=True)
        self.assertTrue(any("外部更改" in w for w in plan["warnings"]), plan["warnings"])
        engine.Hub().apply_preview(plan["planId"])
        block = self.find("agent-default-model")
        self.assertEqual(block["config"]["provider"], "stepfun")
        self.assertEqual(block["config"]["model"], "stepfun/model-2")
        saved = engine.load_json(self.state)
        self.assertTrue(saved["managed"])
        self.assertEqual(saved["selectionHash"],
                         engine._dsh_selection_fingerprint(self.blocks()))
        # 重新接管后普通推送恢复幂等
        engine.Hub().push_dsh()

    def test_unmanaged_state_is_written_when_modelsync_absent(self):
        engine.save_json(server.HUB_CONFIG, config_without_modelsync())
        engine.Hub().push_dsh()
        block = self.find("agent-default-model")
        self.assertEqual(block["config"]["provider"], "deepseek-account")
        self.assertEqual(engine.load_json(self.state),
                         {"version": 1, "managed": False})

    def test_patch_llm_pi_ai_block_is_recreated_after_profile_reset(self):
        engine.Hub().push_dsh()
        # 模拟桌面应用重建 profile：补丁被重写，llm-pi-ai 块消失，选择块保留
        items = [b for b in self.blocks()
                 if not (isinstance(b, dict) and b.get("id") == "llm-pi-ai")]
        self.patch.write_text(engine.dump_yaml(items) + "\n", encoding="utf-8")
        engine.Hub().push_dsh()
        block = self.find("llm-pi-ai")
        self.assertEqual(block["name"], "@deepseek-ai/dsh-llm-pi-ai")
        self.assertIn("stepfun", block["config"]["providers"])
        self.assertIn("deepseek", block["config"]["providers"])
        data = engine.parse_yaml((self.dsh / "settings.yaml").read_text(encoding="utf-8"))
        self.assertIn("stepfun", data["llm-pi-ai"]["providers"])


if __name__ == "__main__":
    unittest.main()
