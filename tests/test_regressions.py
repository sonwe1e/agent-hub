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
    """自定义供应商接入：claudeEnv / claudeSettings / targets 的声明式透传。

    背景：只提供 Anthropic 兼容端点的第三方网关（无 OpenAI /v1）无法用
    ANTHROPIC_* 那一组固定键表达完，因此 provider 支持按声明原样透传。
    """

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        # 本类验证「显式切换 Claude 供应商 / 模型」，而多 agent 全局模型（modelSync）
        # 的既定语义是让 Claude 跟随全局选择，会盖掉这里要验证的切换动作。
        self.without_modelsync()

    def custom(self, pid="tokenfree", **over):
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        p = {
            "name": "TokenFree", "displayName": "TokenFree (Anthropic proxy)",
            "baseUrl": "https://api.tokenfree.shop",
            "anthropicBaseUrl": "https://api.tokenfree.shop",
            "apiKey": "sk-test-not-a-real-key", "api": "openai-completions",
            "enabled": True, "targets": ["claude"],
            "claudeModels": {"model": "claude-sonnet-4-6", "sonnet": "claude-sonnet-4-6"},
            "claudeEnv": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                          "ENABLE_TOOL_SEARCH": "true"},
            "claudeSettings": {"model": "claude-sonnet-4-6", "effortLevel": "high",
                               "skipDangerousModePermissionPrompt": True},
        }
        p.update(over)
        cfg["providers"][pid] = p
        cfg["sync"]["targets"]["claude"]["activeProvider"] = pid
        engine.save_json(server.HUB_CONFIG, cfg)
        return p

    def without_modelsync(self):
        """清掉多 agent 全局模型。

        这些用例验证的是「显式切换 Claude 供应商」：全局主模型的供应商同样具备
        Anthropic 端点时，同步会把 activeProvider 切回全局那家（多 agent 对齐的
        既定语义），会盖掉本用例要验证的切换动作，所以先清掉。
        """
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg.pop("modelSync", None)
        engine.save_json(server.HUB_CONFIG, cfg)

    def claude_settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_claude_passthrough_writes_env_and_top_level_settings(self):
        self.custom()
        engine.Hub().push_claude()
        data = self.claude_settings()
        # 固定管的键
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "https://api.tokenfree.shop")
        self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], "sk-test-not-a-real-key")
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "claude-sonnet-4-6")
        # 声明式透传的 env
        self.assertEqual(data["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"], "1")
        self.assertEqual(data["env"]["ENABLE_TOOL_SEARCH"], "true")
        # 声明式透传的顶层 settings
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
        # provider 显式声明了 model，所以覆盖手工值；未声明的字段不动
        self.assertEqual(data["model"], "claude-sonnet-4-6")

    def test_switching_provider_removes_only_hub_written_keys(self):
        self.without_modelsync()
        self.custom()
        engine.Hub().push_claude()
        self.assertIn("effortLevel", self.claude_settings())
        # 用户手工加的键必须活下来
        data = self.claude_settings()
        data["keepMine"] = 1
        (self.home / ".claude" / "settings.json").write_text(json.dumps(data), encoding="utf-8")
        # 切回 deepseek（它不声明 claudeSettings）
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["sync"]["targets"]["claude"]["activeProvider"] = "deepseek"
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        after = self.claude_settings()
        # 上一个 provider 写入的键被收回，避免残留别家的 model/effortLevel
        self.assertNotIn("effortLevel", after)
        self.assertNotIn("skipDangerousModePermissionPrompt", after)
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
        # 同一个 provider 重复 push 不会误删自己写过的键
        self.assertEqual(data["env"]["ENABLE_TOOL_SEARCH"], "true")

    def test_switching_provider_drops_stale_env_aliases(self):
        """切到不声明 fable 的供应商，上一家的 ANTHROPIC_DEFAULT_FABLE_MODEL 必须消失。

        真实事故：deepseek 声明了 fable，切到 tokenfree（不声明）后
        ANTHROPIC_DEFAULT_FABLE_MODEL 仍残留 deepseek-flash，指向一个
        tokenfree 根本不提供的模型。
        """
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        # 先以 deepseek 为激活方；fixture 里的 deepseek 没有 claudeModels，先补一个带 fable 的
        cfg["providers"]["deepseek"]["claudeModels"] = {
            "model": "deepseek-v4-flash", "sonnet": "deepseek-v4-flash",
            "haiku": "deepseek-v4-flash", "fable": "deepseek-flash",
        }
        cfg.pop("modelSync", None)   # 见 without_modelsync 的说明
        engine.save_json(server.HUB_CONFIG, cfg)
        engine.Hub().push_claude()
        self.assertEqual(self.claude_settings()["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"], "deepseek-flash")
        # 用户手工加的 env 键必须活下来
        data = self.claude_settings()
        data["env"]["MY_OWN_ENV"] = "keep"
        (self.home / ".claude" / "settings.json").write_text(json.dumps(data), encoding="utf-8")
        # 切到不声明 fable 的 tokenfree
        self.custom()
        engine.Hub().push_claude()
        env = self.claude_settings()["env"]
        self.assertNotIn("ANTHROPIC_DEFAULT_FABLE_MODEL", env)
        self.assertNotIn("ANTHROPIC_DEFAULT_FABLE_MODEL_NAME", env)
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-sonnet-4-6")
        self.assertEqual(env["MY_OWN_ENV"], "keep")  # hub 没写过，不收回

    def test_repeat_push_same_provider_keeps_env_aliases(self):
        self.custom()
        engine.Hub().push_claude()
        engine.Hub().push_claude()
        env = self.claude_settings()["env"]
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-sonnet-4-6")
        self.assertEqual(env["ENABLE_TOOL_SEARCH"], "true")

    def test_targets_restrict_provider_to_declared_platforms(self):
        self.custom()
        (self.home / ".pi" / "agent").mkdir(parents=True, exist_ok=True)
        h = engine.Hub()
        self.assertIn("tokenfree", h.providers_for("claude"))
        self.assertNotIn("tokenfree", h.providers_for("pi"))
        # 未声明 targets 的 provider 仍进入所有平台
        self.assertIn("deepseek", h.providers_for("pi"))
        h.push_pi()
        pi = json.loads((self.home / ".pi" / "agent" / "models.json").read_text(encoding="utf-8"))
        self.assertNotIn("tokenfree", pi["providers"])
        self.assertIn("deepseek", pi["providers"])

    def test_switch_claude_model_via_provider_update(self):
        """界面上「保存平台设置」做的事：改 provider 的 claudeModels 再 push。

        模型切换落在 provider 上而不是平台配置里，四个档位统一指向新模型。
        """
        self.custom()
        server.api_update_provider("tokenfree", {"claudeModels": {
            "model": "claude-opus-5-5", "opus": "claude-opus-5-5",
            "sonnet": "claude-opus-5-5", "haiku": "claude-opus-5-5",
            "subagent": "claude-opus-5-5"}})
        engine.Hub().push_claude()
        data = self.claude_settings()
        self.assertEqual(data["model"], "claude-opus-5-5")
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "claude-opus-5-5")
        self.assertEqual(data["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-opus-5-5")
        self.assertEqual(data["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-opus-5-5")
        self.assertEqual(data["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "claude-opus-5-5")
        # claudeSettings.model 也声明过，一起跟上
        self.assertEqual(data["model"], "claude-opus-5-5")

    def test_switch_claude_model_drops_stale_alias_values(self):
        """切换到没有 fable 档的模型后，上一档的 fable 别名不能残留。"""
        self.custom()
        server.api_update_provider("tokenfree", {"claudeModels": {
            "model": "claude-sonnet-5", "sonnet": "claude-sonnet-5"}})
        engine.Hub().push_claude()
        env = self.claude_settings()["env"]
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-sonnet-5")
        self.assertNotIn("ANTHROPIC_DEFAULT_FABLE_MODEL", env)
        self.assertNotIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", env)  # 未声明的档位一并收回

    def test_validation_rejects_invalid_custom_fields(self):
        base = {"name": "x", "baseUrl": "https://a.example.com/v1", "apiKey": "k",
                "api": "openai-completions"}
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "claudeEnv": "nope"})
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "claudeSettings": []})
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "targets": "claude"})
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "targets": ["no-such-target"]})
        with self.assertRaises(ValueError):
            server._validate_provider({**base, "claudeModels": {"model": 1}})
        # 合法声明通过
        server._validate_provider({**base, "targets": ["claude"], "claudeEnv": {"A": "1"},
                                   "claudeSettings": {"model": "m"},
                                   "claudeModels": {"model": "m"}})


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
        cfg["providers"]["deepseek"]["claudeEnv"] = {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}
        cfg["providers"]["openrouter"]["anthropicBaseUrl"] = "https://openrouter.ai/api"
        engine.save_json(server.HUB_CONFIG, cfg)
        self.feature()
        engine.Hub().push_claude()
        self.assertEqual(
            self.claude_settings()["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"], "1")
        cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        cfg["sync"]["targets"]["claude"]["activeProvider"] = "openrouter"
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
