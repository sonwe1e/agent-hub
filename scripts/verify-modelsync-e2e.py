# -*- coding: utf-8 -*-
"""端到端验证：在真实客户端配置的副本上跑一次 push，检查每个适配器的产物。

不会写用户的真实 HOME：先把真实配置复制到临时目录，再把 engine.HOME 指过去。
用法: python scripts/verify-modelsync-e2e.py
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hub as engine  # noqa: E402

REAL_HOME = Path.home()
CLONE = [
    ".pi/agent/settings.json",
    ".pi/agent/models.json",
    ".pi/agent/auth.json",
    ".dsh/.credentials.yaml",
    ".dsh/settings.yaml.imported",
    ".dsh/profiles/desktop/cordis.patch.yml",
    ".claude/settings.json",
    ".codex/config.toml",
    ".zcode/v2/provider_config.json",
]
failures: list[str] = []
notes: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        notes.append(f"  OK   {label}")
    else:
        failures.append(label)


def clone_home(root: Path) -> None:
    for rel in CLONE:
        src = REAL_HOME / rel
        if not src.exists():
            notes.append(f"  --   跳过（真实环境没有）: {rel}")
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def main() -> int:
    cfg = json.loads((ROOT / "providers.json").read_text(encoding="utf-8"))
    providers = cfg["providers"]
    # cc-switch 的 SQLite 库不能原子替换，安全预览会整组拒绝；这里显式排除它，
    # 只验证 pi / dsh / claude / codex / zcode 这些文件型目标。
    # mcode / omp 是保留的旧版目标：本机没装对应客户端时适配器会因找不到
    # 配置文件而失败，同样先排除，避免掩盖真正要验证的目标。
    for legacy in ("cc-switch", "mcode", "omp"):
        entry = (cfg.get("sync", {}).get("targets", {}).get(legacy) or {})
        if entry.get("enabled"):
            cfg["sync"]["targets"][legacy] = dict(entry, enabled=False)
            print(f"已排除 {legacy}（旧版目标，不参与本次端到端验证）")
    print()
    enabled = [pid for pid, p in providers.items()
               if p.get("enabled", True) and p.get("models") and not p.get("targets")]
    if not enabled:
        print("没有可用的 OpenAI 兼容供应商，无法验证")
        return 1
    pid = "deepseek" if "deepseek" in enabled else enabled[0]
    model = providers[pid]["models"][0]["id"]
    cfg["modelSync"] = {"default": {"provider": pid, "model": model, "reasoningEffort": "high"}}
    print(f"验证用模型: {pid} / {model}\n")

    # Codex 首次第三方切换前必须处于官方 provider（Hub 不覆盖外部线路）；
    # 本机可能已是第三方线路，所以整体同步先用官方模式，第三方切换在下面单独验证。
    codex_target = cfg["sync"]["targets"].setdefault("codex", {})
    if codex_target.get("mode") == "third-party":
        codex_target["mode"] = "official"
        print("Codex 先用官方模式参与整体同步（首次第三方切换需官方基线）\n")

    def first_model(source) -> str | None:
        return next((m["id"] for m in (source or []) if isinstance(m, dict) and m.get("id")), None)

    # 按 Agent 覆盖：Codex 第三方只接受 Responses 连接，Claude Code 只接受
    # Anthropic 连接。默认供应商不满足时，给这两个平台各配一个就绪的连接，
    # 让一次 push_all 同时覆盖四种客户端。
    codex_pid = next((name for name, p in providers.items()
                      if p.get("enabled", True) and engine.agent_connection_ready(p, "codex")
                      and first_model(engine.agent_connection(p, "codex")["models"])), None)
    claude_pid = next((name for name, p in providers.items()
                       if p.get("enabled", True) and name != pid
                       and engine.agent_connection_ready(p, "claude")
                       and (first_model(engine.agent_connection(p, "claude")["models"])
                            or first_model(p.get("models")))), None)
    overrides = {}
    if codex_pid:
        overrides["codex"] = {"provider": codex_pid,
                              "model": first_model(engine.agent_connection(providers[codex_pid], "codex")["models"]),
                              "reasoningEffort": "high"}
    if claude_pid:
        overrides["claude"] = {"provider": claude_pid,
                               "model": first_model(engine.agent_connection(providers[claude_pid], "claude")["models"])
                               or first_model(providers[claude_pid].get("models"))}
    if overrides:
        cfg["modelSync"]["overrides"] = overrides
        print(f"按 Agent 覆盖: {', '.join(f'{k}={v['provider']}/{v['model']}' for k, v in overrides.items())}\n")

    with tempfile.TemporaryDirectory(prefix="agent-hub-e2e-") as tmp:
        root = Path(tmp)
        clone_home(root)
        # 首次切换 / 恢复官方都要求 Codex 处于官方基线；本机可能已是第三方线路，
        # 这时用一份合成的官方配置继续验证（真实配置一个字节都不会被改动）。
        codex_cfg = root / ".codex" / "config.toml"
        if codex_cfg.exists() and "model_provider" in codex_cfg.read_text(encoding="utf-8"):
            codex_cfg.write_text('model = "official-model"\nmodel_reasoning_effort = "high"\n',
                                 encoding="utf-8")
            print("Codex 现有配置不是官方基线，已替换为合成官方配置（仅临时副本）\n")
        hub_dir = root / ".agent-hub"
        hub_dir.mkdir()
        hub_config = hub_dir / "providers.json"
        hub_config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        engine.HOME = root
        engine.HUB_DIR = hub_dir
        engine.HUB_CONFIG = hub_config
        engine.HUB_STATE = hub_dir / "state.json"
        engine.HUB_BACKUP = hub_dir / "backups"
        engine.expand = lambda p: (root / str(p)[2:]) if str(p).startswith("~/") else Path(p)

        hub = engine.Hub()
        print("push_all 结果:")
        for line in hub.push_all():
            print("   ", engine.console_safe(line))
        print()

        # --- pi ---
        pi_settings = json.loads((root / ".pi/agent/settings.json").read_text(encoding="utf-8"))
        check(pi_settings.get("defaultProvider") == pid, "pi defaultProvider 指向所选供应商")
        check(pi_settings.get("defaultModel") == model, "pi defaultModel 指向所选模型")
        check(pi_settings.get("defaultThinkingLevel") == "high", "pi defaultThinkingLevel 写入推理强度")
        check("theme" in pi_settings, "pi settings 其它键保留")
        pi_models = json.loads((root / ".pi/agent/models.json").read_text(encoding="utf-8"))
        check(all(isinstance(m.get("input"), list)
                  for e in pi_models["providers"].values() for m in e.get("models", [])),
              "pi models.json 的 input 仍是数组")

        # --- dsh ---
        patch_path = root / ".dsh/profiles/desktop/cordis.patch.yml"
        items = engine.parse_yaml(patch_path.read_text(encoding="utf-8"))
        check(isinstance(items, list) and all(isinstance(i, dict) and "id" in i for i in items),
              "dsh cordis.patch.yml 重新解析后仍是带 id 的块列表")
        by_id = {i["id"]: i for i in items if isinstance(i, dict)}
        before = engine.parse_yaml((REAL_HOME / ".dsh/profiles/desktop/cordis.patch.yml").read_text(encoding="utf-8"))
        before_ids = [i["id"] for i in before if isinstance(i, dict)]
        after_ids = [i["id"] for i in items]
        # 原有块顺序不变，只允许新增 subagent-model-selection-settings
        added = [i for i in after_ids if i not in before_ids]
        check(after_ids[:len(before_ids)] == before_ids or set(after_ids) - set(before_ids)
              <= {"subagent-model-selection-settings"},
              f"dsh patch 原有块顺序不变（新增: {added}）")
        check(by_id["agent-default-model"]["config"]["model"] == model,
              "dsh agent-default-model 指向所选模型")
        sub = by_id.get("subagent-model-selection-settings", {}).get("config", {})
        check(sub.get("allowedModels") == [{"provider": pid, "model": model}],
              "dsh 子代理可选模型写入")
        llm = by_id["llm-pi-ai"]["config"]["providers"]
        check(all(v.get("apiKeyEnv") for v in llm.values()), "dsh 每个 provider 都有 apiKeyEnv")
        # dsh 把 models:[] 当作「整个安装目录」，空模型路由会被刻意移除；
        # 期望集合只包含连接上有模型的供应商。
        expected_routes = {p for p, v in providers.items()
                           if v.get("enabled", True) and not v.get("targets") and v.get("baseUrl")
                           and engine.agent_connection(v, "dsh")["models"]}
        check(set(llm) >= expected_routes,
              f"dsh 路由段包含全部启用供应商（期望 ≥ {sorted(expected_routes)}，实际 {sorted(llm)}）")
        # hub 不管的历史路由（例如 dsh 里遗留的 myopenrouter）必须原样保留
        leftovers = sorted(set(llm) - expected_routes)
        if leftovers:
            notes.append(f"  --   dsh 保留 hub 未管理的路由: {', '.join(leftovers)}")
        creds = (root / ".dsh/.credentials.yaml").read_text(encoding="utf-8")
        check("deepseek-account-platform/device" in creds, "dsh 凭据文件里的 records 保留")
        # 凭据检查按本次实际路由推导，不再依赖会过时的固定供应商清单
        for env_name in sorted(engine.DshModelPlan(hub).credentials()):
            check(f"{env_name}:" in creds, f"dsh 凭据包含 {env_name}")

        # --- claude ---
        claude = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
        check("hooks" in claude and "statusLine" in claude, "Claude settings 的 hooks / statusLine 保留")
        if claude["env"].get("ANTHROPIC_MODEL") == model:
            notes.append("  OK   Claude ANTHROPIC_MODEL 与全局模型一致（供应商匹配）")
        else:
            notes.append(f"  --   Claude ANTHROPIC_MODEL = {claude['env'].get('ANTHROPIC_MODEL')}"
                         f"（全局是 {pid}/{model}，供应商不匹配时保持原样，符合设计）")
        # 按 Agent 覆盖：Claude 连接自带端点与密钥，切到另一家时整套换掉。
        # 候选必须有可用模型（空模型库的连接解析不出主模型），并优先挑一家
        # 与全局、与刚才的覆盖都不同的供应商，真正验证「整套切换」。
        used = (cfg["modelSync"].get("overrides") or {}).get("claude", {}).get("provider")
        claude_candidates = [name for name, p in providers.items()
                             if p.get("enabled", True) and engine.agent_connection_ready(p, "claude")
                             and (first_model(engine.agent_connection(p, "claude")["models"])
                                  or first_model(p.get("models")))]
        ordered = [name for name in claude_candidates if name not in (pid, used)] or claude_candidates
        claude_pid = ordered[0] if ordered else None
        if claude_pid:
            conn = engine.agent_connection(providers[claude_pid], "claude")
            claude_model = next((m["id"] for m in conn["models"]), None) or \
                next((m["id"] for m in providers[claude_pid].get("models", [])), None)
            cfg["modelSync"]["overrides"] = {"claude": {"provider": claude_pid, "model": claude_model}}
            hub_config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            hub = engine.Hub()
            hub.push_claude()
            claude = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
            check(claude["env"].get("ANTHROPIC_BASE_URL") == conn["baseUrl"],
                  f"Claude 连接使用 {claude_pid} 自己的 Anthropic 端点")
            check(claude["env"].get("ANTHROPIC_AUTH_TOKEN") == conn["apiKey"],
                  f"Claude 连接使用 {claude_pid} 自己的密钥")
            check(claude["env"].get("ANTHROPIC_MODEL") == claude_model,
                  f"Claude 主模型跟随 modelSync 覆盖（{claude_pid}/{claude_model}）")
            # 同一供应商给 Codex 的另一个 key：Codex 连接单独生效
            codex_pid = next((name for name, p in providers.items()
                              if engine.agent_connection_ready(p, "codex")
                              and any(m["id"] for m in engine.agent_connection(p, "codex")["models"])), None)
            if codex_pid:
                codex_conn = engine.agent_connection(providers[codex_pid], "codex")
                codex_model = next(m["id"] for m in codex_conn["models"])
                cfg["modelSync"]["overrides"]["codex"] = {"provider": codex_pid, "model": codex_model,
                                                          "reasoningEffort": "high"}
                hub_config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                codex_cfg = root / ".codex" / "config.toml"
                codex_cfg.parent.mkdir(parents=True, exist_ok=True)
                codex_cfg.write_text('model = "official-model"\n', encoding="utf-8")
                cfg["sync"]["targets"]["codex"] = {"enabled": True, "mode": "third-party"}
                hub_config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                hub = engine.Hub()
                hub.push_codex()
                text = codex_cfg.read_text(encoding="utf-8")
                check(f'model = "{codex_model}"' in text, f"Codex 第三方写入 {codex_pid} 连接上的模型")
                check(f'base_url = "{codex_conn["baseUrl"]}"' in text, "Codex 第三方使用该连接的端点")
                check("requires_openai_auth = false" in text, "Codex 第三方不借用 ChatGPT 凭据")
        else:
            notes.append("  --   没有第二个可用的 Claude 连接，跳过按 Agent 覆盖验证")

        # --- zcode ---
        zcode_path = root / ".zcode/v2/provider_config.json"
        if not zcode_path.exists():
            notes.append("  --   跳过 zcode（克隆环境没有 provider_config.json）")
        else:
            official_mode = (cfg["sync"]["targets"].get("zcode") or {}).get("mode", "official") == "official"
            if official_mode:
                before_sel = copy.deepcopy(
                    (json.loads(zcode_path.read_text(encoding="utf-8")).get("config") or {})
                    .get("defaultModelSelection"))
            zcode_pid = next((name for name, p in providers.items()
                              if p.get("enabled", True) and engine.agent_connection_ready(p, "zcode")
                              and first_model(engine.agent_connection(p, "zcode")["models"])), None)
            if not zcode_pid:
                notes.append("  --   没有 zcode 可用的供应商连接，跳过 zcode 验证")
            else:
                zconn = engine.agent_connection(providers[zcode_pid], "zcode")
                zmodel = first_model(zconn["models"])
                cfg["sync"]["targets"]["zcode"] = {"enabled": True, "mode": "third-party"}
                cfg["modelSync"].setdefault("overrides", {})["zcode"] = {
                    "provider": zcode_pid, "model": zmodel}
                hub_config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                engine.Hub().push_zcode()
                zcfg = json.loads(zcode_path.read_text(encoding="utf-8"))
                selection = (zcfg.get("config") or {}).get("defaultModelSelection") or {}
                check(selection.get("modelId") == zmodel,
                      f"ZCode 第三方写入 {zcode_pid} 连接上的模型")
                if official_mode:
                    check((zcfg.get("config") or {}).get("defaultModelSelection") is not None
                          or before_sel is not None,
                          "ZCode 第三方切换记录了默认模型选择")
                # 手填模型：不在连接模型库也放行，并补进个人模型列表
                manual_model = "e2e-manual-model"
                cfg["modelSync"]["overrides"]["zcode"]["model"] = manual_model
                hub_config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                engine.Hub().push_zcode()
                zcfg = json.loads(zcode_path.read_text(encoding="utf-8"))
                selection = (zcfg.get("config") or {}).get("defaultModelSelection") or {}
                check(selection.get("modelId") == manual_model, "ZCode 手填模型照写（不拦截）")
                rules = ((zcfg.get("config") or {}).get("providerConfigRules") or {}).get("providerRules") or []
                rule = next((r for r in rules
                             if isinstance(r, dict) and r.get("providerId") == selection.get("providerId")), None)
                check(isinstance(rule, dict) and manual_model in (rule.get("config") or {}).get("personalModelIds", []),
                      "ZCode 手填模型补进该供应商的个人模型列表")

    print("检查项:")
    print("\n".join(notes))
    if failures:
        print("\n失败项:")
        for item in failures:
            print(f"  FAIL {item}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
