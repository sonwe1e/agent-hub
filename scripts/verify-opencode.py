"""opencode 适配的独立验证：转换函数 + 真实写入 + 配置可被 opencode 读取。"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hub import opencode_providers  # noqa: E402

DEMO = {
    "demo": {
        "name": "Demo",
        "displayName": "Demo (CN)",
        "baseUrl": "https://api.demo.example/v1",
        "apiKey": "sk-demo-secret",
        "api": "openai-completions",
        "enabled": True,
        "models": [
            # context + output 齐全 → 应写出 limit
            {"id": "m-a", "name": "Model A", "contextWindow": 128000,
             "maxTokens": 8192, "input": ["text", "image"]},
            # 只有 context、缺 maxTokens → 不能写 limit（否则 opencode 丢弃该模型）
            {"id": "m-b", "name": "Model B", "contextWindow": 64000},
        ],
    },
    "anth": {
        "name": "Anth",
        "baseUrl": "https://api.anth.example",
        "apiKey": "sk-anth",
        "api": "anthropic-messages",
        "enabled": True,
        "models": [{"id": "claude-ish", "name": "Claude-ish",
                    "contextWindow": 200000, "maxTokens": 64000}],
    },
    "nokey": {
        "name": "NoKey",
        "baseUrl": "https://api.nokey.example",
        "apiKey": "",
        "api": "openai-completions",
        "enabled": True,
        "models": [{"id": "x", "name": "X"}],
    },
    "off": {
        "name": "Disabled",
        "baseUrl": "https://api.off.example",
        "apiKey": "sk-off",
        "enabled": False,
        "models": [{"id": "y", "name": "Y"}],
    },
}


def main() -> int:
    problems: list[str] = []
    out = opencode_providers(DEMO)

    print("=== 转换结果 ===")
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if set(out) != {"demo", "anth"}:
        problems.append(f"应只保留 demo/anth（跳过缺 key 与已停用），实际 {sorted(out)}")
    if out["demo"]["options"]["baseURL"] != "https://api.demo.example/v1":
        problems.append("baseURL 未映射到 options.baseURL")
    if out["demo"]["models"]["m-a"].get("limit") != {"context": 128000, "output": 8192}:
        problems.append(f"limit 应为 context+output，实际 {out['demo']['models']['m-a'].get('limit')}")
    # 这是实测出来的硬规则：只含 context 的 limit 会让 opencode 丢弃整条模型
    if "limit" in out["demo"]["models"]["m-b"]:
        problems.append(f"缺 maxTokens 时不应写 limit，实际 {out['demo']['models']['m-b']['limit']}")
    if out["demo"]["models"]["m-a"]["provider"]["npm"] != "@ai-sdk/openai-compatible":
        problems.append("openai-completions 未映射到 openai-compatible")
    if "api" in out["demo"]["models"]["m-a"]["provider"]:
        problems.append("模型 provider.api 不是协议名，不应覆盖供应商的 baseURL")
    if out["anth"]["models"]["claude-ish"]["provider"]["npm"] != "@ai-sdk/anthropic":
        problems.append("anthropic-messages 未映射到 @ai-sdk/anthropic")

    # 真实写入：把 provider id 加上随机后缀，避开 opencode 之前测试留下的内存缓存
    print("\n=== 写入并交给 opencode 校验 ===")
    tag = uuid.uuid4().hex[:8]
    probe = {}
    for pid, entry in out.items():
        renamed = f"verify{tag}{pid}"
        probe[renamed] = copy.deepcopy(entry)
    with tempfile.TemporaryDirectory(prefix="oc-verify-") as tmp:
        cfg_path = Path(tmp) / "opencode.json"
        first_pid = next(iter(probe))
        first_model = next(iter(probe[first_pid]["models"]))
        data = {"$schema": "https://opencode.ai/config.json",
                "provider": probe,
                "model": f"{first_pid}/{first_model}",
                # 非 provider 段必须原样保留
                "mcp": {"keep-me": {"type": "local", "command": ["echo"], "enabled": True}}}
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        env = dict(os.environ, OPENCODE_CONFIG=str(cfg_path))
        exe = shutil.which("opencode") or "opencode.cmd"
        listed: list[str] = []
        try:
            res = subprocess.run([exe, "models", "--standalone"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=120, env=env)
            listed = [l.strip() for l in res.stdout.splitlines() if l.strip()]
            if res.returncode != 0:
                problems.append(f"opencode models 退出码 {res.returncode}: {res.stderr[:200]}")
        except Exception as exc:
            problems.append(f"opencode models 调用失败: {exc}")

        hub_listed = [x for x in listed if x.startswith(f"verify{tag}")]
        print(f"opencode 共识别 {len(listed)} 个模型；本次 Hub 的：")
        for name in hub_listed:
            print("  -", name)
        expect = {f"{pid}/{mid}" for pid, entry in probe.items() for mid in entry["models"]}
        missing = sorted(expect - set(hub_listed))
        if missing:
            problems.append(f"opencode 未识别: {missing}")
        if any(x.startswith(("nokey/", "off/")) for x in listed):
            problems.append("缺 key / 已停用的供应商不应出现在模型列表里")

    if problems:
        print(f"\n发现 {len(problems)} 个问题:")
        for p in problems:
            print("  -", p)
        return 1
    print("\n[1/2] 转换层通过：映射正确、真实 opencode 能识别、limit 规则与跳过逻辑正确")

    # ---- 端到端：真实调用 hub.push_opencode()，写盘后读回 ----
    print("\n=== push_opencode 端到端（隔离 HOME）===")
    import hub as hubmod

    with tempfile.TemporaryDirectory(prefix="oc-e2e-") as tmp:
        home = Path(tmp) / "home"
        hdir = Path(tmp) / "hub"
        (home / ".config" / "opencode").mkdir(parents=True)
        hdir.mkdir(parents=True)
        # 目标文件里放一段"用户自己的"配置，验证 provider 以外的内容被保留
        target = home / ".config" / "opencode" / "opencode.json"
        target.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "mcp": {"my-server": {"type": "local", "command": ["node", "x.js"], "enabled": True}},
            "skills": {"paths": ["./my-skills"]},
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        cfg = {
            "version": 1,
            "providers": {
                "demo": {"name": "Demo", "baseUrl": "https://api.demo.example/v1",
                         "apiKey": "sk-demo", "api": "openai-completions", "enabled": True,
                         "models": [{"id": "m-a", "name": "Model A",
                                     "contextWindow": 128000, "maxTokens": 8192}]},
            },
            "modelSync": {"default": {"provider": "demo", "model": "m-a"}},
        }
        cfg_path = hdir / "providers.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        old = (hubmod.HOME, hubmod.HUB_DIR, hubmod.HUB_CONFIG, hubmod.HUB_STATE, hubmod.HUB_BACKUP)
        try:
            hubmod.HOME = home
            hubmod.HUB_DIR = hdir
            hubmod.HUB_CONFIG = cfg_path
            hubmod.HUB_STATE = hdir / "state.json"
            hubmod.HUB_BACKUP = Path(tmp) / "backups"
            engine = hubmod.Hub()
            engine.push_opencode()
        finally:
            (hubmod.HOME, hubmod.HUB_DIR, hubmod.HUB_CONFIG, hubmod.HUB_STATE, hubmod.HUB_BACKUP) = old

        written = json.loads(target.read_text(encoding="utf-8"))
        print(f"写入 {target}，顶层键: {sorted(written)}")
        if sorted(written.get("provider") or {}) != ["demo"]:
            problems.append(f"provider 段应完全由 Hub 生成，实际 {sorted(written.get('provider') or {})}")
        if written.get("model") != "demo/m-a":
            problems.append(f"顶层 model 应为 demo/m-a，实际 {written.get('model')!r}")
        if "my-server" not in (written.get("mcp") or {}):
            problems.append("用户的 mcp 配置被覆盖了（应保留）")
        if (written.get("skills") or {}).get("paths") != ["./my-skills"]:
            problems.append("用户的 skills 配置被覆盖了（应保留）")
        if not engine_ok_limit(written):
            problems.append("写出的 limit 不完整（opencode 会丢弃该模型）")

    if problems:
        print(f"\n端到端发现 {len(problems)} 个问题:")
        for p in problems:
            print("  -", p)
        return 1
    print("push_opencode 端到端通过：provider 段完全接管、非 provider 配置保留、model 指向正确")
    return 0


def engine_ok_limit(written: dict) -> bool:
    for entry in (written.get("provider") or {}).values():
        for model in (entry.get("models") or {}).values():
            limit = model.get("limit")
            if limit is not None and not {"context", "output"} <= set(limit):
                return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
