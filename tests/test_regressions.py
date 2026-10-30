"""针对已修复缺陷的回归测试。

覆盖四起真实事故：
1. pi 适配器抹掉 provider 级 compat 与模型级 cost/compat，并把 input 写成字符串；
2. pull_from / 旧版 GUI 用陈旧内存快照整文件回写 providers.json，模型被静默删除；
3. 整文件写入缺少护栏，任何路径都能把模型库改小；
4. hub 往 cc-switch.db 写 providers.created_at 用了 datetime('now')（TEXT），
   cc-switch 按 INTEGER epoch 毫秒读取，启动即崩，整个 app 打不开。
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from support import isolated, engine, server


class PiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.pi_dir = self.home / ".pi" / "agent"
        self.pi_dir.mkdir(parents=True)
        # pi 侧原有条目：带模型级 cost/compat，还有 pi 独有字段
        (self.pi_dir / "models.json").write_text(json.dumps({
            "providers": {
                "deepseek": {
                    "baseUrl": "https://api.deepseek.com",
                    "api": "openai-completions",
                    "apiKey": "old",
                    "headers": {"x-keep": "1"},
                    "models": [{
                        "id": "deepseek/model-1",
                        "name": "Old name",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "contextWindow": 128000,
                        "maxTokens": 32768,
                        "cost": {"input": 0.3, "output": 1.2},
                        "compat": {"thinkingFormat": "deepseek"},
                    }],
                }
            }
        }, ensure_ascii=False), encoding="utf-8")

    def pi_models(self) -> dict:
        return json.loads((self.pi_dir / "models.json").read_text(encoding="utf-8"))

    def test_provider_compat_is_written(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"]["compat"] = {
            "maxTokensField": "max_tokens",
            "supportsDeveloperRole": False,
            "thinkingFormat": "deepseek",
        }
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_pi()
        entry = self.pi_models()["providers"]["deepseek"]
        self.assertEqual(entry["compat"]["thinkingFormat"], "deepseek")
        self.assertEqual(entry["compat"]["maxTokensField"], "max_tokens")
        self.assertFalse(entry["compat"]["supportsDeveloperRole"])

    def test_existing_model_cost_and_compat_survive(self):
        # hub 没有显式配置 cost/compat 时，pi 侧的手工参数必须原样保留
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        for model in cfg["providers"]["deepseek"]["models"]:
            model.pop("cost", None)
            model.pop("compat", None)
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_pi()
        model = next(m for m in self.pi_models()["providers"]["deepseek"]["models"]
                     if m["id"] == "deepseek/model-1")
        self.assertEqual(model["cost"], {"input": 0.3, "output": 1.2})
        self.assertEqual(model["compat"], {"thinkingFormat": "deepseek"})
        # hub 负责的字段仍然更新
        self.assertEqual(model["name"], "DeepSeek Model 1")

    def test_hub_cost_wins_over_pi_side_value(self):
        engine.Hub().push_pi()
        model = next(m for m in self.pi_models()["providers"]["deepseek"]["models"]
                     if m["id"] == "deepseek/model-1")
        self.assertEqual(model["cost"], {"input": 0.5})

    def test_model_list_is_written_as_array(self):
        engine.Hub().push_pi()
        for entry in self.pi_models()["providers"].values():
            for model in entry["models"]:
                self.assertIsInstance(model["input"], list, model["id"])

    def test_pi_only_provider_fields_survive(self):
        engine.Hub().push_pi()
        self.assertEqual(self.pi_models()["providers"]["deepseek"]["headers"], {"x-keep": "1"})

    def test_new_models_are_appended(self):
        engine.Hub().push_pi()
        ids = [m["id"] for m in self.pi_models()["providers"]["deepseek"]["models"]]
        self.assertEqual(len(ids), 7)
        self.assertIn("deepseek/model-7", ids)

    def test_sync_keeps_unmanaged_qwen_and_legacy_named_providers_and_auth(self):
        models=self.pi_models()
        names=('qwen-token-plan-cn','volcano','cc-switch-step-fun','provider-0b8770')
        for name in names:models['providers'][name]={'apiKey':'local','models':[]}
        (self.pi_dir/'models.json').write_text(json.dumps(models),encoding='utf-8')
        (self.pi_dir/'auth.json').write_text(json.dumps({name:{'type':'oauth','token':'fixture'} for name in names}),encoding='utf-8')
        engine.Hub().push_pi()
        for name in names:
            self.assertIn(name,self.pi_models()['providers'])
            self.assertIn(name,json.loads((self.pi_dir/'auth.json').read_text(encoding='utf-8')))


class StaleWriteTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def counts(self) -> dict:
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        return {pid: len(p.get("models") or []) for pid, p in cfg["providers"].items()}

    def test_pull_from_does_not_revert_newer_edits(self):
        stale = engine.Hub()                      # 模拟常驻进程启动时的快照
        server.api_add_model("deepseek", {"id": "deepseek/added-later", "name": "Later",
                                          "contextWindow": 128000, "maxTokens": 32768,
                                          "input": ["text"], "reasoning": True})
        before = self.counts()
        stale.pull_from("pi")
        self.assertEqual(before, self.counts())
        ids = [m["id"] for m in json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
               ["providers"]["deepseek"]["models"]]
        self.assertIn("deepseek/added-later", ids)

    def test_pull_from_keeps_all_providers(self):
        stale = engine.Hub()
        before = self.counts()
        stale.pull_from("pi")
        self.assertEqual(before, self.counts())

    def test_remove_provider_only_touches_that_provider(self):
        hub = engine.Hub()
        before = self.counts()
        hub.remove_provider("firework")
        after = self.counts()
        self.assertNotIn("firework", after)
        for pid, count in before.items():
            if pid != "firework":
                self.assertEqual(after[pid], count, pid)

    def test_remove_provider_keeps_unrelated_dsh_qwen_entries(self):
        imported = self.home / '.dsh' / 'settings.yaml.imported'
        imported.parent.mkdir(parents=True)
        providers = {'firework': {'api': 'example'},
                     'qwen-token-unrelated': {'api': 'other'},
                     'qwen-token-plan-cn': {'api': 'legacy'}}
        imported.write_text(engine.dump_yaml({'llm-pi-ai': {'providers': providers}}), encoding='utf-8')
        engine.Hub().remove_provider('firework')
        after = engine.parse_yaml(imported.read_text(encoding='utf-8'))['llm-pi-ai']['providers']
        self.assertEqual(after, {k: v for k, v in providers.items() if k != 'firework'})

    def test_removing_named_qwen_provider_leaves_other_qwen_providers(self):
        imported = self.home / '.dsh' / 'settings.yaml.imported'
        imported.parent.mkdir(parents=True)
        providers = {'qwen-token-plan-cn': {'api': 'selected'}, 'qwen-token-other': {'api': 'other'}}
        imported.write_text(engine.dump_yaml({'llm-pi-ai': {'providers': providers}}), encoding='utf-8')
        engine.Hub().remove_provider('qwen-token-plan-cn')
        after = engine.parse_yaml(imported.read_text(encoding='utf-8'))['llm-pi-ai']['providers']
        self.assertEqual(after, {'qwen-token-other': {'api': 'other'}})

    def test_dsh_sync_keeps_unmanaged_qwen_routes_in_all_three_layers(self):
        root=self.home/'.dsh';profile=root/'profiles/desktop';profile.mkdir(parents=True)
        entries={'qwen-token-other':{'api':'untouched'}}
        imported=root/'settings.yaml.imported';settings=root/'settings.yaml';patch=profile/'cordis.patch.yml'
        value={'llm-pi-ai':{'providers':entries}}
        imported.write_text(engine.dump_yaml(value),encoding='utf-8')
        settings.write_text(engine.dump_yaml(value),encoding='utf-8')
        patch.write_text(engine.dump_yaml([{'id':'llm-pi-ai','name':'@deepseek-ai/dsh-llm-pi-ai',
                                          'config':{'providers':entries}}]),encoding='utf-8')
        engine.Hub().push_dsh()
        for file in (imported,settings):
            value=engine.parse_yaml(file.read_text(encoding='utf-8'))
            self.assertEqual(value['llm-pi-ai']['providers']['qwen-token-other'],{'api':'untouched'})
        data=engine.parse_yaml(patch.read_text(encoding='utf-8'))
        item=next(x for x in data if x.get('id')=='llm-pi-ai')
        self.assertEqual(item['config']['providers']['qwen-token-other'],{'api':'untouched'})

    def test_dsh_sync_creates_missing_configuration_directory(self):
        engine.Hub().push_dsh()
        self.assertTrue((self.home/'.dsh/settings.yaml').is_file())

    def test_nothing_is_written_when_guard_rejects(self):
        before = server.HUB_CONFIG.read_bytes()
        hub = engine.Hub()
        hub.providers["deepseek"]["models"] = hub.providers["deepseek"]["models"][:1]
        with self.assertRaises(engine.ModelLossError):
            server._save_config(hub)
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_declared_deletes_are_allowed(self):
        hub = engine.Hub()
        ids = [m["id"] for m in hub.providers["deepseek"]["models"][:6]]
        hub.providers["deepseek"]["models"] = [
            m for m in hub.providers["deepseek"]["models"] if m["id"] not in ids]
        server._save_config(hub, allowed_removed={"deepseek": set(ids)})
        self.assertEqual(self.counts()["deepseek"], 1)

    def test_drop_snapshot_still_written(self):
        hub = engine.Hub()
        ids = {m["id"] for m in hub.providers["deepseek"]["models"]}
        hub.providers["deepseek"]["models"] = []
        server._save_config(hub, allowed_removed={"deepseek": ids})
        self.assertTrue(list(engine.HUB_DIR.glob("providers.json.drop-*")))


class RestoreGoldTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def test_restore_gold_replaces_models_and_keeps_credentials(self):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        gold = {"providers": {"deepseek": {"models": [
            {"id": f"deepseek/gold-{i}", "name": f"Gold {i}", "contextWindow": 128000,
             "maxTokens": 32768, "input": ["text"], "reasoning": True} for i in range(3)]}}}
        (engine.HUB_DIR / "providers.gold.json").write_text(json.dumps(gold), encoding="utf-8")
        key_before = cfg["providers"]["deepseek"]["apiKey"]
        self.assertEqual(engine.cmd_restore_gold(engine.Hub(), None), 0)
        after = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual([m["id"] for m in after["providers"]["deepseek"]["models"]],
                         ["deepseek/gold-0", "deepseek/gold-1", "deepseek/gold-2"])
        self.assertEqual(after["providers"]["deepseek"]["apiKey"], key_before)

    def test_restore_gold_keeps_existing_cost_and_compat(self):
        # 黄金模板只有 id/name，恢复时不能把模型已有的 cost/compat 抹掉
        gold = {"providers": {"deepseek": {"models": [
            {"id": "deepseek/model-1", "name": "Restored", "contextWindow": 128000,
             "maxTokens": 32768, "input": ["text"], "reasoning": True}]}}}
        (engine.HUB_DIR / "providers.gold.json").write_text(json.dumps(gold), encoding="utf-8")
        self.assertEqual(engine.cmd_restore_gold(engine.Hub(), None), 0)
        model = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))["providers"]["deepseek"]["models"][0]
        self.assertEqual(model["cost"], {"input": 0.5})
        self.assertEqual(model["compat"], {"custom": True})


class ConsoleOutputTests(unittest.TestCase):
    def test_non_encodable_glyphs_degrade(self):
        import io
        import sys
        original = sys.stdout
        try:
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
            self.assertEqual(engine.console_safe("✓ pi"), "[OK] pi")
            self.assertEqual(engine.console_safe("✗ dsh"), "[FAIL] dsh")
            self.assertEqual(engine.console_safe("中文 ok"), "中文 ok")
        finally:
            sys.stdout = original


class CustomProviderTests(unittest.TestCase):
    """\u6309 Agent \u8986\u76d6\u7684\u8fde\u63a5\uff1aagents.<agent> \u7684\u7aef\u70b9 / \u5bc6\u94a5 / \u534f\u8bae / \u6a21\u578b\u4e0e\u900f\u4f20\u5b57\u6bb5\u3002

    \u80cc\u666f\uff1a\u53ea\u63d0\u4f9b Anthropic \u517c\u5bb9\u7aef\u70b9\u7684\u7b2c\u4e09\u65b9\u7f51\u5173\uff08\u65e0 OpenAI /v1\uff09\u65e0\u6cd5\u7528
    ANTHROPIC_* \u90a3\u4e00\u7ec4\u56fa\u5b9a\u952e\u8868\u8fbe\u5b8c\uff0c\u56e0\u6b64\u8fde\u63a5\u652f\u6301\u6309\u58f0\u660e\u539f\u6837\u900f\u4f20\u3002
    \u540c\u4e00\u4f9b\u5e94\u5546\u8fd8\u53ef\u4ee5\u7ed9 Claude \u4e0e Codex \u914d\u4e0d\u540c\u7684 key \u4e0e\u6a21\u578b\u3002
    """

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def custom(self, pid="tokenfree", model="claude-sonnet-4-6", **over):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        p = {
            "name": "TokenFree", "displayName": "TokenFree (Anthropic proxy)",
            "baseUrl": "https://api.tokenfree.shop/v1",
            "apiKey": "sk-openai-key", "api": "openai-completions",
            "enabled": True, "targets": ["claude", "codex"],
            "agents": {
                "claude": {
                    "baseUrl": "https://api.tokenfree.shop/v1",
                    "api": "anthropic-messages",
                    "apiKey": "sk-test-not-a-real-key",
                    "models": [{"id": "claude-sonnet-4-6", "name": "Sonnet"},
                               {"id": "claude-opus-5-5", "name": "Opus"}],
                    "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                            "ENABLE_TOOL_SEARCH": "true"},
                    "settings": {"model": "claude-sonnet-4-6", "effortLevel": "high",
                                 "skipDangerousModePermissionPrompt": True},
                },
                "codex": {
                    "baseUrl": "https://api.tokenfree.shop/v1",
                    "api": "openai-responses",
                    "apiKey": "sk-codex-key",
                    "models": [{"id": "gpt-6-astra", "name": "Astra"}],
                },
            },
        }
        p.update(over)
        cfg["providers"][pid] = p
        # Claude Code 与其它 Agent 一样只认 modelSync
        cfg["modelSync"] = {"overrides": {"claude": {"provider": pid, "model": model}}}
        engine.save_json(server.HUB_CONFIG, cfg)
        return p

    def claude_settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_claude_passthrough_writes_env_and_top_level_settings(self):
        self.custom()
        engine.Hub().push_claude()
        data = self.claude_settings()
        # \u56fa\u5b9a\u7ba1\u7684\u952e
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "https://api.tokenfree.shop/v1")
        self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], "sk-test-not-a-real-key")
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "claude-sonnet-4-6")
        # \u58f0\u660e\u5f0f\u900f\u4f20\u7684 env
        self.assertEqual(data["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"], "1")
        self.assertEqual(data["env"]["ENABLE_TOOL_SEARCH"], "true")
        # \u58f0\u660e\u5f0f\u900f\u4f20\u7684\u9876\u5c42 settings
        self.assertEqual(data["model"], "claude-sonnet-4-6")
        self.assertEqual(data["effortLevel"], "high")
        self.assertIs(data["skipDangerousModePermissionPrompt"], True)

    def test_passthrough_preserves_handwritten_keys(self):
        self.custom()
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {"keep": True}, "statusLine": "mine", "modelSettings": {"a": 1},
            "model": "hand-set",
            "env": {"KEEP_ME": "yes"},
        }, ensure_ascii=False), encoding="utf-8")
        engine.Hub().push_claude()
        data = self.claude_settings()
        self.assertEqual(data["hooks"], {"keep": True})
        self.assertEqual(data["statusLine"], "mine")
        self.assertEqual(data["modelSettings"], {"a": 1})
        self.assertEqual(data["env"]["KEEP_ME"], "yes")
        # \u8fde\u63a5\u663e\u5f0f\u58f0\u660e\u4e86 model\uff0c\u6240\u4ee5\u8986\u76d6\u624b\u5de5\u503c\uff1b\u672a\u58f0\u660e\u7684\u5b57\u6bb5\u4e0d\u52a8
        self.assertEqual(data["model"], "claude-sonnet-4-6")

    def test_switching_provider_removes_only_hub_written_keys(self):
        self.custom()
        engine.Hub().push_claude()
        self.assertIn("effortLevel", self.claude_settings())
        # \u7528\u6237\u624b\u5de5\u52a0\u7684\u952e\u5fc5\u987b\u6d3b\u4e0b\u6765
        data = self.claude_settings()
        data["keepMine"] = 1
        (self.home / ".claude" / "settings.json").write_text(json.dumps(data), encoding="utf-8")
        # \u5207\u5230 deepseek\uff08\u5b83\u7684 Claude \u8fde\u63a5\u4e0d\u58f0\u660e claudeSettings\uff09
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"overrides": {"claude": {"provider": "deepseek",
                                                    "model": "deepseek/model-1"}}}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        after = self.claude_settings()
        # \u4e0a\u4e00\u4e2a\u8fde\u63a5\u5199\u5165\u7684\u952e\u88ab\u6536\u56de\uff0c\u907f\u514d\u6b8b\u7559\u522b\u5bb6\u7684 model / effortLevel
        self.assertNotIn("effortLevel", after)
        self.assertNotIn("skipDangerousModePermissionPrompt", after)
        self.assertNotIn("ENABLE_TOOL_SEARCH", after["env"])
        self.assertEqual(after["keepMine"], 1)
        self.assertEqual(after["env"]["ANTHROPIC_BASE_URL"],
                         "https://api.deepseek.com/anthropic")

    def test_second_push_same_provider_is_idempotent(self):
        self.custom()
        engine.Hub().push_claude()
        engine.Hub().push_claude()
        data = self.claude_settings()
        self.assertEqual(data["model"], "claude-sonnet-4-6")
        self.assertEqual(data["effortLevel"], "high")
        # \u540c\u4e00\u4e2a\u8fde\u63a5\u91cd\u590d push \u4e0d\u4f1a\u8bef\u5220\u81ea\u5df1\u5199\u8fc7\u7684\u952e
        self.assertEqual(data["env"]["ENABLE_TOOL_SEARCH"], "true")

    def test_switching_provider_drops_stale_env_aliases(self):
        """\u5207\u5230\u4e0d\u58f0\u660e fable \u7684\u4f9b\u5e94\u5546\uff0c\u4e0a\u4e00\u5bb6\u7684 ANTHROPIC_DEFAULT_FABLE_MODEL \u5fc5\u987b\u6d88\u5931\u3002

        \u771f\u5b9e\u4e8b\u6545\uff1adeepseek \u58f0\u660e\u4e86 fable\uff0c\u5207\u5230 tokenfree\uff08\u4e0d\u58f0\u660e\uff09\u540e
        ANTHROPIC_DEFAULT_FABLE_MODEL \u4ecd\u6b8b\u7559 deepseek \u7684\u6a21\u578b\uff0c\u6307\u5411\u4e00\u4e2a
        tokenfree \u6839\u672c\u4e0d\u63d0\u4f9b\u7684\u6a21\u578b\u3002
        """
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"overrides": {"claude": {
            "provider": "deepseek", "model": "deepseek/model-1",
            "tiers": {"fable": {"provider": "deepseek", "model": "deepseek/model-2"},
                      "haiku": {"provider": "deepseek", "model": "deepseek/model-3"}}}}}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        env = self.claude_settings()["env"]
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "deepseek/model-2")
        # \u7528\u6237\u624b\u5de5\u52a0\u7684 env \u952e\u5fc5\u987b\u6d3b\u4e0b\u6765
        env["MY_OWN_ENV"] = "keep"
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"env": env, **{k: v for k, v in self.claude_settings().items() if k != "env"}},
            ensure_ascii=False), encoding="utf-8")
        # \u5207\u5230\u4e0d\u58f0\u660e fable \u7684 tokenfree
        self.custom()
        engine.Hub().push_claude()
        env = self.claude_settings()["env"]
        self.assertNotIn("ANTHROPIC_DEFAULT_FABLE_MODEL", env)
        self.assertNotIn("ANTHROPIC_DEFAULT_FABLE_MODEL_NAME", env)
        self.assertNotIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", env)   # \u672a\u58f0\u660e\u7684\u6863\u4f4d\u4e00\u5e76\u6536\u56de
        self.assertEqual(env["MY_OWN_ENV"], "keep")  # hub \u6ca1\u5199\u8fc7\uff0c\u4e0d\u6536\u56de

    def test_repeat_push_same_provider_keeps_env_aliases(self):
        self.custom()
        engine.Hub().push_claude()
        engine.Hub().push_claude()
        env = self.claude_settings()["env"]
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-sonnet-4-6")
        self.assertEqual(env["ENABLE_TOOL_SEARCH"], "true")

    def test_targets_restrict_provider_to_declared_platforms(self):
        self.custom(targets=["claude"])
        (self.home / ".pi" / "agent").mkdir(parents=True, exist_ok=True)
        h = engine.Hub()
        self.assertIn("tokenfree", h.providers_for("claude"))
        self.assertNotIn("tokenfree", h.providers_for("pi"))
        self.assertNotIn("tokenfree", h.providers_for("codex"))
        # \u672a\u58f0\u660e targets \u7684 provider \u4ecd\u8fdb\u5165\u6240\u6709\u5e73\u53f0
        self.assertIn("deepseek", h.providers_for("pi"))
        h.push_pi()
        pi = json.loads((self.home / ".pi" / "agent" / "models.json").read_text(encoding="utf-8"))
        self.assertNotIn("tokenfree", pi["providers"])
        self.assertIn("deepseek", pi["providers"])

    def test_switch_claude_model_via_modelsync(self):
        """\u754c\u9762\u4e0a\u6539\u6a21\u578b\u73b0\u5728\u843d\u5728 modelSync\uff08\u4e0e\u5176\u5b83 Agent \u540c\u4e00\u5957\u673a\u5236\uff09\u3002"""
        self.custom()
        server.api_update_modelsync({"modelSync": {"overrides": {"claude": {
            "provider": "tokenfree", "model": "claude-opus-5-5",
            "tiers": {"opus": {"provider": "tokenfree", "model": "claude-opus-5-5"},
                      "sonnet": {"provider": "tokenfree", "model": "claude-opus-5-5"},
                      "haiku": {"provider": "tokenfree", "model": "claude-opus-5-5"},
                      "subagent": {"provider": "tokenfree", "model": "claude-opus-5-5"}}}}}})
        engine.Hub().push_claude()
        data = self.claude_settings()
        self.assertEqual(data["model"], "claude-opus-5-5")
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "claude-opus-5-5")
        self.assertEqual(data["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-opus-5-5")
        self.assertEqual(data["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-opus-5-5")
        self.assertEqual(data["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "claude-opus-5-5")

    def test_model_must_exist_in_that_connection(self):
        """Claude \u8fde\u63a5\u7684\u6a21\u578b\u5e93\u91cc\u6ca1\u6709\u7684\u6a21\u578b\u4f1a\u88ab\u6807\u8bb0\u4e3a\u624b\u586b\u503c\u3002"""
        self.custom(model="gpt-6-astra")
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        # gpt-6-astra \u53ea\u5728\u9ed8\u8ba4 / Codex \u8fde\u63a5\u4e0a\uff0cClaude \u8fde\u63a5\u6ca1\u6709
        cfg["providers"]["tokenfree"]["agents"]["codex"]["models"] = [
            {"id": "gpt-6-astra", "name": "Astra"}]
        cfg["providers"]["tokenfree"]["models"] = [{"id": "gpt-6-astra", "name": "Astra"}]
        cfg["modelSync"]["overrides"]["codex"] = {"provider": "tokenfree", "model": "gpt-6-astra"}
        engine.save_json(server.HUB_CONFIG, cfg)
        resolved = engine.resolve_agent_model(cfg, "claude")
        self.assertEqual(resolved["model"], "gpt-6-astra")
        self.assertFalse(resolved["known"])
        # Codex \u8fde\u63a5\u91cc\u6709\u8fd9\u4e2a\u6a21\u578b\uff0c\u6240\u4ee5\u5bf9\u5b83\u662f\u5df2\u77e5
        self.assertTrue(engine.resolve_agent_model(cfg, "codex")["known"])

    def test_validation_rejects_invalid_agent_overrides(self):
        base = {"name": "x", "baseUrl": "https://a.example.com/v1", "apiKey": "k",
                "api": "openai-completions"}
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "agents": {"claude": {"api": "openai-completions"}}})
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "agents": {"codex": {"api": "openai-chat-completions"}}})
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "agents": {"nope": {}}})
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "agents": {"claude": "nope"}})
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "agents": {"claude": {"models": "nope"}}})
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "targets": "claude"})
        # \u5408\u6cd5\u58f0\u660e\u901a\u8fc7
        server._validate_provider({**base, "targets": ["claude"],
                                   "agents": {"claude": {"baseUrl": "https://a.example/v1",
                                                        "api": "anthropic-messages",
                                                        "env": {"A": "1"},
                                                        "settings": {"model": "m"}}}})


class CcSwitchDbTypeTests(unittest.TestCase):
    """cc-switch 的 providers.created_at 是 INTEGER（epoch 毫秒）列。

    app 启动 setup 阶段按整数读该列；TEXT 值直接导致
    "Invalid column type Text at index: 5, name: created_at" 崩溃，app 打不开。
    hub 的 push_claude / push_cc_switch 都必须写整数。
    """

    SCHEMA = """
        CREATE TABLE providers (
            id TEXT PRIMARY KEY,
            app_type TEXT NOT NULL,
            name TEXT NOT NULL,
            settings_config TEXT NOT NULL,
            website_url TEXT,
            category TEXT,
            created_at INTEGER,
            sort_index INTEGER,
            notes TEXT,
            icon TEXT,
            icon_color TEXT,
            meta TEXT NOT NULL DEFAULT '{}',
            is_current BOOLEAN NOT NULL DEFAULT 0,
            in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
            cost_multiplier TEXT NOT NULL DEFAULT '1.0',
            limit_daily_usd TEXT,
            limit_monthly_usd TEXT,
            provider_type TEXT
        )
    """

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.db_path = self.home / ".cc-switch" / "cc-switch.db"
        self.db_path.parent.mkdir(parents=True)
        con = sqlite3.connect(str(self.db_path))
        with con:
            con.execute(self.SCHEMA)
            # 预置一行 app 自己写的整数时间戳，模拟真实库
            con.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, created_at)"
                " VALUES ('claude-official', 'claude', 'Claude Official', '{}', 1776130929218)")
        con.close()

    def types(self) -> list[tuple]:
        con = sqlite3.connect(str(self.db_path))
        try:
            return con.execute(
                "SELECT id, typeof(created_at), created_at FROM providers WHERE id LIKE 'hub-%'"
            ).fetchall()
        finally:
            con.close()

    def test_claude_never_touches_cc_switch_database(self):
        before = self.db_path.read_bytes()
        engine.Hub().push_claude()
        self.assertEqual(before, self.db_path.read_bytes())
        self.assertFalse(self.types())

    def test_cc_switch_rows_use_integer_epoch_ms(self):
        engine.Hub().push_cc_switch()
        rows = [r for r in self.types() if r[0].startswith(("hub-pi-", "hub-mcode-"))]
        self.assertTrue(rows)
        for row_id, type_name, value in rows:
            self.assertEqual(type_name, "integer", row_id)
            self.assertIsInstance(value, int, row_id)
            self.assertGreater(value, 1_600_000_000_000, row_id)

    def test_no_text_created_at_after_push(self):
        engine.Hub().push_claude()
        engine.Hub().push_cc_switch()
        con = sqlite3.connect(str(self.db_path))
        try:
            n = con.execute(
                "SELECT count(*) FROM providers WHERE typeof(created_at) = 'text'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 0)

    def test_push_fails_loud_on_legacy_text_rows(self):
        """历史遗留 TEXT 行会让 cc-switch 打不开，hub 必须报错而不是静默跳过。"""
        con = sqlite3.connect(str(self.db_path))
        with con:
            con.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, created_at)"
                " VALUES ('legacy-text-1', 'claude', 'Legacy', '{}', datetime('now'))")
        con.close()
        with self.assertRaises(RuntimeError) as ctx:
            engine.Hub().push_cc_switch()
        self.assertIn("fix_cc_switch_created_at", str(ctx.exception))


class ClaudeFeatureEnvTests(unittest.TestCase):
    """平台级特性开关（sync.targets.claude.featureEnv）。

    背景：Agent Teams 由 ~/.claude/settings.json 的
    CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 开启，它属于 Claude Code 的平台能力，
    不属于任何 provider。此前只写在 tokenfree 的 claudeEnv 里，用户在 cc-switch
    切到其它预设后 settings.json 被预设的 settings_config 整份覆盖，特性即丢失。
    featureEnv 让这类开关每次同步都写入 settings.json，并注入 cc-switch 的
    每一个 claude 预设（含 app 自建与 GUI 手建条目）。
    """

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.db_path = self.home / ".cc-switch" / "cc-switch.db"
        self.db_path.parent.mkdir(parents=True)
        con = sqlite3.connect(str(self.db_path))
        with con:
            con.execute(CcSwitchDbTypeTests.SCHEMA)
            # app 自建预设：不带特性开关
            con.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, created_at)"
                " VALUES ('claude-official', 'claude', 'Claude Official', ?, 1776130929218)",
                (json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}}),))
            # GUI 手建预设：同样不带特性开关
            con.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, created_at)"
                " VALUES ('gui-made', 'claude', 'DeepSeek', ?, 1785567574833)",
                (json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}}),))
        con.close()

    def feature(self, **flags):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["sync"]["targets"]["claude"]["featureEnv"] = (
            flags or {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"})
        engine.save_json(server.HUB_CONFIG, cfg)

    def claude_settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def presets(self):
        con = sqlite3.connect(str(self.db_path))
        try:
            return con.execute(
                "SELECT id, settings_config FROM providers WHERE app_type='claude'"
            ).fetchall()
        finally:
            con.close()

    def test_feature_env_reaches_settings_without_mutating_other_tools(self):
        self.feature()
        before = self.db_path.read_bytes()
        engine.Hub().push_claude()
        self.assertEqual(self.claude_settings()["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"], "1")
        self.assertEqual(before, self.db_path.read_bytes())

    def test_feature_env_survives_provider_switch(self):
        # deepseek 自己声明了同名开关；切到不声明的 openrouter 后也必须保留
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["providers"]["deepseek"]["agents"]["claude"]["env"] = {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}
        cfg["providers"]["openrouter"]["agents"]["claude"] = {
            "baseUrl": "https://openrouter.ai/api", "api": "anthropic-messages"}
        engine.save_json(server.HUB_CONFIG, cfg)
        self.feature()
        engine.Hub().push_claude()
        self.assertEqual(
            self.claude_settings()["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"], "1")
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["modelSync"] = {"overrides": {"claude": {"provider": "openrouter",
                                                     "model": "openrouter/model-1"}}}
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        # 开关由平台配置兜底，不随 provider 声明与否消失
        self.assertEqual(
            self.claude_settings()["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"], "1")

    def test_absent_feature_env_changes_nothing(self):
        engine.Hub().push_claude()
        for row_id, payload in self.presets():
            env = json.loads(payload)["env"]
            self.assertNotIn("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", env, row_id)
        self.assertEqual(
            json.loads(self.presets()[0][1])["env"]["ANTHROPIC_BASE_URL"],
            "https://api.anthropic.com")

    def test_repeat_push_is_idempotent(self):
        self.feature()
        engine.Hub().push_claude()
        before = dict(self.presets())
        engine.Hub().push_claude()
        after = dict(self.presets())
        self.assertEqual(before, after)

    def test_push_fails_loud_on_broken_preset(self):
        con = sqlite3.connect(str(self.db_path))
        with con:
            con.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, created_at)"
                " VALUES ('broken-env', 'claude', 'Broken', '{\"env\": \"weird\"}', 1780000000000)")
        con.close()
        self.feature()
        before = self.db_path.read_bytes()
        engine.Hub().push_claude()
        self.assertEqual(before, self.db_path.read_bytes())

    def test_feature_env_values_are_strings(self):
        values = engine.Hub._feature_env_values(
            {"A": "1", "B": True, "C": False, "D": 3})
        self.assertEqual(values, {"A": "1", "B": "true", "C": "false", "D": "3"})


if __name__ == "__main__":
    unittest.main()
