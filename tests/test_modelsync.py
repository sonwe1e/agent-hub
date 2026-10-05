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

    def test_explicit_tiers_beat_legacy_claude_models(self):
        self.cfg["providers"]["deepseek"]["claudeModels"] = {
            "model": "legacy-model", "opus": "legacy-opus", "haiku": "legacy-haiku",
        }
        self.cfg["sync"]["targets"]["claude"]["activeProvider"] = "deepseek"
        self.cfg["modelSync"] = {"default": entry(),
                                 "overrides": {"claude": {
                                     "provider": "deepseek", "model": "deepseek/model-1",
                                     "tiers": {"opus": entry("openrouter", "openrouter/model-3")}}}}
        tiers = self.tiers("claude")
        self.assertEqual(tiers["opus"]["provider"], "openrouter")
        self.assertEqual(tiers["opus"]["model"], "openrouter/model-3")
        # 未显式覆盖的别名档位继续走旧 claudeModels
        self.assertEqual(tiers["haiku"]["model"], "legacy-haiku")
        # 主模型档位：全局选择的供应商 == 当前 Anthropic 供应商（都是 deepseek），
        # 所以全局主模型接管，claudeModels.model 退居其次
        self.assertEqual(tiers["model"]["model"], "deepseek/model-1")

    def test_legacy_claude_models_only_from_active_provider(self):
        """非活跃供应商的 claudeModels 不能污染当前档位（历史缺陷的根因）。"""
        self.cfg["sync"]["targets"]["claude"]["activeProvider"] = "deepseek"
        self.cfg["providers"]["openrouter"]["claudeModels"] = {"haiku": "openrouter/haiku"}
        self.assertNotIn("haiku", self.tiers("claude"))

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

    def test_public_tier_view_hides_internal_tiers(self):
        self.cfg["providers"]["deepseek"]["claudeModels"] = {"fable": "deepseek-flash",
                                                             "model": "deepseek-v4-flash"}
        self.cfg["sync"]["targets"]["claude"]["activeProvider"] = "deepseek"
        internal = engine.agent_model_tiers(self.cfg, "claude")
        public = engine.agent_model_tiers_public(self.cfg, "claude")
        self.assertIn("fable", internal)      # 适配器要靠它写/收回旧别名
        self.assertNotIn("fable", public)     # 界面不把它当目标档位
        self.assertNotIn("agent", public)


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
            "name": "TokenFree", "baseUrl": "https://api.tokenfree.shop",
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
            "name": "TokenFree", "baseUrl": "https://api.tokenfree.shop",
            "anthropicBaseUrl": "https://api.tokenfree.shop", "apiKey": "sk-test",
            "api": "openai-completions", "enabled": True, "targets": ["claude"],
            "models": [{"id": "claude-opus-5-5"}],
        }
        cfg["modelSync"] = {"default": entry("tokenfree", "claude-opus-5-5")}
        engine.save_json(server.HUB_CONFIG, cfg)
        with self.assertRaises(ValueError) as ctx:
            engine.Hub().push_dsh()
        self.assertIn("tokenfree", str(ctx.exception))
        # 失败的推送不能留下半套写入：模型选择必须还是用户原来的
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
        cfg["providers"]["deepseek"]["anthropicBaseUrl"] = "https://api.deepseek.com/anthropic"
        cfg["providers"]["deepseek"]["claudeModels"] = {
            "model": "deepseek-v4-flash", "haiku": "deepseek-v4-flash",
            "subagent": "deepseek-v4-flash",
        }
        cfg["sync"]["targets"]["claude"]["activeProvider"] = "deepseek"
        cfg["modelSync"] = {"default": entry("deepseek", "deepseek/model-1", "medium")}
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
        # Claude Code 的供应商就是全局选中的这个，所以主模型一起对齐
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek/model-1")
        # claudeModels 自己声明的档位（haiku / subagent）不被全局设置改写
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "deepseek-v4-flash")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "deepseek-v4-flash")
        # dsh 的子代理档位：allowedModels 写当前主模型
        settings = next(b for b in patch if b.get("id") == "subagent-model-selection-settings")
        self.assertEqual(settings["config"]["allowedModels"],
                         [{"provider": "deepseek", "model": "deepseek/model-1"}])

    def test_global_choice_does_not_cross_providers_for_claude(self):
        """全局模型属于另一家供应商时，Claude Code 保持自己的 claudeModels。"""
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"]["claudeModels"] = {"model": "deepseek-v4-flash"}
        cfg["sync"]["targets"]["claude"]["activeProvider"] = "deepseek"
        cfg["modelSync"] = {"default": entry("firework", "firework/model-1")}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        env = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))["env"]
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek-v4-flash")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")

    def test_global_choice_is_used_when_platform_has_no_active_provider(self):
        """平台配置没有可用的 activeProvider 时，Claude Code 跟随全局选择。"""
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"]["anthropicBaseUrl"] = "https://api.deepseek.com/anthropic"
        cfg["providers"]["deepseek"]["claudeModels"] = {"model": "stale-model"}
        cfg["providers"]["openrouter"]["claudeModels"] = {"model": "openrouter/stale"}
        cfg["sync"]["targets"]["claude"].pop("activeProvider", None)   # 未指定
        cfg["modelSync"] = {"default": entry("deepseek", "deepseek/model-1")}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        data = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "deepseek/model-1")
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        # 编译不隐式回写 Hub；后续同步仍按相同的 modelSync 解析。
        on_disk = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        self.assertNotIn("activeProvider", on_disk["sync"]["targets"]["claude"])


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
        cfg["providers"]["deepseek"].pop("codexWireApi")
        cfg["providers"]["deepseek"]["api"] = "openai-responses"
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_codex()
        self.assertIn('wire_api = "responses"', self.text())

        cfg["providers"]["deepseek"]["codexWireApi"] = "chat"
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


if __name__ == "__main__":
    unittest.main()
