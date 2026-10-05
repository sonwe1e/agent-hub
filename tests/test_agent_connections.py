# -*- coding: utf-8 -*-
"""「默认连接 + agents.<agent> 覆盖」与旧配置迁移的回归测试。

覆盖四件事：
1. agent_connection 的合并顺序（agents.<agent> > 顶层默认）与旧字段兼容；
2. 各 Agent 的连接就绪判定（Claude 需要 Anthropic 端点，Codex 只认 Responses）；
3. 模型库按连接隔离：同一供应商不同 key 可以有不同的模型集合；
4. hub migrate-agents 把旧扁平字段搬进新结构，且重复执行是幂等的。
"""
from __future__ import annotations

import json
import unittest

from support import isolated, engine, server


def claude_block(**over):
    block = {"baseUrl": "https://gateway.example/v1", "api": "anthropic-messages",
             "apiKey": "sk-anthropic", "models": [{"id": "claude-opus-5-5", "name": "Opus"}]}
    block.update(over)
    return block


class AgentConnectionTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def config(self):
        return json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))

    def save(self, cfg):
        engine.save_json(server.HUB_CONFIG, cfg)

    def test_default_connection_is_used_without_overrides(self):
        p = self.config()["providers"]["deepseek"]
        conn = engine.agent_connection(p, "pi")
        self.assertEqual(conn["baseUrl"], "https://api.deepseek.com")
        self.assertEqual(conn["api"], "openai-completions")
        self.assertEqual(len(conn["models"]), 7)
        self.assertEqual(conn["models"][0]["id"], "deepseek/model-1")

    def test_agent_override_wins_over_defaults(self):
        cfg = self.config()
        cfg["providers"]["deepseek"]["agents"]["pi"] = {
            "baseUrl": "https://pi.example/v1", "apiKey": "sk-pi",
            "models": [{"id": "pi-only-model"}],
        }
        self.save(cfg)
        p = self.config()["providers"]["deepseek"]
        conn = engine.agent_connection(p, "pi")
        self.assertEqual(conn["baseUrl"], "https://pi.example/v1")
        self.assertEqual(conn["apiKey"], "sk-pi")
        self.assertEqual([m["id"] for m in conn["models"]], ["pi-only-model"])
        # 其它 Agent 仍走默认连接
        self.assertEqual(engine.agent_connection(p, "dsh")["baseUrl"], "https://api.deepseek.com")

    def test_claude_requires_a_declared_anthropic_endpoint(self):
        cfg = self.config()
        cfg["providers"]["stepfun"]["agents"] = {"claude": claude_block()}
        self.save(cfg)
        p = self.config()["providers"]["stepfun"]
        self.assertTrue(engine.agent_connection_ready(p, "claude"))
        self.assertEqual(engine.agent_connection(p, "claude")["apiKey"], "sk-anthropic")
        # 没有声明 Claude 连接的供应商：OpenAI 端点不能直接给 Claude Code 用
        self.assertFalse(engine.agent_connection_ready(self.config()["providers"]["firework"], "claude"))
        # 协议不对也不就绪
        bad = dict(p, agents={"claude": claude_block(api="openai-completions")})
        self.assertFalse(engine.agent_connection_ready(bad, "claude"))

    def test_claude_connection_may_come_from_legacy_anthropic_base_url(self):
        cfg = self.config()
        cfg["providers"]["firework"]["anthropicBaseUrl"] = "https://firework.example/anthropic"
        self.save(cfg)
        p = self.config()["providers"]["firework"]
        conn = engine.agent_connection(p, "claude")
        self.assertEqual(conn["baseUrl"], "https://firework.example/anthropic")
        self.assertEqual(conn["api"], "anthropic-messages")
        self.assertTrue(engine.agent_connection_ready(p, "claude"))
        # 旧 claudeEnv / claudeSettings 映射成连接的 env / settings
        p["claudeEnv"] = {"ENABLE_TOOL_SEARCH": "true"}
        p["claudeSettings"] = {"effortLevel": "high"}
        self.assertEqual(engine.agent_connection(p, "claude")["env"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(engine.agent_connection(p, "claude")["settings"]["effortLevel"], "high")

    def test_codex_requires_responses(self):
        cfg = self.config()
        cfg["providers"]["stepfun"]["agents"] = {"codex": {"api": "openai-responses"}}
        self.save(cfg)
        p = self.config()["providers"]["stepfun"]
        self.assertTrue(engine.agent_connection_ready(p, "codex"))
        # Chat Completions 端点不能给 Codex 第三方线路用
        p["agents"]["codex"]["api"] = "openai-chat-completions"
        self.assertFalse(engine.agent_connection_ready(p, "codex"))
        # 旧 codexWireApi 也认
        legacy = {"baseUrl": "https://x.example/v1", "api": "openai-completions",
                  "apiKey": "k", "codexWireApi": "responses"}
        self.assertTrue(engine.agent_connection_ready(legacy, "codex"))

    def test_providers_for_only_returns_ready_connections(self):
        cfg = self.config()
        cfg["providers"]["tokenfree"] = {
            "name": "TokenFree", "baseUrl": "https://gateway.example/v1",
            "apiKey": "sk-openai", "api": "openai-completions", "enabled": True,
            "models": [{"id": "gpt-6-astra"}],
            "agents": {"claude": claude_block(), "codex": {"api": "openai-responses"}},
        }
        self.save(cfg)
        hub = engine.Hub()
        self.assertIn("tokenfree", hub.providers_for("claude"))
        self.assertIn("tokenfree", hub.providers_for("codex"))
        self.assertIn("tokenfree", hub.providers_for("pi"))
        # 纯 Claude 端点（无 OpenAI baseUrl）不进 pi / dsh
        cfg["providers"]["tokenfree"]["baseUrl"] = ""
        self.save(cfg)
        hub = engine.Hub()
        self.assertIn("tokenfree", hub.providers_for("claude"))
        self.assertNotIn("tokenfree", hub.providers_for("pi"))
        self.assertNotIn("tokenfree", hub.providers_for("dsh"))

    def test_model_options_are_scoped_per_agent(self):
        cfg = self.config()
        cfg["providers"]["tokenfree"] = {
            "name": "TokenFree", "baseUrl": "https://gateway.example/v1",
            "apiKey": "sk-openai", "api": "openai-completions", "enabled": True,
            "models": [{"id": "gpt-6-astra", "name": "Astra"}],
            "agents": {"claude": claude_block(), "codex": {"api": "openai-responses",
                                                           "models": [{"id": "gpt-6-sol"}]}},
        }
        self.save(cfg)
        self.assertEqual([m["id"] for m in engine.agent_model_options(cfg, "claude")["tokenfree"]],
                         ["claude-opus-5-5"])
        self.assertEqual([m["id"] for m in engine.agent_model_options(cfg, "codex")["tokenfree"]],
                         ["gpt-6-sol"])
        self.assertEqual([m["id"] for m in engine.agent_model_options(cfg, "pi")["tokenfree"]],
                         ["gpt-6-astra"])
        # 总览视图合并所有连接
        self.assertEqual([m["id"] for m in engine.agent_model_options(cfg)["tokenfree"]],
                         ["gpt-6-astra", "claude-opus-5-5", "gpt-6-sol"])


class MigrateAgentsTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def config(self):
        return json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))

    def legacy(self):
        """一份旧版扁平配置：anthropicBaseUrl / claudeModels / openaiApiKey / activeProvider。"""
        cfg = self.config()
        p = cfg["providers"]["tokenfree"] = {
            "name": "TokenFree", "displayName": "TokenFree",
            "baseUrl": "https://api.tokenfree.shop/v1", "apiKey": "sk-claude-key",
            "api": "openai-completions", "enabled": True, "targets": ["claude", "codex"],
            "anthropicBaseUrl": "https://api.tokenfree.shop/v1",
            "claudeModels": {"model": "claude-opus-5-5", "opus": "claude-opus-5-5",
                             "haiku": "claude-opus-5-5"},
            "claudeEnv": {"ENABLE_TOOL_SEARCH": "true"},
            "claudeSettings": {"model": "claude-opus-5-5", "effortLevel": "high"},
            "openaiApiKey": "sk-codex-key", "apiKeyEnv": "TOKENFREE_API_KEY",
            "codexWireApi": "responses",
            "models": [{"id": "gpt-6-astra", "name": "Astra"}],
        }
        cfg["sync"]["targets"]["claude"]["activeProvider"] = "tokenfree"
        cfg["modelSync"] = {"default": {"provider": "tokenfree", "model": "gpt-6-astra"}}
        return cfg

    def test_migration_moves_legacy_fields(self):
        engine.save_json(server.HUB_CONFIG, self.legacy())
        self.assertEqual(engine.cmd_migrate_agents(engine.Hub(), None), 0)
        cfg = self.config()
        p = cfg["providers"]["tokenfree"]
        # 旧扁平字段被清掉
        for field in ("anthropicBaseUrl", "claudeModels", "claudeEnv", "claudeSettings",
                      "openaiApiKey", "apiKeyEnv", "codexWireApi"):
            self.assertNotIn(field, p)
        # Claude 连接接走端点 / 透传字段 / 模型目录
        claude = p["agents"]["claude"]
        self.assertEqual(claude["baseUrl"], "https://api.tokenfree.shop/v1")
        self.assertEqual(claude["api"], "anthropic-messages")
        self.assertEqual(claude["env"], {"ENABLE_TOOL_SEARCH": "true"})
        self.assertEqual(claude["settings"]["effortLevel"], "high")
        self.assertIn("claude-opus-5-5", [m["id"] for m in claude["models"]])
        # Codex 连接接走另一个 key 与协议
        codex = p["agents"]["codex"]
        self.assertEqual(codex["apiKey"], "sk-codex-key")
        self.assertEqual(codex["apiKeyEnv"], "TOKENFREE_API_KEY")
        self.assertEqual(codex["api"], "openai-responses")
        # 档位搬进 modelSync，activeProvider 删除
        tiers = cfg["modelSync"]["overrides"]["claude"]["tiers"]
        self.assertEqual(tiers["opus"], {"provider": "tokenfree", "model": "claude-opus-5-5"})
        self.assertEqual(cfg["modelSync"]["overrides"]["claude"]["model"], "claude-opus-5-5")
        self.assertNotIn("activeProvider", cfg["sync"]["targets"]["claude"])
        # 迁移后各连接就绪，模型一个都没少
        self.assertTrue(engine.agent_connection_ready(p, "claude"))
        self.assertTrue(engine.agent_connection_ready(p, "codex"))
        self.assertTrue(engine.agent_connection_ready(p, "pi"))

    def test_migration_is_idempotent(self):
        engine.save_json(server.HUB_CONFIG, self.legacy())
        engine.cmd_migrate_agents(engine.Hub(), None)
        first = self.config()
        engine.cmd_migrate_agents(engine.Hub(), None)
        self.assertEqual(first, self.config())

    def test_migration_keeps_existing_modelsync_tiers(self):
        cfg = self.legacy()
        cfg["modelSync"] = {"overrides": {"claude": {
            "provider": "tokenfree", "model": "already-set",
            "tiers": {"opus": {"provider": "tokenfree", "model": "already-set"}}}}}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.cmd_migrate_agents(engine.Hub(), None)
        tiers = self.config()["modelSync"]["overrides"]["claude"]["tiers"]
        self.assertEqual(tiers["opus"]["model"], "already-set")
        self.assertEqual(self.config()["modelSync"]["overrides"]["claude"]["model"], "already-set")

    def test_migration_ignores_inactive_provider_claude_models(self):
        """非活跃供应商的 claudeModels 本来就是惰性的，迁移时直接丢弃。"""
        cfg = self.config()
        cfg["providers"]["openrouter"]["claudeModels"] = {"model": "openrouter/stale"}
        cfg["sync"]["targets"]["claude"]["activeProvider"] = "deepseek"
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.cmd_migrate_agents(engine.Hub(), None)
        cfg = self.config()
        self.assertNotIn("claudeModels", cfg["providers"]["openrouter"])
        self.assertNotIn("overrides", cfg["modelSync"])


if __name__ == "__main__":
    unittest.main()


class AdapterConnectionTests(unittest.TestCase):
    """适配器必须按各自连接写端点与密钥，而不是一律用顶层默认值。"""

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def config(self):
        return json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))

    def save(self, cfg):
        engine.save_json(server.HUB_CONFIG, cfg)

    def gateway(self):
        cfg = self.config()
        cfg["providers"]["gateway"] = {
            "name": "Gateway", "displayName": "Gateway",
            "baseUrl": "https://default.example/v1", "apiKey": "sk-default",
            "api": "openai-completions", "enabled": True,
            "models": [{"id": "default-model", "name": "Default"}],
            "agents": {
                "pi": {"baseUrl": "https://pi.example/v1", "apiKey": "sk-pi",
                       "models": [{"id": "pi-model", "name": "Pi"}]},
                "dsh": {"baseUrl": "https://dsh.example/v1", "apiKey": "sk-dsh",
                        "models": [{"id": "dsh-model", "name": "Dsh"}]},
            },
        }
        self.save(cfg)

    def test_pi_adapter_writes_the_pi_connection(self):
        self.gateway()
        pi_dir = self.home / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        (pi_dir / "settings.json").write_text("{}", encoding="utf-8")
        engine.Hub().push_pi()
        models = json.loads((pi_dir / "models.json").read_text(encoding="utf-8"))
        entry = models["providers"]["gateway"]
        self.assertEqual(entry["baseUrl"], "https://pi.example/v1")
        self.assertEqual(entry["apiKey"], "sk-pi")
        self.assertEqual([m["id"] for m in entry["models"]], ["pi-model"])
        auth = json.loads((pi_dir / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(auth["gateway"]["key"], "sk-pi")
        # 默认连接的模型不会漏进 pi
        self.assertNotIn("default-model", [m["id"] for m in entry["models"]])

    def test_dsh_adapter_writes_the_dsh_connection(self):
        self.gateway()
        dsh = self.home / ".dsh"
        (dsh / "profiles" / "desktop").mkdir(parents=True)
        (dsh / "settings.yaml.imported").write_text("llm-pi-ai:\n  providers: {}\n", encoding="utf-8")
        (dsh / ".credentials.yaml").write_text("version: 1\nrefs: {}\n", encoding="utf-8")
        (dsh / "profiles" / "desktop" / "cordis.patch.yml").write_text(
            '- id: llm-pi-ai\n  name: "llm"\n  config:\n    providers: {}\n', encoding="utf-8")
        engine.Hub().push_dsh()
        imported = engine.parse_yaml((dsh / "settings.yaml.imported").read_text(encoding="utf-8"))
        entry = imported["llm-pi-ai"]["providers"]["gateway"]
        self.assertEqual(entry["baseURL"], "https://dsh.example/v1")
        self.assertEqual(entry["apiKeyEnv"], "GATEWAY_API_KEY")
        self.assertEqual([m["id"] for m in entry["models"]], ["dsh-model"])
        creds = (dsh / ".credentials.yaml").read_text(encoding="utf-8")
        self.assertIn("GATEWAY_API_KEY: sk-dsh", creds)

    def test_pull_from_writes_back_the_same_layer(self):
        self.gateway()
        pi_dir = self.home / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        (pi_dir / "models.json").write_text(json.dumps({"providers": {
            "gateway": {"baseUrl": "https://pi.example/v1", "apiKey": "sk-pi-rotated"}}}),
            encoding="utf-8")
        engine.Hub().pull_from("pi")
        p = self.config()["providers"]["gateway"]
        self.assertEqual(p["agents"]["pi"]["apiKey"], "sk-pi-rotated")
        self.assertEqual(p["apiKey"], "sk-default")


if __name__ == "__main__":
    unittest.main()
