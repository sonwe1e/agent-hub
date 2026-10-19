"""modelSync API：同一模型跨客户端对齐的读写、校验与数据丢失护栏。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from support import isolated, engine, server

EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def entry(pid="deepseek", model="deepseek/model-3", effort="high"):
    return {"provider": pid, "model": model, "reasoningEffort": effort}


class ModelSyncAPITests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    # -- helpers --
    def config(self):
        return json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))

    def model_ids(self):
        return {pid: [m["id"] for m in p.get("models", [])]
                for pid, p in self.config()["providers"].items()}

    def put(self, block):
        return server.api_update_modelsync({"modelSync": block})

    def http(self, method, path, body=None, raw=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", data=data,
                                         method=method, headers={"Content-Type": "application/json"})
        try:
            try:
                response = urllib.request.urlopen(request)
            except urllib.error.HTTPError as e:
                response = e
            with response:
                return response.code, json.loads(response.read().decode())
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()

    # -- hub.py 依赖 --
    def test_hub_modelsync_api_is_available(self):
        """端点直接调用 hub.py 的实现；接口被移除时必须显式失败而不是静默降级。"""
        for name in ("resolve_agent_model", "agent_model_tiers_public", "agent_model_options"):
            self.assertTrue(callable(getattr(server, name)), name)
        # 适配器视图与界面视图都在，fable 才不会被 API 层吞掉
        self.assertTrue(callable(engine.agent_model_tiers))
        self.assertEqual(tuple(engine.REASONING_EFFORTS), EFFORTS)

    def test_reads_stay_inside_isolated_home(self):
        self.assertEqual(engine.HOME, self.home)
        self.assertTrue(str(server.HUB_CONFIG).startswith(str(self.home)))

    # -- GET --
    def test_get_returns_config_resolved_options(self):
        result = server.api_modelsync()
        self.assertEqual(result["modelSync"], self.config()["modelSync"])
        self.assertTrue(result["configured"])
        # 六个目标都必须出现，顺序与界面展示顺序一致
        self.assertEqual(list(result["resolved"]),
                         ["pi", "dsh", "dsh-desktop", "zcode", "claude", "codex"])
        # pi 的 agent 档拿到全局主模型
        self.assertEqual(result["resolved"]["pi"]["model"], "deepseek/model-1")
        self.assertEqual(result["resolved"]["pi"]["provider"], "deepseek")
        self.assertEqual(result["resolved"]["pi"]["reasoningEffort"], "medium")
        # dsh-desktop 跟随 dsh；codex 现在是正式目标，同样吃全局默认
        self.assertEqual(result["resolved"]["dsh-desktop"], result["resolved"]["dsh"])
        self.assertEqual(result["resolved"]["dsh"]["model"], "deepseek/model-1")
        self.assertEqual(result["resolved"]["zcode"]["model"], "deepseek/model-1")
        self.assertEqual(result["resolved"]["codex"]["model"], "deepseek/model-1")
        self.assertEqual(result["resolved"]["codex"]["provider"], "deepseek")
        # claude 的档位由 hub.py 决定（是否吃全局默认是它的策略）；这里只验证透传
        self.assertEqual(result["resolved"]["claude"],
                         engine.agent_model_tiers(self.config(), "claude").get("model"))
        # 档位明细使用 hub.py 的界面视图（claude 不含内部 agent 档与历史 fable 档）
        self.assertEqual(set(result["tiers"]), {"pi", "dsh", "dsh-desktop", "zcode", "claude", "codex"})
        self.assertEqual(result["tiers"]["pi"], {"agent": result["resolved"]["pi"]})
        self.assertEqual(result["tiers"]["dsh"], engine.agent_model_tiers_public(self.config(), "dsh"))
        self.assertEqual(result["tiers"]["dsh-desktop"], result["tiers"]["dsh"])
        self.assertEqual(result["tiers"]["zcode"], engine.agent_model_tiers_public(self.config(), "zcode"))
        self.assertEqual(result["tiers"]["codex"], engine.agent_model_tiers_public(self.config(), "codex"))
        self.assertEqual(result["tiers"]["claude"],
                         engine.agent_model_tiers_public(self.config(), "claude"))
        self.assertTrue(result["tiers"]["dsh"]["agent"]["known"])
        # 界面视图只隐藏档位，不改变每个档位对象本身的形状
        self.assertTrue(set(result["tiers"]["claude"]) <= {"model", "opus", "sonnet", "haiku", "subagent"})
        # 每个档位都是完整对象，界面靠这些字段显示 provider / model 与手填提示
        for target, tiers in result["tiers"].items():
            for name, tier in tiers.items():
                with self.subTest(target=target, tier=name):
                    self.assertEqual(set(tier),
                                     {"provider", "model", "reasoningEffort", "known", "tier"})
                    self.assertEqual(tier["tier"], name)
                    self.assertIsInstance(tier["known"], bool)
        # 下拉选项来自 agent_model_options
        self.assertEqual(len(result["options"]["deepseek"]), 7)
        for model in result["options"]["deepseek"]:
            self.assertTrue({"id", "name"} <= set(model))

    def test_get_options_skips_disabled_providers(self):
        server.api_update_provider("stepfun", {"enabled": False})
        self.assertNotIn("stepfun", server.api_modelsync()["options"])

    # -- PUT 正常路径 --
    def test_put_writes_block_and_backup(self):
        result = self.put({"default": entry(model="deepseek/model-3"),
                           "overrides": {"pi": entry("openrouter", "openrouter/model-2", "low")}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["warnings"], [])
        disk = self.config()
        self.assertEqual(disk["modelSync"]["default"],
                         {"provider": "deepseek", "model": "deepseek/model-3", "reasoningEffort": "high"})
        self.assertEqual(disk["modelSync"]["overrides"]["pi"]["provider"], "openrouter")
        # 写入确实走了 _save_config：updatedAt 刷新且留下备份
        self.assertNotEqual(disk["updatedAt"], "2026-01-01T00:00:00Z")
        self.assertTrue(list((self.home / ".agent-hub/backups").glob("*")))
        # 返回值直接带上新的解析结果，界面无需再请求一次
        self.assertEqual(result["resolved"]["pi"]["model"], "openrouter/model-2")
        self.assertEqual(result["resolved"]["pi"]["reasoningEffort"], "low")

    def test_put_all_efforts_and_null_round_trip(self):
        for effort in (None, *EFFORTS):
            with self.subTest(effort=effort):
                self.put({"default": entry(effort=effort)})
                self.assertEqual(self.config()["modelSync"]["default"]["reasoningEffort"], effort)

    def test_put_empty_block_clears_and_marks_unconfigured(self):
        result = self.put({})
        self.assertTrue(result["ok"])
        self.assertEqual(self.config()["modelSync"], {})
        self.assertEqual(server.api_modelsync()["modelSync"], {})
        self.assertFalse(server.api_modelsync()["configured"])

    def test_put_trims_whitespace_and_drops_empty_overrides(self):
        self.put({"default": entry("  deepseek  ", "  deepseek/model-4  "), "overrides": {}})
        stored = self.config()["modelSync"]
        self.assertEqual(stored["default"]["provider"], "deepseek")
        self.assertEqual(stored["default"]["model"], "deepseek/model-4")
        self.assertNotIn("overrides", stored)

    # -- PUT 拒绝路径 --
    def test_put_rejects_unknown_target(self):
        before = server.HUB_CONFIG.read_bytes()
        # codex / zcode 从 v2 起是正式目标，不再属于未知目标
        for target in ("mcode", "cc-switch", "omp", "dsh_desktop", "unknown"):
            with self.subTest(target=target), self.assertRaises(ValueError) as ctx:
                self.put({"overrides": {target: entry()}})
            self.assertIn("未知的 modelSync 目标", str(ctx.exception))
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_put_rejects_unknown_provider(self):
        before = server.HUB_CONFIG.read_bytes()
        for block in ({"default": entry("nope", "nope/model")},
                      {"overrides": {"pi": entry("nope", "nope/model")}},
                      {"overrides": {"claude": {"provider": "ghost", "model": "ghost/model",
                                                "tiers": {"opus": entry()}}}}):
            with self.subTest(block=block), self.assertRaises(ValueError) as ctx:
                self.put(block)
            self.assertIn("不存在", str(ctx.exception))
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_put_rejects_disabled_provider(self):
        server.api_update_provider("stepfun", {"enabled": False})
        before = server.HUB_CONFIG.read_bytes()
        for block in ({"default": entry("stepfun", "stepfun/model-1")},
                      {"overrides": {"claude": {"provider": "deepseek", "model": "deepseek/model-1",
                                                "tiers": {"opus": entry("stepfun", "stepfun/model-1")}}}}):
            with self.subTest(block=block), self.assertRaises(ValueError) as ctx:
                self.put(block)
            self.assertIn("未启用", str(ctx.exception))
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_put_rejects_invalid_reasoning_effort(self):
        before = server.HUB_CONFIG.read_bytes()
        for effort in ("turbo", "HIGH", "", " high", 5, True, ["high"]):
            with self.subTest(effort=effort), self.assertRaises(ValueError) as ctx:
                self.put({"default": entry(effort=effort)})
            self.assertIn("reasoningEffort", str(ctx.exception))
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_put_rejects_malformed_blocks(self):
        before = server.HUB_CONFIG.read_bytes()
        blocks = [
            [], "deepseek/model-1", None, 5,
            {"default": entry(), "extra": 1},
            {"overrides": []},
            {"default": "deepseek/model-1"},
            {"default": {**entry(), "tier": "agent"}},
            {"default": {"provider": "", "model": "deepseek/model-1"}},
            {"default": {"provider": 5, "model": "deepseek/model-1"}},
            {"default": {"provider": "deepseek"}},
            {"default": {"provider": "deepseek", "model": ""}},
            {"default": {"provider": "deepseek", "model": "   "}},
            {"default": {"provider": "deepseek", "model": 5}},
            {"overrides": {"pi": []}},
        ]
        for block in blocks:
            with self.subTest(block=block), self.assertRaises(ValueError):
                self.put(block)
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_put_requires_modelsync_key(self):
        before = server.HUB_CONFIG.read_bytes()
        with self.assertRaises(ValueError) as ctx:
            server.api_update_modelsync({})
        self.assertIn("modelSync", str(ctx.exception))
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    # -- dsh-desktop 归一化 --
    def test_dsh_desktop_normalizes_to_dsh(self):
        result = self.put({"default": entry(),
                           "overrides": {"dsh-desktop": entry("openrouter", "openrouter/model-1", "low")}})
        overrides = self.config()["modelSync"]["overrides"]
        self.assertEqual(list(overrides), ["dsh"])
        self.assertNotIn("dsh-desktop", overrides)
        self.assertEqual(result["resolved"]["dsh"], result["resolved"]["dsh-desktop"])
        self.assertEqual(result["resolved"]["dsh"]["provider"], "openrouter")
        self.assertEqual(result["tiers"]["dsh"], result["tiers"]["dsh-desktop"])

    def test_dsh_alias_pair_must_not_drift(self):
        before = server.HUB_CONFIG.read_bytes()
        with self.assertRaises(ValueError) as ctx:
            self.put({"overrides": {"dsh": entry("deepseek", "deepseek/model-1"),
                                    "dsh-desktop": entry("openrouter", "openrouter/model-1")}})
        self.assertIn("dsh-desktop", str(ctx.exception))
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())
        # 内容一致时只保留归一化后的那一份
        self.put({"overrides": {"dsh": entry("deepseek", "deepseek/model-1", "low"),
                                "dsh-desktop": entry("deepseek", "deepseek/model-1", "low")}})
        self.assertEqual(list(self.config()["modelSync"]["overrides"]), ["dsh"])

    # -- 手填模型 --
    def test_hand_typed_model_is_kept_with_warning(self):
        result = self.put({"default": entry("deepseek", "deepseek/not-in-library")})
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("deepseek/not-in-library", result["warnings"][0])
        self.assertIn("deepseek", result["warnings"][0])
        self.assertEqual(self.config()["modelSync"]["default"]["model"], "deepseek/not-in-library")
        self.assertFalse(result["resolved"]["pi"]["known"])
        # 手填值不阻止写入，也不会被写进供应商模型库
        self.assertNotIn("deepseek/not-in-library", self.model_ids()["deepseek"])

    def test_warning_also_covers_override_and_tier(self):
        result = self.put({"default": entry(),
                           "overrides": {"claude": {"provider": "deepseek", "model": "deepseek/model-1",
                                                    "tiers": {"opus": entry("deepseek", "deepseek/custom")}}}})
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("modelSync.overrides.claude.tiers.opus", result["warnings"][0])
        self.assertIn("deepseek/custom", result["warnings"][0])
        self.assertFalse(server.api_modelsync()["tiers"]["claude"]["opus"]["known"])

    # -- Claude 档位 --
    def test_claude_tiers_round_trip_and_resolution(self):
        block = {"default": entry(),
                 "overrides": {"claude": {
                     "provider": "deepseek", "model": "deepseek/model-1",
                     "tiers": {"opus": {"provider": "deepseek", "model": "deepseek/model-3"},
                               "haiku": entry("deepseek", "deepseek/model-2", "low"),
                               # fable 是历史档位：必须能存回去，也参与解析，避免老配置漂移
                               "fable": entry("deepseek", "deepseek/model-1", None)}}}}
        result = self.put(block)
        self.assertEqual(result["warnings"], [])
        stored = self.config()["modelSync"]["overrides"]["claude"]["tiers"]
        self.assertEqual(set(stored), {"opus", "haiku", "fable"})
        self.assertEqual(stored["fable"], {"provider": "deepseek", "model": "deepseek/model-1",
                                           "reasoningEffort": None})
        # GET 能原样取回，界面回填后再保存不会丢档位
        self.assertEqual(server.api_modelsync()["modelSync"], self.config()["modelSync"])
        tiers = server.api_modelsync()["tiers"]["claude"]
        # API 暴露的是「界面档位视图」：不含内部 agent 档与历史 fable 档；
        # 适配器读的 agent_model_tiers 仍保留 fable，用于收回旧别名。
        self.assertEqual(tiers, engine.agent_model_tiers_public(self.config(), "claude"))
        self.assertNotIn("agent", tiers)
        self.assertNotIn("fable", tiers)
        self.assertTrue({"model", "opus", "haiku"} <= set(tiers))
        self.assertEqual(tiers["opus"], {"provider": "deepseek", "model": "deepseek/model-3",
                                         "reasoningEffort": None, "tier": "opus", "known": True})
        self.assertEqual(tiers["haiku"], {"provider": "deepseek", "model": "deepseek/model-2",
                                          "reasoningEffort": "low", "tier": "haiku", "known": True})
        # 适配器视图里 fable 仍然可用：写进去的档位不能被解析层吞掉
        internal = engine.agent_model_tiers(self.config(), "claude")
        self.assertEqual(internal["fable"], {"provider": "deepseek", "model": "deepseek/model-1",
                                             "reasoningEffort": None, "tier": "fable", "known": True})
        # 显式覆盖后，claude 主模型走覆盖里的 model 档
        resolved = server.api_modelsync()["resolved"]["claude"]
        self.assertEqual(resolved["model"], "deepseek/model-1")
        self.assertEqual(resolved["tier"], "model")

    def test_tiers_only_allowed_for_claude(self):
        before = server.HUB_CONFIG.read_bytes()
        for target in ("pi", "dsh", "dsh-desktop"):
            with self.subTest(target=target), self.assertRaises(ValueError) as ctx:
                self.put({"overrides": {target: {**entry(), "tiers": {"opus": entry()}}}})
            self.assertIn("tiers", str(ctx.exception))
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_unknown_claude_tier_rejected(self):
        before = server.HUB_CONFIG.read_bytes()
        for tier in ("sonnetX", "agent", "Opus", "fable2"):
            with self.subTest(tier=tier), self.assertRaises(ValueError) as ctx:
                self.put({"overrides": {"claude": {"provider": "deepseek", "model": "deepseek/model-1",
                                                  "tiers": {tier: entry()}}}})
            self.assertIn("未知档位", str(ctx.exception))
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    # -- 数据丢失护栏 --
    def test_write_never_loses_models(self):
        before_ids = self.model_ids()
        before = self.config()
        self.assertTrue(self.put({"default": entry("openrouter", "openrouter/model-5")})["ok"])
        after = self.config()
        self.assertEqual(before_ids, self.model_ids())
        # 只改 modelSync，其他顶层块原样保留
        for key in ("providers", "sync", "removedProviders"):
            with self.subTest(key=key):
                self.assertEqual(before[key], after[key])

    def test_model_loss_guard_is_still_armed_on_this_path(self):
        """反向对照：同一个 _save_config 遇到真实模型丢失仍然拒绝写入。"""
        stale = engine.Hub()
        stale.cfg["providers"]["deepseek"]["models"] = []
        with self.assertRaises(engine.ModelLossError):
            server._save_config(stale)
        self.assertEqual(len(self.model_ids()["deepseek"]), 7)

    # -- provider.agentModel --
    def test_provider_agent_model_round_trip(self):
        server.api_add_provider({"id": "agentic", "name": "Agentic", "baseUrl": "https://a.example.com/v1",
                                 "apiKey": "sk-test-not-a-real-key", "agentModel": "vendor/best"})
        self.assertEqual(engine.Hub().providers["agentic"]["agentModel"], "vendor/best")
        server.api_update_provider("agentic", {"agentModel": "vendor/other"})
        self.assertEqual(engine.Hub().providers["agentic"]["agentModel"], "vendor/other")
        # null 表示清空
        server.api_update_provider("agentic", {"agentModel": None})
        self.assertIsNone(engine.Hub().providers["agentic"]["agentModel"])

    def test_provider_agent_model_type_rejected_without_write(self):
        before = server.HUB_CONFIG.read_bytes()
        for bad in (5, ["vendor/x"], {"id": "vendor/x"}, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as ctx:
                server.api_update_provider("deepseek", {"agentModel": bad})
            self.assertIn("agentModel", str(ctx.exception))
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())
        with self.assertRaises(ValueError):
            server.api_add_provider({"id": "badagent", "name": "Bad", "baseUrl": "https://a.example.com/v1",
                                     "apiKey": "sk-test", "agentModel": 7})
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    # -- status --
    def test_status_exposes_modelsync_and_readonly_codex(self):
        status = server.api_status()
        self.assertEqual(status["modelSync"], self.config()["modelSync"])
        self.assertEqual(status["modelSyncResolved"]["pi"]["model"], "deepseek/model-1")
        self.assertEqual(status["modelSyncResolved"]["dsh-desktop"], status["modelSyncResolved"]["dsh"])
        self.assertEqual(status["modelSyncResolved"]["zcode"]["model"], "deepseek/model-1")
        self.assertEqual(status["modelSyncResolved"]["codex"]["model"], "deepseek/model-1")
        # codex 现在是正式写入目标（只写 model_provider/model 与 custom 段），
        # 不再是只读。
        self.assertFalse(status["targets"]["codex"]["readonly"])
        self.assertTrue(status["targets"]["codex"]["supported"])
        self.assertFalse(status["targets"]["pi"]["readonly"])
        self.assertFalse(status["targets"]["dsh-desktop"]["readonly"])
        # 给 codex 配了模型后 status 立刻能解析出来
        cfg = self.config()
        cfg["sync"]["targets"]["codex"] = {"enabled": True, "providerId": "custom",
                                           "files": ["~/.codex/config.toml"]}
        engine.save_json(server.HUB_CONFIG, cfg)
        self.put({"default": entry(), "overrides": {"codex": entry("deepseek", "deepseek/model-3")}})
        status = server.api_status()
        self.assertEqual(status["modelSyncResolved"]["codex"]["provider"], "deepseek")
        self.assertEqual(status["modelSyncResolved"]["codex"]["model"], "deepseek/model-3")
        # 写入后 status 立刻反映新配置。Claude 需要显式覆盖，而且覆盖里的供应商
        # 必须有 Claude 连接——这里给 openrouter 补上 Anthropic 连接，
        # 否则解析层会判定 Claude 仍由 deepseek 提供。
        cfg = self.config()
        cfg["providers"]["openrouter"]["agents"] = {
            "codex": {"api": "openai-responses"},
            "claude": {"baseUrl": "https://openrouter.ai/api", "api": "anthropic-messages"},
        }
        engine.save_json(server.HUB_CONFIG, cfg)
        self.put({"default": entry("openrouter", "openrouter/model-6", "max"),
                  "overrides": {"claude": entry("openrouter", "openrouter/model-6", "max")}})
        status = server.api_status()
        self.assertEqual(status["modelSync"]["default"]["provider"], "openrouter")
        self.assertEqual(status["modelSyncResolved"]["pi"]["model"], "openrouter/model-6")
        self.assertEqual(status["modelSyncResolved"]["claude"]["model"], "openrouter/model-6")
        self.assertEqual(status["modelSyncResolved"]["claude"]["reasoningEffort"], "max")
        # 每个 Agent 各自可用的供应商按连接就绪度给出
        self.assertEqual(status["agentCapable"]["claude"], ["deepseek", "openrouter"])
        self.assertEqual(status["agentCapable"]["codex"], ["deepseek", "openrouter"])
        self.assertEqual(status["agentCapable"]["pi"], ["deepseek", "stepfun", "openrouter", "firework"])
        self.assertNotIn("zcode", status["agentCapable"])
        # 连接概况：未声明 Claude 连接的供应商显示未就绪
        self.assertTrue(status["connections"]["deepseek"]["claude"]["ready"])
        self.assertFalse(status["connections"]["stepfun"]["claude"]["ready"])
        self.assertNotIn("anthropicBaseUrl", status["connections"]["deepseek"]["claude"])

    # -- HTTP 层 --
    def test_http_modelsync_get_and_put(self):
        code, result = self.http("GET", "/api/modelsync")
        self.assertEqual(code, 200)
        self.assertEqual(result["modelSync"]["default"]["provider"], "deepseek")
        code, result = self.http("PUT", "/api/modelsync", {"modelSync": {"default": entry("deepseek", "deepseek/model-2", None)}})
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.config()["modelSync"]["default"]["model"], "deepseek/model-2")

    def test_http_modelsync_errors_return_400(self):
        before = server.HUB_CONFIG.read_bytes()
        for body, raw, expected in (
            (None, b"{not json", "JSON"),
            ({}, None, "modelSync"),
            ({"modelSync": {"overrides": {"mcode": entry()}}}, None, "未知的 modelSync 目标"),
            ({"modelSync": {"default": entry("nope", "nope/model")}}, None, "不存在"),
            ({"modelSync": {"default": entry(effort="turbo")}}, None, "reasoningEffort"),
        ):
            with self.subTest(body=body, raw=raw):
                code, result = self.http("PUT", "/api/modelsync", body, raw)
                self.assertEqual(code, 400)
                self.assertIn(expected, result["error"])
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())


if __name__ == "__main__":
    unittest.main()
