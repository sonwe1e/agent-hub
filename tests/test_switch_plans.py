# -*- coding: utf-8 -*-
"""切换方案（switchPlans）回归：一次选择、多 Agent 应用、真实原生产物。

所有用例都在隔离 HOME 里跑合成配置，不接触真实客户端与真实密钥，
也不发起任何供应商调用。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import engine, isolated
import server
import switch_plans as sp
import sync_transaction as transaction


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class SwitchPlanFixture(unittest.TestCase):
    """公共夹具与断言助手；不含测试方法，避免被继承时重复执行。"""

    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        transaction._PLANS.clear()
        self.addCleanup(transaction._PLANS.clear)

    # -- 夹具 --
    def config(self):
        return json.loads(engine.HUB_CONFIG.read_text(encoding="utf-8"))

    def save(self, cfg):
        engine.HUB_CONFIG.write_text(json.dumps(cfg), encoding="utf-8")

    def enable(self, *targets):
        cfg = self.config()
        for name, entry in cfg["sync"]["targets"].items():
            entry["enabled"] = name in targets
        self.save(cfg)
        return cfg

    def set_target_files(self, name, *files):
        cfg = self.config()
        cfg["sync"]["targets"][name]["files"] = [str(path) for path in files]
        self.save(cfg)
        return cfg

    def write_claude_settings(self, extra: dict | None = None):
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"env": {"ANTHROPIC_MODEL": "old-model", "MY_OWN_ENV": "keep-me"},
                "hooks": {"PreToolUse": [{"matcher": "Bash"}]}, "statusLine": {"type": "command"}}
        data.update(extra or {})
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def write_pi_settings(self):
        path = self.home / ".pi" / "agent" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"theme": "dark", "defaultThinkingLevel": "xhigh",
                                    "defaultModel": "keep-this"}), encoding="utf-8")
        return path

    def plan(self, raw):
        """原样返回方案请求体：服务端自己做归一化，测试走真实入口。"""
        return raw

    def normalized(self, raw):
        """归一化后的方案：给纯函数（compile / analyze / reference）用。"""
        return sp.normalize_plan(raw)


class SwitchPlanTestCase(SwitchPlanFixture):
    """切换方案的核心行为：按连接校验、取值语义、模式边界、失效引用、事务与恢复。"""

    def test_preview_stages_hub_and_native_config_and_restore_recovers_both(self):
        self.enable("pi")
        native = self.write_pi_settings()
        before_config = engine.HUB_CONFIG.read_bytes()
        before_native = native.read_bytes()
        plan = {"name": "暂存", "targets": ["pi"],
                "agents": {"pi": {"policy": "fixed", "provider": "deepseek",
                                    "model": "deepseek/model-2"}}}
        preview = server.api_switch_plan_preview({"plan": plan})
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(engine.HUB_CONFIG.read_bytes(), before_config)
        self.assertEqual(native.read_bytes(), before_native)
        self.assertIn(str(engine.HUB_CONFIG), [row["path"] for row in preview["files"]])
        applied = server.api_apply({"planId": preview["planId"]})
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(self.config()["modelSync"]["overrides"]["pi"]["model"], "deepseek/model-2")
        self.assertEqual(read_json(native)["defaultModel"], "deepseek/model-2")
        server.api_restore({"transactionId": applied["transactionId"]})
        self.assertEqual(engine.HUB_CONFIG.read_bytes(), before_config)
        self.assertEqual(native.read_bytes(), before_native)

    def test_failed_preview_does_not_persist_model_selection(self):
        self.enable("pi")  # dsh is disabled.
        before = engine.HUB_CONFIG.read_bytes()
        plan = {"name": "未启用", "targets": ["dsh"],
                "agents": {"dsh": {"policy": "fixed", "provider": "deepseek",
                                     "model": "deepseek/model-2"}}}
        with self.assertRaisesRegex(transaction.TransactionError, "not explicitly enabled"):
            server.api_switch_plan_preview({"plan": plan})
        self.assertEqual(engine.HUB_CONFIG.read_bytes(), before)

    def test_hub_config_write_failure_rolls_back_native_changes(self):
        self.enable("pi")
        native = self.write_pi_settings()
        before_config = engine.HUB_CONFIG.read_bytes()
        before_native = native.read_bytes()
        plan = {"name": "回滚", "targets": ["pi"],
                "agents": {"pi": {"policy": "fixed", "provider": "deepseek",
                                    "model": "deepseek/model-2"}}}
        preview = server.api_switch_plan_preview({"plan": plan})
        original = transaction._atomic_write
        def fail_config(path, data, mode=None):
            if path == engine.HUB_CONFIG:
                raise OSError("simulated Hub config write failure")
            return original(path, data, mode)
        with patch.object(transaction, "_atomic_write", side_effect=fail_config):
            with self.assertRaisesRegex(transaction.TransactionError, "rolled back"):
                server.api_apply({"planId": preview["planId"]})
        self.assertEqual(engine.HUB_CONFIG.read_bytes(), before_config)
        self.assertEqual(native.read_bytes(), before_native)

    def test_switch_transaction_restores_config_outside_home_with_custom_filename(self):
        self.enable("pi")
        self.write_pi_settings()
        with tempfile.TemporaryDirectory(prefix='hub-external-config-') as directory:
            config = Path(directory) / 'my-config.jsonc'
            config.write_bytes(engine.HUB_CONFIG.read_bytes())
            before = config.read_bytes()
            with patch.object(engine, 'HUB_CONFIG', config), patch.object(server, 'HUB_CONFIG', config):
                plan = {"name": "外部配置", "targets": ["pi"],
                        "agents": {"pi": {"policy": "fixed", "provider": "deepseek",
                                            "model": "deepseek/model-2"}}}
                preview = server.api_switch_plan_preview({"plan": plan})
                self.assertEqual(config.read_bytes(), before)
                applied = server.api_apply({"planId": preview["planId"]})
                self.assertNotEqual(config.read_bytes(), before)
                server.api_restore({"transactionId": applied["transactionId"]})
                self.assertEqual(config.read_bytes(), before)

    def test_opencode_can_be_enabled_previewed_applied_and_restored_with_gateway_on(self):
        self.assertNotIn("opencode", self.config()["sync"]["targets"])
        self.assertFalse(server.api_status()["targets"]["opencode"]["enabled"])
        server.api_update_targets({"targets": {"opencode": {"enabled": True}}})
        cfg = self.config()
        cfg["gateway"] = {"enabled": True}
        self.save(cfg)
        before = engine.HUB_CONFIG.read_bytes()
        native = self.home / ".config" / "opencode" / "opencode.json"
        native.parent.mkdir(parents=True)
        native.write_text(json.dumps({"mcp": {"kept": {"type": "local", "command": ["echo"]}}}), encoding="utf-8")
        before_native = native.read_bytes()
        plan = {"name": "OpenCode", "targets": ["opencode"],
                "agents": {"opencode": {"policy": "fixed", "provider": "deepseek",
                                         "model": "deepseek/model-2"}}}
        preview = server.api_switch_plan_preview({"plan": plan})
        self.assertTrue(preview["ok"], preview)
        self.assertTrue(any("替换" in warning and "provider" in warning for warning in preview["warnings"]))
        self.assertEqual(preview["analysis"]["details"][0]["effort"]["support"], "unsupported")
        self.assertEqual(engine.HUB_CONFIG.read_bytes(), before)
        self.assertEqual(native.read_bytes(), before_native)

        applied = server.api_apply({"planId": preview["planId"]})
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(read_json(native)["model"], "deepseek/deepseek/model-2")
        self.assertIn("kept", read_json(native)["mcp"])
        self.assertEqual(self.config()["modelSync"]["overrides"]["opencode"]["model"], "deepseek/model-2")
        server.api_restore({"transactionId": applied["transactionId"]})
        self.assertEqual(engine.HUB_CONFIG.read_bytes(), before)
        self.assertEqual(native.read_bytes(), before_native)

    def test_opencode_uses_the_connection_specific_model_catalog(self):
        cfg = self.config()
        provider = cfg["providers"]["deepseek"]
        provider.setdefault("agents", {})["opencode"] = {"models": [{"id": "private-model"}]}
        exported = engine.opencode_providers({"deepseek": provider})
        self.assertEqual(set(exported["deepseek"]["models"]), {"private-model"})
        self.assertNotIn('api', exported['deepseek']['models']['private-model']['provider'])

    # -- 1. 多 Agent 一次应用，核对原生配置产物 --
    def test_one_plan_applies_to_several_agents_and_writes_native_artifacts(self):
        self.enable("pi", "claude")
        self.write_claude_settings()
        self.write_pi_settings()
        plan = self.plan({
            "name": "工作模式", "targets": ["pi", "claude"],
            "base": {"provider": "deepseek", "model": "deepseek/model-2",
                     "reasoningEffort": "high"},
            "agents": {"pi": {"policy": "fixed", "provider": "openrouter",
                              "model": "openrouter/model-3", "reasoningEffort": "low"},
                       "claude": None},
        })
        result = server.api_switch_plan_apply({"plan": plan, "targets": ["pi", "claude"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(sorted(result["written"]), ["claude", "pi"])
        self.assertEqual(result["failed"], [])

        claude = read_json(self.home / ".claude" / "settings.json")
        self.assertEqual(claude["env"]["ANTHROPIC_MODEL"], "deepseek/model-2")
        self.assertEqual(claude["env"]["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        # 用户自己的非托管字段必须原样保留
        self.assertEqual(claude["env"]["MY_OWN_ENV"], "keep-me")
        self.assertIn("hooks", claude)
        self.assertIn("statusLine", claude)

        pi = read_json(self.home / ".pi" / "agent" / "settings.json")
        self.assertEqual(pi["defaultProvider"], "openrouter")
        self.assertEqual(pi["defaultModel"], "openrouter/model-3")
        self.assertEqual(pi["defaultThinkingLevel"], "low")
        self.assertEqual(pi["theme"], "dark")

    # -- 2. 不同连接的模型可用范围不同 --
    def test_model_availability_is_checked_per_connection(self):
        cfg = self.config()
        # deepseek 的 Claude 连接只提供 claude-*；openrouter 的 Codex 连接只提供 gpt-*
        cfg["providers"]["deepseek"]["agents"]["claude"]["models"] = [
            {"id": "deepseek/claude-1"}, {"id": "deepseek/claude-2"}]
        cfg["providers"]["openrouter"]["agents"]["codex"]["models"] = [{"id": "gpt-1"}]
        self.enable("claude", "codex")
        cfg["sync"]["targets"]["codex"]["mode"] = "third-party"
        self.save(cfg)

        # 同一个模型名在 claude 连接上有、在 codex 连接上没有
        plan = self.normalized({"name": "跨连接", "targets": ["claude", "codex"],
                                "base": {"provider": "deepseek", "model": "deepseek/claude-1"},
                                "agents": {"codex": {"policy": "fixed", "provider": "deepseek",
                                                     "model": "deepseek/claude-1"}}})
        report = sp.analyze_plan(self.config(), plan, mode_of=lambda n: "third-party")
        self.assertFalse(report["writable"])
        blocked = {d["target"]: d for d in report["details"]}
        # codex 的连接上没有这个模型 -> 整组不写；claude 自身可用但被整组拦下
        self.assertEqual(blocked["codex"]["status"], "error")
        self.assertIn("模型库里没有", blocked["codex"]["reason"])
        self.assertEqual(blocked["claude"]["status"], "error")
        self.assertIn("整组不写", blocked["claude"]["reason"])
        codes = {i["code"] for i in report["errors"]}
        self.assertIn("model-not-in-connection", codes)

        # 手填放行：明确标记但仍然可写
        allowed = self.normalized({"name": "跨连接-放行", "targets": ["codex"],
                                   "base": {"provider": "deepseek", "model": "deepseek/claude-1"},
                                   "agents": {"codex": {"policy": "fixed", "provider": "deepseek",
                                                        "model": "deepseek/claude-1",
                                                        "allowUnknownModel": True}}})
        report2 = sp.analyze_plan(self.config(), allowed, mode_of=lambda n: "third-party")
        self.assertTrue(report2["writable"])
        self.assertEqual(report2["details"][0]["status"], "warning")
        self.assertIn("model-unknown", {i["code"] for i in report2["warnings"]})

    # -- 3. follow / fixed / keep 与推理强度的覆盖和保留 --
    def test_policy_semantics_control_overrides_and_preservation(self):
        self.enable("pi", "dsh", "claude")
        self.write_pi_settings()
        self.write_claude_settings()
        self.save(self.config())
        # 先有一份 modelSync：pi 单独指定，dsh 跟随全局，claude 有 opus 档位
        current = self.config()
        current["modelSync"] = {
            "default": {"provider": "stepfun", "model": "stepfun/model-1"},
            "overrides": {
                "pi": {"provider": "stepfun", "model": "stepfun/model-1"},
                "claude": {"provider": "deepseek", "model": "deepseek/model-1",
                           "tiers": {"opus": {"provider": "deepseek", "model": "deepseek/model-1"}}},
            },
        }
        self.save(current)

        plan = self.plan({
            "name": "只换 pi 和 dsh", "targets": ["pi", "dsh", "claude"],
            "base": {"provider": "openrouter", "model": "openrouter/model-1"},
            "agents": {
                "pi": {"policy": "fixed", "provider": "stepfun", "model": "stepfun/model-5"},
                "dsh": {"policy": "follow"},
                # 保留客户端当前值：本次完全不碰 Claude Code
                "claude": {"policy": "keep"},
            },
        })
        patched, patch, issues, blocked = sp.compile_plan(self.config(), plan,
                                                          targets=["pi", "dsh", "claude"])
        self.assertEqual(blocked, set())
        self.assertEqual(patch["clear"], [])           # keep -> 不动
        self.assertEqual(patch["overrides"]["pi"]["model"], "stepfun/model-5")
        self.assertEqual(patch["overrides"]["dsh"]["provider"], "openrouter")
        self.assertNotIn("claude", patch["overrides"])          # keep 不写
        # keep 的目标不能被顺手加进 overrides
        self.assertEqual(patched["modelSync"]["overrides"]["claude"], current["modelSync"]["overrides"]["claude"])
        # dsh 原本没有 override，现在跟随 base 写入
        self.assertEqual(patched["modelSync"]["default"]["model"], "stepfun/model-1")

        # 档位：keep 语义不能顺手改掉已经存在的 opus 档位
        self.assertEqual(patched["modelSync"]["overrides"].get("claude"), current["modelSync"]["overrides"]["claude"])

    def test_reasoning_effort_vocabulary_is_per_target(self):
        cfg = self.config()
        cfg["sync"]["targets"]["codex"]["enabled"] = True
        cfg["sync"]["targets"]["codex"]["mode"] = "third-party"
        for name in ("pi", "claude", "dsh"):
            cfg["sync"]["targets"][name]["enabled"] = False
        self.save(cfg)
        # base 用 off（Claude 词表）但 codex 不认 off
        plan = self.plan({"name": "词表冲突", "targets": ["codex"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-1",
                                   "reasoningEffort": "off"}})
        report = sp.analyze_plan(self.config(), plan, mode_of=lambda n: "third-party")
        self.assertFalse(report["writable"])
        self.assertIn("effort-vocabulary", {i["code"] for i in report["errors"]})
        # 单独给 codex 一个它支持的强度就能写
        plan2 = self.plan({"name": "词表对齐", "targets": ["codex"],
                           "base": {"provider": "deepseek", "model": "deepseek/model-1"},
                           "agents": {"codex": {"policy": "fixed", "provider": "deepseek",
                                                "model": "deepseek/model-1",
                                                "reasoningEffort": "none"}}})
        report2 = sp.analyze_plan(self.config(), plan2, mode_of=lambda n: "third-party")
        self.assertTrue(report2["writable"])

    def test_effort_vocabulary_mismatch_is_never_shown_as_applied(self):
        """词表不一致的强度绝不显示成"已成功应用"。

        'none' 只属于 Codex 的词表；pi 收到它必须被拦下，而不是当成写成功。
        """
        self.enable("pi")
        plan = self.normalized({"name": "pi 强度", "targets": ["pi"],
                                "base": {"provider": "deepseek", "model": "deepseek/model-1"},
                                "agents": {"pi": {"policy": "fixed", "provider": "deepseek",
                                                  "model": "deepseek/model-1",
                                                  "reasoningEffort": "none"}}})
        report = sp.analyze_plan(self.config(), plan)
        self.assertFalse(report["writable"])
        self.assertEqual(report["details"][0]["status"], "error")
        self.assertIn("effort-vocabulary", {i["code"] for i in report["errors"]})
        self.assertIn("不接受推理强度 none", report["details"][0]["reason"])
        # 该客户端的原生落点要如实写出来，便于用户核对
        self.assertEqual(report["details"][0]["effort"]["native"],
                         "~/.pi/agent/settings.json 的 defaultThinkingLevel")

    def test_effort_lands_in_each_clients_native_field(self):
        """同一个强度值在不同客户端落到不同原生字段；不能当成同一件事。"""
        self.enable("pi", "dsh")
        raw = {"name": "落点", "targets": ["pi", "dsh"],
               "base": {"provider": "deepseek", "model": "deepseek/model-1",
                        "reasoningEffort": "high"}}
        report = sp.analyze_plan(self.config(), self.normalized(raw))
        self.assertTrue(report["writable"], report)
        native = {d["target"]: d["effort"]["native"] for d in report["details"]}
        self.assertIn("defaultThinkingLevel", native["pi"])
        self.assertIn(".dsh", native["dsh"])
        # 写完之后核对原生产物
        self.write_pi_settings()
        result = server.api_switch_plan_apply({"plan": raw, "targets": ["pi", "dsh"]})
        self.assertTrue(result["ok"], result)
        pi = read_json(self.home / ".pi" / "agent" / "settings.json")
        self.assertEqual(pi["defaultThinkingLevel"], "high")
        dsh = (self.home / ".dsh" / "settings.yaml").read_text(encoding="utf-8")
        self.assertIn("high", dsh)

    # -- 4. 官方 / 第三方模式边界 --
    def test_official_mode_is_reported_as_skipped_not_written(self):
        cfg = self.config()
        cfg["sync"]["targets"]["codex"]["enabled"] = True
        cfg["sync"]["targets"]["codex"]["mode"] = "official"
        cfg["sync"]["targets"]["zcode"]["enabled"] = True
        cfg["sync"]["targets"]["zcode"]["mode"] = "official"
        plan = self.plan({"name": "官方模式", "targets": ["codex", "zcode"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-1"}})
        report = sp.analyze_plan(cfg, plan, mode_of=lambda n: "official")
        statuses = {d["target"]: d["status"] for d in report["details"]}
        self.assertEqual(statuses, {"codex": "skipped", "zcode": "skipped"})
        self.assertEqual({i["code"] for i in report["warnings"]}, {"mode-gated"})
        # 第三方模式才写入
        report2 = sp.analyze_plan(cfg, plan, mode_of=lambda n: "third-party")
        self.assertTrue(report2["writable"])
        self.assertEqual({d["status"] for d in report2["details"]}, {"ok"})

    def test_official_zcode_apply_keeps_default_model_selection(self):
        cfg = self.config()
        cfg["sync"]["targets"]["zcode"]["enabled"] = True
        cfg["sync"]["targets"]["zcode"]["mode"] = "official"
        cfg["sync"]["targets"]["zcode"]["files"] = [str(self.home / ".zcode" / "v2" / "provider_config.json")]
        self.save(cfg)
        native = self.home / ".zcode" / "v2" / "provider_config.json"
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text(json.dumps({"config": {"defaultModelSelection": {"providerId": "official-account"}}}),
                          encoding="utf-8")
        plan = self.plan({"name": "只动目录", "targets": ["zcode"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-1"}})
        before = engine.HUB_CONFIG.read_bytes()
        with self.assertRaises(ValueError):
            server.api_switch_plan_apply({"plan": plan, "targets": ["zcode"]})
        self.assertEqual(before, engine.HUB_CONFIG.read_bytes())
        after = read_json(native)
        self.assertEqual(after["config"]["defaultModelSelection"], {"providerId": "official-account"})

    # -- 5. 失效引用与不兼容目标 --
    def test_deleted_model_marks_plan_stale_without_substitution(self):
        self.enable("pi")
        plan = self.plan({"name": "会失效", "targets": ["pi"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-1"}})
        server.api_update_switch_plans({"switchPlans": {"version": 1, "default": "p1",
                                                       "plans": {"p1": plan}}})
        self.assertFalse(server.api_switch_plans()["store"]["plans"][0]["stale"])

        cfg = self.config()
        cfg["providers"]["deepseek"]["models"] = [
            m for m in cfg["providers"]["deepseek"]["models"] if m["id"] != "deepseek/model-1"]
        self.save(cfg)
        view = server.api_switch_plans()["store"]["plans"][0]
        self.assertTrue(view["stale"])
        ref = next(r for r in view["refs"] if r["where"] == "base")
        self.assertEqual(ref["state"], "model-missing")
        self.assertFalse(ref["valid"])
        # 失效的引用不会被改写成别的模型
        self.assertEqual(view["base"]["model"], "deepseek/model-1")

    def test_deleted_provider_marks_fixed_reference_stale(self):
        self.enable("pi")
        plan = self.normalized({"name": "指向会消失的供应商", "targets": ["pi"],
                                "base": {"provider": "deepseek", "model": "deepseek/model-1"},
                                "agents": {"pi": {"policy": "fixed", "provider": "openrouter",
                                                  "model": "openrouter/model-1"}}})
        refs = sp.reference_states(self.config(), plan)
        self.assertFalse(refs["stale"])
        cfg = self.config()
        del cfg["providers"]["openrouter"]
        states = sp.reference_states(cfg, plan)
        self.assertTrue(states["stale"])
        missing = next(r for r in states["refs"] if r["where"] == "agents.pi")
        self.assertEqual(missing["state"], "provider-missing")
        # 编译期也会整组拦下来，不产生"一半目标被跳过"的结果
        patched, patch, issues, blocked = sp.compile_plan(cfg, plan, targets=["pi"])
        self.assertIsNone(patched)
        self.assertEqual(blocked, {"pi"})
        self.assertIn("provider-missing", {i["code"] for i in issues})

    def test_disabled_provider_and_unready_connection_block_the_group(self):
        cfg = self.config()
        cfg["providers"]["openrouter"]["enabled"] = False
        self.save(cfg)
        self.enable("pi")
        plan = self.normalized({"name": "停用", "targets": ["pi"],
                                "base": {"provider": "openrouter",
                                         "model": "openrouter/model-1"}})
        report = sp.analyze_plan(self.config(), plan)
        self.assertFalse(report["writable"])
        self.assertIn("provider-disabled", {i["code"] for i in report["errors"]})
        # 连接不就绪同样整组拦下：这里的供应商没有 Claude 连接（默认线是 OpenAI 兼容）
        cfg2 = self.config()
        cfg2["providers"]["openrouter"]["enabled"] = True
        cfg2["providers"]["openrouter"].pop("agents", None)
        plan2 = self.normalized({"name": "无 Claude 连接", "targets": ["claude"],
                                 "base": {"provider": "openrouter",
                                          "model": "openrouter/model-1"}})
        report2 = sp.analyze_plan(cfg2, plan2)
        self.assertFalse(report2["writable"])
        self.assertIn("connection-not-ready", {i["code"] for i in report2["errors"]})

    def test_plan_structure_validation_rejects_unknown_fields(self):
        with self.assertRaises(sp.PlanError):
            sp.normalize_plan({"name": "x", "targets": ["pi"], "bogus": 1})
        with self.assertRaises(sp.PlanError):
            sp.normalize_plan({"name": "x", "targets": ["pi"],
                               "base": {"provider": "a", "model": "b", "policy": "fixed"}})
        with self.assertRaises(sp.PlanError):
            sp.normalize_plan({"name": "x", "targets": ["pi"],
                               "agents": {"claude": {"policy": "fixed", "provider": "a", "model": "b"}}})
        with self.assertRaises(sp.PlanError):
            sp.normalize_plan({"name": "x", "targets": ["pi"],
                               "base": {"provider": "a", "model": "b"},
                               "agents": {"pi": {"policy": "follow", "provider": "a"}}})
        with self.assertRaises(ValueError):
            server.normalize_switch_plans(server.hub(), {"version": 1, "plans": {
                "a": {"name": "同名", "targets": ["pi"], "base": {"provider": "deepseek", "model": "deepseek/model-1"}},
                "b": {"name": "同名", "targets": ["pi"], "base": {"provider": "deepseek", "model": "deepseek/model-1"}}}})
        with self.assertRaises(ValueError):
            server.normalize_switch_plans(server.hub(), {"version": 1, "default": "nope", "plans": {}})
        with self.assertRaises(ValueError):
            server.normalize_switch_plans(server.hub(), {"version": 2, "plans": {}})

    # -- 6. 冲突、外部修改与回滚 / 恢复 --
    def test_apply_is_rejected_when_a_native_file_changed_after_preview(self):
        self.enable("pi")
        self.write_pi_settings()
        plan = self.plan({"name": "冲突", "targets": ["pi"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-1"}})
        preview = server.api_switch_plan_preview({"plan": plan, "targets": ["pi"]})
        self.assertTrue(preview["ok"])
        # 预览后、应用前有人在客户端改了同一个文件
        self.write_pi_settings()
        path = self.home / ".pi" / "agent" / "settings.json"
        path.write_text(json.dumps({"theme": "dark", "defaultModel": "externally-edited"}),
                        encoding="utf-8")
        with self.assertRaises(Exception):
            server.hub().apply_preview(preview["planId"])
        self.assertEqual(read_json(path)["defaultModel"], "externally-edited")

    def test_mid_write_failure_rolls_back_and_hub_config_stays_consistent(self):
        self.enable("pi", "claude")
        self.write_claude_settings()
        self.write_pi_settings()
        pi_before = (self.home / ".pi" / "agent" / "settings.json").read_bytes()
        claude_before = (self.home / ".claude" / "settings.json").read_bytes()
        plan = self.plan({"name": "回滚", "targets": ["pi", "claude"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-1"}})
        preview = server.api_switch_plan_preview({"plan": plan, "targets": ["pi", "claude"]})
        self.assertTrue(preview["ok"])
        original = transaction._atomic_write
        calls = {"n": 0}

        def fail_second(path, data, mode=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("synthetic mid-write failure")
            return original(path, data, mode)

        with patch.object(transaction, "_atomic_write", side_effect=fail_second):
            with self.assertRaises(Exception):
                server.hub().apply_preview(preview["planId"])
        self.assertEqual((self.home / ".pi" / "agent" / "settings.json").read_bytes(), pi_before)
        self.assertEqual((self.home / ".claude" / "settings.json").read_bytes(), claude_before)
        # providers.json 的 modelSync 已按方案落盘；原生文件没被改动
        self.assertEqual(self.config()["modelSync"]["default"]["model"], "deepseek/model-1")

    def test_apply_then_restore_returns_exact_original_files(self):
        self.enable("pi", "claude")
        self.write_claude_settings()
        self.write_pi_settings()
        pi_before = (self.home / ".pi" / "agent" / "settings.json").read_bytes()
        claude_before = (self.home / ".claude" / "settings.json").read_bytes()
        plan = self.plan({"name": "可恢复", "targets": ["pi", "claude"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-2"}})
        applied = server.api_switch_plan_apply({"plan": plan, "targets": ["pi", "claude"]})
        self.assertTrue(applied["ok"], applied)
        self.assertNotEqual((self.home / ".claude" / "settings.json").read_bytes(), claude_before)
        server.api_restore({"transactionId": applied["transactionId"]})
        self.assertEqual((self.home / ".claude" / "settings.json").read_bytes(), claude_before)
        self.assertEqual((self.home / ".pi" / "agent" / "settings.json").read_bytes(), pi_before)

    def test_restore_refuses_files_edited_after_apply(self):
        self.enable("claude")
        self.write_claude_settings()
        plan = self.plan({"name": "外部改动", "targets": ["claude"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-2"}})
        applied = server.api_switch_plan_apply({"plan": plan, "targets": ["claude"]})
        self.assertTrue(applied["ok"])
        path = self.home / ".claude" / "settings.json"
        data = read_json(path)
        data["env"]["HAND_EDITED"] = "yes"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(Exception):
            server.api_restore({"transactionId": applied["transactionId"]})
        self.assertEqual(read_json(path)["env"]["HAND_EDITED"], "yes")

    # -- 7. Claude 档位：显式指定 / 清除 / 不碰 --
    def test_claude_tiers_are_explicit_only_and_clear_removes_them(self):
        self.enable("claude")
        self.write_claude_settings()
        plan = {
            "name": "档位", "targets": ["claude"],
            "base": {"provider": "deepseek", "model": "deepseek/model-1"},
            "agents": {"claude": {"policy": "follow", "tiers": {
                # 档位与主模型可以来自不同连接，但档位必须落在有 Claude 连接的供应商上
                "opus": {"policy": "explicit", "provider": "deepseek", "model": "deepseek/model-2"},
                "haiku": {"policy": "clear"},
            }}},
        }
        result = server.api_switch_plan_apply({"plan": plan, "targets": ["claude"]})
        self.assertTrue(result["ok"], result)
        env = read_json(self.home / ".claude" / "settings.json")["env"]
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "deepseek/model-2")
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek/model-1")
        # 未指定的档位不会被顺手写入
        self.assertNotIn("ANTHROPIC_DEFAULT_SONNET_MODEL", env)
        self.assertNotIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", env)

        # 清除：显式移除 opus 档位（Hub 管理的键被收回，用户自己的键不受影响）
        plan2 = {
            "name": "清档位", "targets": ["claude"],
            "base": {"provider": "deepseek", "model": "deepseek/model-1"},
            "agents": {"claude": {"policy": "follow", "tiers": {
                "opus": {"policy": "clear"}, "haiku": {"policy": "clear"}}}},
        }
        result2 = server.api_switch_plan_apply({"plan": plan2, "targets": ["claude"]})
        self.assertTrue(result2["ok"], result2)
        settings = read_json(self.home / ".claude" / "settings.json")
        env2 = settings["env"]
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env2)
        self.assertEqual(env2["MY_OWN_ENV"], "keep-me")     # 非托管键保留
        self.assertIn("hooks", settings)

    def test_tier_with_missing_provider_blocks_the_whole_group(self):
        self.enable("claude")
        plan = self.normalized({
            "name": "档位失效", "targets": ["claude"],
            "base": {"provider": "deepseek", "model": "deepseek/model-1"},
            "agents": {"claude": {"policy": "follow", "tiers": {
                "opus": {"policy": "explicit", "provider": "ghost", "model": "x"}}}},
        })
        report = sp.analyze_plan(self.config(), plan)
        self.assertFalse(report["writable"])
        self.assertIn("tier-connection-not-ready", {i["code"] for i in report["errors"]})
        # 没有产生任何写入
        self.assertFalse((self.home / ".claude" / "settings.json").exists())

    # -- 8. 方案保存 / 更新 / 删除 --
    def test_save_update_and_delete_plans_without_silent_substitution(self):
        self.enable("pi")
        plan_a = {"name": "A", "targets": ["pi"],
                  "base": {"provider": "deepseek", "model": "deepseek/model-1"}}
        plan_b = {"name": "B", "targets": ["pi"],
                  "base": {"provider": "openrouter", "model": "openrouter/model-1"}}
        saved = server.api_update_switch_plans({"switchPlans": {
            "version": 1, "default": "a", "plans": {"a": plan_a, "b": plan_b}}})
        self.assertEqual([p["name"] for p in saved["store"]["plans"]], ["A", "B"])
        self.assertEqual(saved["store"]["default"], "a")

        # 更新 A：名字保留，内容换成另一个模型
        plan_a2 = dict(plan_a, base={"provider": "deepseek", "model": "deepseek/model-3"})
        updated = server.api_update_switch_plans({"switchPlans": {
            "version": 1, "default": "a", "plans": {"a": plan_a2, "b": plan_b}}})
        a = next(p for p in updated["store"]["plans"] if p["id"] == "a")
        self.assertEqual(a["base"]["model"], "deepseek/model-3")

        # 删除默认方案：默认指针一并清空，不偷偷指向另一个方案
        deleted = server.api_switch_plan_delete({"planId": "a"})
        self.assertTrue(deleted["defaultCleared"])
        self.assertIsNone(deleted["store"]["default"])
        self.assertEqual([p["name"] for p in deleted["store"]["plans"]], ["B"])

    def test_unknown_plan_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            server.api_switch_plan_analyze({"planId": "does-not-exist"})
        with self.assertRaises(ValueError):
            server.api_switch_plan_delete({"planId": "does-not-exist"})
        with self.assertRaises(ValueError):
            server.api_switch_plan_preview({})

    # -- 9. 备用线路诊断 --
    def test_fallback_diagnosis_respects_order_capability_and_cost(self):
        cfg = self.config()
        # 给 codex 连接准备两条线路：便宜的模型缺 image 模态，贵的超费用上限
        cfg["providers"]["deepseek"]["agents"]["codex"] = {"api": "openai-responses",
                                                           "models": [
            {"id": "cheap", "contextWindow": 8000, "input": ["text"], "reasoning": False,
             "cost": {"inputPerMillion": 0.1}},
            {"id": "pricey", "contextWindow": 200000, "input": ["text", "image"],
             "reasoning": True, "cost": {"inputPerMillion": 30.0}}]}
        cfg["sync"]["targets"]["codex"]["enabled"] = True
        cfg["sync"]["targets"]["codex"]["mode"] = "third-party"
        for name in ("pi", "claude", "dsh", "zcode"):
            cfg["sync"]["targets"][name]["enabled"] = False
        self.save(cfg)
        plan = self.plan({
            "name": "备用线路", "targets": ["codex"],
            "base": {"provider": "deepseek", "model": "cheap"},
            "fallback": {
                "routes": [{"provider": "deepseek", "model": "cheap"},
                           {"provider": "deepseek", "model": "pricey"}],
                "require": {"minContextWindow": 100000, "input": ["image"], "reasoning": True},
                "allowCost": {"inputPerMillion": 5.0},
            },
        })
        report = server.api_switch_fallback({"plan": plan, "targets": ["codex"]})
        self.assertFalse(report["autoSwitch"])
        first, second = report["routes"]
        self.assertFalse(first["usable"])
        states = {c["state"] for c in first["checks"]}
        self.assertIn("capability-below", states)
        self.assertFalse(second["usable"])
        self.assertIn("cost-exceeds", {c["state"] for c in second["checks"]})
        self.assertIsNone(report["recommended"])          # 没有满足约束的线路
        self.assertTrue(report["unknowns"])               # 额度 / 会话状态如实标未知

    def test_fallback_reports_unknown_when_metadata_is_missing(self):
        cfg = self.config()
        cfg["providers"]["deepseek"]["agents"]["codex"] = {
            "api": "openai-responses", "models": [{"id": "bare"}]}
        cfg["sync"]["targets"]["codex"]["enabled"] = True
        cfg["sync"]["targets"]["codex"]["mode"] = "third-party"
        plan = self.plan({"name": "缺信息", "targets": ["codex"],
                          "base": {"provider": "deepseek", "model": "bare"},
                          "fallback": {"routes": [{"provider": "deepseek", "model": "bare"}],
                                       "require": {"minContextWindow": 100000, "reasoning": True},
                                       "allowCost": {"inputPerMillion": 1.0}}})
        report = server.api_switch_fallback({"plan": plan, "targets": ["codex"]})
        codes = {i["code"] for c in report["routes"][0]["checks"] for i in c["constraints"]}
        self.assertEqual(codes, {"capability-unknown", "cost-unknown"})
        self.assertFalse(report["routes"][0]["usable"])   # 信息缺失时不承诺可用

    # -- 10. 范围隔离：未被选中的 Agent 一个文件都不碰 --
    def test_untouched_targets_are_not_in_the_preview(self):
        self.enable("pi", "claude")
        self.write_pi_settings()
        self.write_claude_settings()
        claude_before = (self.home / ".claude" / "settings.json").read_bytes()
        plan = self.plan({"name": "只动 pi", "targets": ["pi"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-1"}})
        result = server.api_switch_plan_apply({"plan": plan, "targets": ["pi"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["targets"], ["pi"])
        self.assertEqual((self.home / ".claude" / "settings.json").read_bytes(), claude_before)
        self.assertEqual(read_json(self.home / ".pi" / "agent" / "settings.json")["defaultModel"],
                         "deepseek/model-1")

    def test_plan_state_view_never_leaks_keys(self):
        self.enable("pi")
        plan = self.plan({"name": "脱敏", "targets": ["pi"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-1"}})
        server.api_update_switch_plans({"switchPlans": {"version": 1, "default": "p", "plans": {"p": plan}}})
        blob = json.dumps(server.api_switch_plans(), ensure_ascii=False)
        self.assertNotIn("test-not-a-real-key", blob)
        preview = server.api_switch_plan_preview({"planId": "p"})
        blob2 = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn("test-not-a-real-key", blob2)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", blob2)

    def test_legacy_config_without_switch_plans_still_loads(self):
        # 旧配置没有 switchPlans：读取不报错，写入也不破坏原有字段
        self.assertNotIn("switchPlans", self.config())
        view = server.api_switch_plans()
        self.assertEqual(view["store"]["plans"], [])
        self.assertIsNone(view["store"]["default"])
        plan = self.plan({"name": "新方案", "targets": ["pi"],
                          "base": {"provider": "deepseek", "model": "deepseek/model-1"}})
        server.api_update_switch_plans({"switchPlans": {"version": 1, "plans": {"p": plan}}})
        cfg = self.config()
        self.assertIn("switchPlans", cfg)
        self.assertIn("providers", cfg)          # 原有结构未被替换
        self.assertIn("modelSync", cfg)


class SwitchPlanControlTests(SwitchPlanFixture):
    """错误注入对照：确认这些检查真的会失败，而不是恒真。"""

    def test_deleting_the_selected_model_breaks_an_otherwise_valid_plan(self):
        self.enable("pi")
        raw = {"name": "对照", "targets": ["pi"],
               "base": {"provider": "deepseek", "model": "deepseek/model-3"}}
        self.assertTrue(sp.analyze_plan(self.config(), self.normalized(raw))["writable"])
        # 只删掉被选中的那个模型：同一个方案必须变成不可写入
        cfg = self.config()
        cfg["providers"]["deepseek"]["models"] = [
            m for m in cfg["providers"]["deepseek"]["models"] if m["id"] != "deepseek/model-3"]
        self.save(cfg)
        report = sp.analyze_plan(self.config(), self.normalized(raw))
        self.assertFalse(report["writable"])
        self.assertIn("model-not-in-connection", {i["code"] for i in report["errors"]})
        self.assertEqual(report["compiledModelSync"], None)

    def test_removing_the_agent_connection_breaks_an_otherwise_valid_plan(self):
        self.enable("claude")
        raw = {"name": "对照2", "targets": ["claude"],
               "base": {"provider": "deepseek", "model": "deepseek/model-1"}}
        self.assertTrue(sp.analyze_plan(self.config(), self.normalized(raw))["writable"])
        cfg = self.config()
        del cfg["providers"]["deepseek"]["agents"]["claude"]   # 只剩 OpenAI 兼容的默认线
        self.save(cfg)
        report = sp.analyze_plan(self.config(), self.normalized(raw))
        self.assertFalse(report["writable"])
        self.assertIn("connection-not-ready", {i["code"] for i in report["errors"]})

    def test_analyse_is_read_only_and_apply_is_not_duplicated(self):
        self.enable("pi")
        self.write_pi_settings()
        before_cfg = engine.HUB_CONFIG.read_bytes()
        before_pi = (self.home / ".pi" / "agent" / "settings.json").read_bytes()
        raw = {"name": "只读对照", "targets": ["pi"],
               "base": {"provider": "deepseek", "model": "deepseek/model-1"}}
        server.api_switch_plan_analyze(server.api_switch_plans and {"plan": raw, "targets": ["pi"]})
        self.assertEqual(engine.HUB_CONFIG.read_bytes(), before_cfg)
        self.assertEqual((self.home / ".pi" / "agent" / "settings.json").read_bytes(), before_pi)


class SwitchPlanCLITests(SwitchPlanFixture):
    """hub plan list / show / apply：与工作台应用走同一条实现。"""

    def capture(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = engine.main(argv)
        return code, out.getvalue() + err.getvalue()

    def seed(self, plans, default=None):
        cfg = self.config()
        cfg["switchPlans"] = {"version": 1, "default": default, "plans": plans}
        self.save(cfg)

    def test_plan_list_marks_stale_references(self):
        self.enable("pi")
        self.seed({"ok-plan": {"name": "好方案", "targets": ["pi"],
                               "base": {"provider": "deepseek", "model": "deepseek/model-1"}},
                   "bad-plan": {"name": "坏方案", "targets": ["pi"],
                                "base": {"provider": "ghost", "model": "x"}}}, default="ok-plan")
        code, text = self.capture(["plan", "list"])
        self.assertEqual(code, 0)
        self.assertIn("ok-plan", text)
        self.assertIn("好方案", text)
        self.assertIn("引用已失效", text)
        self.assertIn("provider-missing", text)

    def test_plan_show_reports_each_target_and_refuses_broken(self):
        self.enable("pi", "claude")
        self.seed({"work": {"name": "工作", "targets": ["pi", "claude"],
                            "base": {"provider": "deepseek", "model": "deepseek/model-2",
                                     "reasoningEffort": "high"}},
                   "bad": {"name": "坏", "targets": ["pi"],
                           "base": {"provider": "ghost", "model": "x"}}})
        code, text = self.capture(["plan", "show", "工作"])
        self.assertEqual(code, 0)
        self.assertIn("pi", text)
        self.assertIn("Claude Code", text)
        self.assertIn("强度 high", text)
        bad_code, bad_text = self.capture(["plan", "show", "bad"])
        self.assertEqual(bad_code, 1)
        self.assertIn("不可写入", bad_text)
        missing_code, _ = self.capture(["plan", "show", "不存在"])
        self.assertEqual(missing_code, 1)

    def test_plan_apply_compiles_and_pushes_native_config(self):
        self.enable("pi", "claude")
        self.write_pi_settings()
        self.write_claude_settings()
        self.seed({"work": {"name": "工作", "targets": ["pi", "claude"],
                            "base": {"provider": "deepseek", "model": "deepseek/model-2",
                                     "reasoningEffort": "high"},
                            "agents": {"claude": {"policy": "keep"}}}})
        code, text = self.capture(["plan", "apply", "work"])
        self.assertEqual(code, 0, text)
        self.assertIn("跳过 Claude Code", text)
        # 只写了 pi：keep 的目标不写
        self.assertEqual(self.config()["modelSync"]["overrides"]["pi"]["model"], "deepseek/model-2")
        self.assertNotIn("claude", self.config()["modelSync"]["overrides"])
        pi = read_json(self.home / ".pi" / "agent" / "settings.json")
        self.assertEqual(pi["defaultModel"], "deepseek/model-2")
        self.assertEqual(pi["defaultThinkingLevel"], "high")
        self.assertEqual(pi["theme"], "dark")            # 未托管字段保留
        claude = read_json(self.home / ".claude" / "settings.json")
        self.assertEqual(claude["env"]["ANTHROPIC_MODEL"], "old-model")   # keep：客户端当前值不动

    def test_plan_apply_refuses_disabled_target_and_broken_plan(self):
        self.enable("pi")
        self.seed({"work": {"name": "工作", "targets": ["pi", "claude"],
                            "base": {"provider": "deepseek", "model": "deepseek/model-1"}},
                   "bad": {"name": "坏", "targets": ["pi"],
                           "base": {"provider": "ghost", "model": "x"}}})
        code, text = self.capture(["plan", "apply", "work"])
        self.assertEqual(code, 1)
        self.assertIn("没有在 sync.targets 里启用", text)
        # 坏方案仍然被拒
        self.enable("pi")
        bad_code, bad_text = self.capture(["plan", "apply", "bad"])
        self.assertEqual(bad_code, 1)
        self.assertIn("没有写入任何文件", bad_text)

    def test_plan_apply_with_explicit_target_scope(self):
        self.enable("pi", "claude")
        self.write_pi_settings()
        self.seed({"work": {"name": "工作", "targets": ["pi", "claude"],
                            "base": {"provider": "deepseek", "model": "deepseek/model-2"}}})
        code, text = self.capture(["plan", "apply", "work", "--target", "pi"])
        self.assertEqual(code, 0, text)
        self.assertIn("pi", self.config()["modelSync"]["overrides"])
        self.assertNotIn("claude", self.config()["modelSync"]["overrides"])
        self.assertFalse((self.home / ".claude" / "settings.json").exists())
