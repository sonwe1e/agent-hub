# -*- coding: utf-8 -*-
"""端到端验证：在真实客户端配置的副本上跑一次 push，检查每个适配器的产物。

不会写用户的真实 HOME：先把真实配置复制到临时目录，再把 engine.HOME 指过去。
用法: python scripts/verify-modelsync-e2e.py
"""
from __future__ import annotations

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
    enabled = [pid for pid, p in providers.items()
               if p.get("enabled", True) and p.get("models") and not p.get("targets")]
    if not enabled:
        print("没有可用的 OpenAI 兼容供应商，无法验证")
        return 1
    pid = "deepseek" if "deepseek" in enabled else enabled[0]
    model = providers[pid]["models"][0]["id"]
    cfg["modelSync"] = {"default": {"provider": pid, "model": model, "reasoningEffort": "high"}}
    print(f"验证用模型: {pid} / {model}\n")

    with tempfile.TemporaryDirectory(prefix="agent-hub-e2e-") as tmp:
        root = Path(tmp)
        clone_home(root)
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
        expected_routes = {p for p, v in providers.items()
                           if v.get("enabled", True) and not v.get("targets") and v.get("baseUrl")}
        check(set(llm) >= expected_routes,
              f"dsh 路由段包含全部启用供应商（期望 ≥ {sorted(expected_routes)}，实际 {sorted(llm)}）")
        # hub 不管的历史路由（例如 dsh 里遗留的 myopenrouter）必须原样保留
        leftovers = sorted(set(llm) - expected_routes)
        if leftovers:
            notes.append(f"  --   dsh 保留 hub 未管理的路由: {', '.join(leftovers)}")
        creds = (root / ".dsh/.credentials.yaml").read_text(encoding="utf-8")
        check("deepseek-account-platform/device" in creds, "dsh 凭据文件里的 records 保留")
        for key in ("FIREWORK_API_KEY", "MCODE_DEEPSEEK_API_KEY", "STEPFUN_API_KEY",
                    "MYOPENROUTER_API_KEY"):
            check(f"{key}:" in creds, f"dsh 凭据包含 {key}")

        # --- claude ---
        claude = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
        check("hooks" in claude and "statusLine" in claude, "Claude settings 的 hooks / statusLine 保留")
        if claude["env"].get("ANTHROPIC_MODEL") == model:
            notes.append("  OK   Claude ANTHROPIC_MODEL 与全局模型一致（供应商匹配）")
        else:
            notes.append(f"  --   Claude ANTHROPIC_MODEL = {claude['env'].get('ANTHROPIC_MODEL')}"
                         f"（全局是 {pid}/{model}，供应商不匹配时保持原样，符合设计）")

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
