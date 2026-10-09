#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Hub — 统一 provider 配置同步引擎
=====================================
单一事实来源: 本目录下的 providers.json（目录可整体搬迁）
把 stepfun / firework / deepseek / openrouter 等 provider 同步到
pi / mcode / zcode / dsh / cc-switch / omp 等 agent 的原生配置格式。

用法:
  hub status              查看各 agent 当前 provider 概况
  hub push                将 providers.json 写入所有已启用 agent
  hub pull [agent]        从指定 agent 拉取 provider 变更到 hub
  hub sync                检测变更并双向对齐 (hash 比对)
  hub watch               持续监听并自动同步 (Ctrl+C 退出)
  hub remove-provider ID  从 hub 与所有 agent 删除某个 provider
  hub backup              备份所有目标配置文件
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

HOME = Path.home()
# Hub 自身（代码 + providers.json + state.json + backups）不再绑定用户主目录，
# 按本文件位置定位，整个目录可任意搬迁（如 ~/.agent-hub -> G:\workspaces\agent-hub）。
# 注意 HOME 仍指向用户主目录，各 agent 的配置文件（~/.pi、~/.dsh 等）不受影响。
HUB_DIR = Path(__file__).resolve().parent
HUB_CONFIG = HUB_DIR / "providers.json"
HUB_STATE = HUB_DIR / "state.json"
HUB_BACKUP = HUB_DIR / "backups"

# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_epoch_ms() -> int:
    """SQLite 整数毫秒时间戳。

    cc-switch 的 providers.created_at 声明为 INTEGER（epoch 毫秒），app 启动
    setup 阶段按整数读取。写 datetime('now') 得到 TEXT，cc-switch 会直接崩
    （"Invalid column type Text at index: 5, name: created_at"），整个 app 打不开。
    """
    return int(time.time() * 1000)


def configure_stdio() -> None:
    """让命令行输出在 Windows 控制台上不会因编码崩溃。

    控制台代码页通常是 GBK：中文能正常显示，但 ✓ / ✗ 这类字符会抛
    UnicodeEncodeError。保留原编码、只把无法编码的字符降级替换。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def console_safe(text: str) -> str:
    """控制台编码放不下 ✓ / ✗ 时退化成 ASCII 标记。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return text.replace("✓", "[OK]").replace("✗", "[FAIL]").replace("·", "-")
    return text


def expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def target_files(name: str, config: dict) -> list[str]:
    """Never let legacy monitoring lists widen native adapter boundaries."""
    if name == "codex":
        return [str(HOME / ".codex" / "config.toml"),
                str(HOME / ".codex" / ".agent-hub-state.json")]
    if name == "claude":
        return [str(HOME / ".claude" / "settings.json")]
    return list(config.get("files", []))


def file_hash(path: Path) -> str:
    if not path.exists():
        return "missing"
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def strip_json_comments(text: str) -> str:
    """Remove JSONC comments without interpreting comment markers inside strings."""
    out, index, quoted = [], 0, False
    while index < len(text):
        char = text[index]
        if quoted:
            out.append(char)
            if char == "\\" and index + 1 < len(text):
                index += 1
                out.append(text[index])
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif text.startswith("//", index):
            end = text.find("\n", index + 2)
            index = len(text) if end == -1 else end
            out.append("\n")
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ValueError("Unclosed JSON comment")
            out.append(" ")
            index = end + 2
            continue
        else:
            out.append(char)
        index += 1
    return "".join(out)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(strip_json_comments(path.read_text(encoding="utf-8")))


def fingerprint(value: Any) -> str:
    """托管记录比对用的稳定摘要；与 codex_config.fingerprint 同规则。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()



def save_json(path: Path, data: Any, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 保护: 写 providers.json 前若模型数量大幅减少则先快照
    if path.name == "providers.json" and path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            old_n = sum(_model_count(old, pid) for pid in (old.get("providers") or {}))
            new_n = sum(_model_count(data, pid) for pid in (data.get("providers") or {}))
            dropped = old_n > 0 and new_n < old_n * 0.5
            if not dropped:
                # 总量没腰斩也要留证：单个供应商被清空同样是事故信号
                for pid, entry in (old.get("providers") or {}).items():
                    before = _model_count(old, pid)
                    after = _model_count(data, pid)
                    if before > 0 and after < before * 0.5:
                        dropped = True
                        break
            if dropped:
                snap = path.with_suffix(f".json.drop-{old_n}-{new_n}")
                snap.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
    tmp.replace(path)


def backup_file(path: Path) -> None:
    if not path.exists():
        return
    HUB_BACKUP.mkdir(parents=True, exist_ok=True)
    # GUI 中连续保存 / 批量操作可能发生在同一秒，避免覆盖前一次备份。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = HUB_BACKUP / f"{path.name}.{stamp}"
    shutil.copy2(path, dest)


class ModelLossError(RuntimeError):
    """整文件写入会丢掉本次操作范围之外的模型时抛出，阻止静默数据丢失。"""


def _model_ids(cfg: Any, pid: str) -> set[str]:
    """供应商在所有连接上的模型 ID：默认连接 + 各 Agent 覆盖。"""
    entry = (cfg or {}).get("providers", {}).get(pid) or {}
    ids = {m.get("id") for m in (entry.get("models") or []) if isinstance(m, dict)}
    agents = entry.get("agents")
    if isinstance(agents, dict):
        for override in agents.values():
            if isinstance(override, dict):
                ids |= {m.get("id") for m in (override.get("models") or []) if isinstance(m, dict)}
    return ids


def _model_count(cfg: Any, pid: str) -> int:
    entry = (cfg or {}).get("providers", {}).get(pid) or {}
    total = len([m for m in (entry.get("models") or []) if isinstance(m, dict)])
    agents = entry.get("agents")
    if isinstance(agents, dict):
        for override in agents.values():
            if isinstance(override, dict):
                total += len([m for m in (override.get("models") or []) if isinstance(m, dict)])
    return total


# ---------------------------------------------------------------------------
# 多 agent 模型选择 (providers.json -> modelSync)
# ---------------------------------------------------------------------------

# 每个平台可单独指定的模型档位。pi 没有子代理概念，只有 agent 主模型；
# dsh 的 subagent 档位与 Claude Code 的 opus/sonnet/haiku/subagent 档位
# 各自落到客户端原生配置的对应字段。
AGENT_MODEL_TIERS: dict[str, tuple[str, ...]] = {
    "pi": ("agent",),
    "dsh": ("agent", "subagent"),
    # ZCode 持久化一个主模型选择；它的 provider / model 目录由同一份
    # agents.zcode 连接提供。
    "zcode": ("agent",),
    # Claude Code 的主模型档位叫 model（→ ANTHROPIC_MODEL）；agent 是 pi/dsh/codex
    # 的叫法，这里不重复解析，避免同一选择出现两个档位。
    "claude": ("model", "opus", "sonnet", "haiku", "fable", "subagent"),
    # Codex 只有一个顶层 model；codex-auto-review 之类子代理不在本工具管理范围。
    "codex": ("agent",),
}

REASONING_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
# Codex 用 none 表示关闭推理；max / ultra 是否被具体模型接受由客户端自己判断。
CODEX_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
# Claude Code 连接默认只走 Anthropic Messages；Codex 第三方只走 Responses。
AGENT_DEFAULT_API = {"claude": "anthropic-messages", "codex": "openai-responses"}
# 每个供应商在这几个 Agent 上的连接会被界面与状态展示（含未声明天认的默认连接）。
AGENT_CONNECTION_TARGETS = ("pi", "dsh", "zcode", "claude", "codex")
ZCODE_API_TYPES = {
    "openai-completions": "openai-chat-completions",
    "openai-chat-completions": "openai-chat-completions",
    "openai-responses": "openai-responses",
    "anthropic-messages": "anthropic-messages",
}
# 反向映射：从 ZCode 原生 api.type 还原 Hub 的协议词表（用于供应商导入）。
ZCODE_API_REVERSE = {
    "openai-chat-completions": "openai-completions",
    "openai-responses": "openai-responses",
    "anthropic-messages": "anthropic-messages",
}


def canonical_target(target: str) -> str:
    """dsh-desktop 与 dsh 共用同一份配置，连接与档位都按 dsh 解析。"""
    return "dsh" if target == "dsh-desktop" else target


def agent_connection(provider: dict, target: str) -> dict:
    """provider 面向某个 Agent 的连接：默认连接 + ``agents.<agent>`` 覆盖。

    一个供应商可以同时服务多个 Coding Agent，也可以给不同 Agent 配不同的
    端点、密钥和模型（同一网关的不同 key 往往对应不同模型集合）。未声明
    ``agents.<agent>`` 时该 Agent 直接用顶层默认连接，行为与旧版一致。

    兼容旧字段：``anthropicBaseUrl`` → claude 连接的 baseUrl，
    ``openaiApiKey`` / ``apiKeyEnv`` / ``codexWireApi`` → codex 连接，
    ``claudeEnv`` / ``claudeSettings`` → claude 连接的 env / settings。
    """
    canonical = canonical_target(target)
    agents = provider.get("agents")
    agents = agents if isinstance(agents, dict) else {}
    override = agents.get(canonical)
    override = override if isinstance(override, dict) else {}

    def pick(key: str, default=None):
        if key in override and override[key] is not None:
            return override[key]
        return default

    base_url = pick("baseUrl", provider.get("baseUrl") or "")
    api_key = pick("apiKey", provider.get("apiKey") or "")
    api = pick("api", provider.get("api") or "openai-completions")
    api_key_env = pick("apiKeyEnv", provider.get("apiKeyEnv") or "")
    models = pick("models", provider.get("models") or [])
    models = models if isinstance(models, list) else []
    compat = pick("compat", provider.get("compat"))
    env = pick("env", {}) if isinstance(pick("env"), dict) else {}
    settings = pick("settings", {}) if isinstance(pick("settings"), dict) else {}

    if canonical == "claude":
        # Claude Code 只认 Anthropic Messages 端点，且必须显式声明：
        # agents.claude.baseUrl、旧 anthropicBaseUrl，或整个 provider 就是
        # anthropic-messages 协议。没声明就没有 Claude 连接（默认的 OpenAI
        # 端点不能直接给 Claude Code 用）。
        legacy_base = provider.get("anthropicBaseUrl")
        legacy_base = legacy_base if isinstance(legacy_base, str) else ""
        override_base = override.get("baseUrl")
        override_base = override_base if isinstance(override_base, str) else ""
        if override_base.strip():
            base_url = override_base
        elif legacy_base.strip():
            base_url = legacy_base
        elif provider.get("api") == AGENT_DEFAULT_API["claude"]:
            base_url = provider.get("baseUrl") or ""
        else:
            base_url = ""
        api = override.get("api") or (AGENT_DEFAULT_API["claude"] if base_url.strip() else api)
        if not override.get("env") and isinstance(provider.get("claudeEnv"), dict):
            env = provider["claudeEnv"]
        if not override.get("settings") and isinstance(provider.get("claudeSettings"), dict):
            settings = provider["claudeSettings"]
    if canonical == "codex":
        if override.get("apiKey") is None and isinstance(provider.get("openaiApiKey"), str) \
                and provider["openaiApiKey"].strip():
            api_key = provider["openaiApiKey"]
        if override.get("apiKeyEnv") is None and isinstance(provider.get("apiKeyEnv"), str) \
                and provider["apiKeyEnv"].strip():
            api_key_env = provider["apiKeyEnv"]
        if not override.get("api"):
            api = AGENT_DEFAULT_API["codex"] if provider.get("codexWireApi") == "responses" \
                else (provider.get("api") or "openai-completions")
    return {"baseUrl": base_url if isinstance(base_url, str) else "",
            "apiKey": api_key if isinstance(api_key, str) else "",
            "apiKeyEnv": api_key_env if isinstance(api_key_env, str) else "",
            "api": api if isinstance(api, str) else "openai-completions",
            "models": models, "compat": compat, "env": env, "settings": settings}


def agent_connection_ready(provider: dict, target: str) -> bool:
    """该 provider 是否能给这个 Agent 用：端点齐全且协议匹配。

    Claude Code 需要显式声明的 Anthropic Messages 端点；Codex 第三方线路只接受
    Responses。没有对应连接的供应商不会进入该平台的同步。
    """
    canonical = canonical_target(target)
    conn = agent_connection(provider, canonical)
    if not conn["baseUrl"].strip():
        return False
    if canonical == "claude":
        return conn["api"] == "anthropic-messages"
    if canonical == "codex":
        return conn["api"] == "openai-responses"
    if canonical == "zcode":
        return conn["api"] in ZCODE_API_TYPES
    return True


def provider_agents(provider: dict) -> list[str]:
    """该 provider 显式声明了连接覆盖的 Agent 名。"""
    agents = provider.get("agents")
    return sorted(agents) if isinstance(agents, dict) else []


def _norm_pid_model(pid: Any, model: Any) -> tuple[str, str] | None:
    if not isinstance(pid, str) or not pid.strip():
        return None
    if not isinstance(model, str) or not model.strip():
        return None
    return pid.strip(), model.strip()


def _cfg_providers(cfg: Any) -> dict:
    providers = (cfg or {}).get("providers")
    return providers if isinstance(providers, dict) else {}


def provider_targets(p: dict, target: str) -> bool:
    """Whether a provider is configured for an adapter target."""
    return not p.get("targets") or canonical_target(target) in p["targets"]


def claude_providers(providers: dict) -> dict:
    """The providers that the Claude Code adapter can actually use."""
    return {pid: p for pid, p in providers.items()
            if isinstance(p, dict) and provider_targets(p, "claude")
            and p.get("enabled", True) and agent_connection_ready(p, "claude")}


def agent_model_tiers(cfg: Any, target: str) -> dict[str, dict]:
    """解析某平台每个档位实际要用的模型，不做写入。

    取值优先级（从高到低）：
      1. modelSync.overrides.<target>.tiers.<tier> —— 该档位被显式指定；
      2. modelSync.overrides.<target>.{provider,model} —— 平台级指定；
      3. provider.agentModel —— 供应商声明的 agent 默认模型；
      4. modelSync.default.{provider,model} —— 全局主模型；
      5. 供应商该连接模型列表的第一项 —— 兜底。

    第 2~5 条只作用于「主模型」（pi 的 agent、dsh 的 agent、Claude Code 的 model 档）；
    Claude Code 的 opus/sonnet/haiku/fable/subagent 档位不会被一个全局设置改写，
    只有 modelSync 显式指定过才写。

    可用供应商 = 通过 targets 限定、已启用、且该 Agent 连接就绪（端点 + 协议匹配）。
    「模型是否已知」按该连接的模型库判断：同一供应商给不同 Agent 配不同 key 时，
    各自连接的模型库可以完全不同。

    每个档位返回 {"provider","model","reasoningEffort","known","tier"}；
    `known` 表示该模型 ID 是否出现在该连接的模型库里（手填值会是 False）。
    """
    canonical = canonical_target(target)
    tier_names = AGENT_MODEL_TIERS.get(canonical, ())
    if not tier_names:
        return {}
    providers = _cfg_providers(cfg)
    sync = (cfg or {}).get("modelSync")
    sync = sync if isinstance(sync, dict) else {}
    default = sync.get("default") if isinstance(sync.get("default"), dict) else {}
    overrides = sync.get("overrides") if isinstance(sync.get("overrides"), dict) else {}
    override = overrides.get(canonical) if isinstance(overrides.get(canonical), dict) else {}
    override_tiers = override.get("tiers") if isinstance(override.get("tiers"), dict) else {}
    available = {
        pid: p for pid, p in providers.items()
        if isinstance(p, dict) and provider_targets(p, canonical)
        and p.get("enabled", True) and agent_connection_ready(p, canonical)
    }

    def pick(*candidates: Any) -> dict | None:
        for candidate in candidates:
            if isinstance(candidate, dict):
                parsed = _norm_pid_model(candidate.get("provider"), candidate.get("model"))
                if parsed:
                    effort = candidate.get("reasoningEffort")
                    allowed = CODEX_REASONING_EFFORTS if canonical == "codex" else REASONING_EFFORTS
                    return {"provider": parsed[0], "model": parsed[1],
                            "reasoningEffort": effort if effort in allowed else None}
        return None

    def pick_available(candidate: Any) -> dict | None:
        selected = pick(candidate)
        return selected if selected and selected["provider"] in available else None

    def provider_models(pid: str) -> list[str]:
        entry = providers.get(pid)
        entry = entry if isinstance(entry, dict) else {}
        return [m["id"] for m in agent_connection(entry, canonical)["models"]
                if isinstance(m, dict) and isinstance(m.get("id"), str)]

    def fallback_for(pid: str) -> dict | None:
        if pid not in available:
            return None
        entry = providers.get(pid)
        entry = entry if isinstance(entry, dict) else {}
        agent_model = entry.get("agentModel")
        if isinstance(agent_model, str) and agent_model.strip():
            return {"provider": pid, "model": agent_model.strip(), "reasoningEffort": None}
        first = provider_models(pid)
        if first:
            return {"provider": pid, "model": first[0], "reasoningEffort": None}
        return None

    # 平台级覆盖：overrides.<target> 可带 tiers 子对象（每个档位单独指定）
    override_pid = override.get("provider")
    base = (pick_available(override)
            or (fallback_for(override_pid) if isinstance(override_pid, str) else None)
            or pick_available(default)
            or (fallback_for(default["provider"]) if isinstance(default.get("provider"), str) else None))

    out: dict[str, dict] = {}
    for tier in tier_names:
        resolved = None
        if canonical == "claude":
            # 1) 该档位被界面显式指定（供应商必须具备 Claude 连接）
            resolved = pick_available(override_tiers.get(tier))
            if resolved is None and tier == "model":
                # 2) 主模型档位：平台级覆盖 / 全局主模型。baseUrl 与 apiKey 必须
                #    配套，所以只认「该供应商确实有 Claude 连接」的选择。
                for candidate in (override, default):
                    picked = pick(candidate)
                    if picked and picked["provider"] in available:
                        resolved = picked
                        break
            # 3) 别名档位（opus/sonnet/haiku/fable/subagent）没有兜底：未显式
            #    指定的档位不写，避免替用户改掉 Claude Code 的配置。
        else:
            # pi 的 agent 档、dsh 的 agent/subagent 档、Codex 的 agent 档
            # 都跟随同一个主模型
            resolved = base
        if resolved is None:
            continue
        entry_out = dict(resolved)
        entry_out["tier"] = tier
        entry_out["known"] = entry_out["model"] in provider_models(entry_out["provider"])
        out[tier] = entry_out
    return out


def agent_model_tiers_public(cfg: Any, target: str) -> dict[str, dict]:
    """给界面/API 用的档位视图。

    与 agent_model_tiers 的区别：隐藏历史档位 `fable`。适配器仍通过
    agent_model_tiers 读到 fable，这样旧配置里的 ANTHROPIC_DEFAULT_FABLE_MODEL
    该写就写、该收回就收回。
    """
    tiers = agent_model_tiers(cfg, target)
    canonical = canonical_target(target)
    if canonical == "claude":
        tiers = {k: v for k, v in tiers.items() if k != "fable"}
    return tiers


def resolve_agent_model(cfg: Any, target: str) -> dict | None:
    """某平台主 agent 实际使用的模型；未配置或该平台不受管理时返回 None。"""
    tiers = agent_model_tiers(cfg, target)
    if not tiers:
        return None
    canonical = "dsh" if target == "dsh-desktop" else target
    if canonical == "claude":
        # Claude Code 的主模型就是 ANTHROPIC_MODEL 档；adapters 也按这一档决定供应商
        return tiers.get("model")
    return tiers.get("agent") or tiers.get("model")


def agent_model_options(cfg: Any, target: str | None = None) -> dict[str, list[dict]]:
    """每个供应商可供选择的模型（精简字段，供界面下拉使用）。

    `target` 为 None 时返回该供应商所有连接合并后的模型（模型库总览用）；
    指定 target 时只返回该 Agent 连接上的模型——同一供应商给不同 Agent 配
    不同 key 时，下拉里只应出现这条连接真正提供的模型。
    """
    out: dict[str, list[dict]] = {}
    for pid, entry in _cfg_providers(cfg).items():
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        if target is None:
            sources = [entry.get("models") or []]
            sources += [agent_connection(entry, name)["models"] for name in provider_agents(entry)]
        else:
            sources = [agent_connection(entry, target)["models"]]
        models, seen = [], set()
        for source in sources:
            for m in source or []:
                if not isinstance(m, dict) or not isinstance(m.get("id"), str):
                    continue
                if m["id"] in seen:
                    continue
                seen.add(m["id"])
                models.append({"id": m["id"], "name": m.get("name", m["id"])})
        out[pid] = models
    return out


def agent_connections(cfg: Any) -> dict[str, dict[str, dict]]:
    """每个供应商在每个 Agent 上的连接概况（界面与状态展示用，不含密钥）。"""
    out: dict[str, dict[str, dict]] = {}
    for pid, entry in _cfg_providers(cfg).items():
        if not isinstance(entry, dict):
            continue
        agents = {}
        for name in AGENT_CONNECTION_TARGETS:
            conn = agent_connection(entry, name)
            agents[name] = {
                "ready": agent_connection_ready(entry, name),
                "overridden": name in provider_agents(entry),
                "baseUrl": conn["baseUrl"],
                "api": conn["api"],
                "hasKey": bool(conn["apiKey"].strip() or conn["apiKeyEnv"].strip()),
                "modelCount": len([m for m in conn["models"] if isinstance(m, dict)]),
            }
        out[pid] = agents
    return out


def assert_expected_model_changes(
    disk_cfg: Any,
    new_cfg: Any,
    *,
    allowed_removed: dict[str, set[str]] | None = None,
    removed_providers: set[str] | None = None,
) -> None:
    """写入 providers.json 前校验：除本次操作明确的删除外，不允许丢失任何模型。

    `disk_cfg` 是磁盘上的当前配置，`new_cfg` 是即将写入的配置。
    历史事故（pull_from / 旧版 GUI 回写陈旧快照）都是"模型悄悄变少"，
    这里把这类写入挡在落盘之前。
    """
    allowed_removed = allowed_removed or {}
    removed_providers = removed_providers or set()
    disk_providers = (disk_cfg or {}).get("providers") or {}
    new_providers = (new_cfg or {}).get("providers") or {}
    for pid, entry in disk_providers.items():
        old_ids = _model_ids(disk_cfg, pid)
        if not old_ids:
            continue
        if pid not in new_providers:
            if pid not in removed_providers:
                raise ModelLossError(f"拒绝写入 providers.json：供应商 {pid} 会被整体删除")
            continue
        new_ids = _model_ids(new_cfg, pid)
        unexpected = old_ids - new_ids - set(allowed_removed.get(pid, ()))
        if unexpected:
            preview = "、".join(sorted(unexpected)[:5])
            more = f" 等 {len(unexpected)} 个" if len(unexpected) > 5 else ""
            raise ModelLossError(
                f"拒绝写入 providers.json：{pid} 将丢失本次操作范围外的模型 {preview}{more}"
            )


# ---------------------------------------------------------------------------
# TOML 定向改写 (只服务 Codex 的 config.toml：顶层键 + [model_providers.*] 段)
# ---------------------------------------------------------------------------


def _toml_dump_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_dump_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_dump_value(v) for v in value) + "]"
    return _toml_dump_string(str(value))


def _toml_find_table(lines: list[str], header: str) -> tuple[int, int] | None:
    """返回 [header] 段的 (起, 止) 行号区间（含 header，不含下一个段头）。"""
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if start is not None:
                return start, index
            if stripped == f"[{header}]":
                start = index
    if start is None:
        return None
    return start, len(lines)


def _toml_set_project_scalar(lines: list[str], key: str, value: Any) -> None:
    """设置文件开头的项目级标量（在第一个 [table] 之前）。"""
    rendered = f"{key} = {_toml_dump_value(value)}"
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    first_table = next((i for i, line in enumerate(lines)
                        if line.strip().startswith("[")), len(lines))
    for index in range(first_table):
        if pattern.match(lines[index]):
            lines[index] = rendered
            return
    lines.insert(first_table, rendered)


def _toml_set_table_keys(lines: list[str], header: str, values: dict[str, Any]) -> None:
    """在指定段里更新/新增键；段不存在时追加到文件末尾。"""
    span = _toml_find_table(lines, header)
    if span is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{header}]")
        lines.extend(f"{k} = {_toml_dump_value(v)}" for k, v in values.items())
        return
    start, end = span
    remaining = dict(values)
    for index in range(start + 1, end):
        match = re.match(r"^\s*([A-Za-z0-9_\-\.]+)\s*=", lines[index])
        if not match or match.group(1) not in remaining:
            continue
        key = match.group(1)
        lines[index] = f"{key} = {_toml_dump_value(remaining.pop(key))}"
    closing = end
    while closing > start + 1 and not lines[closing - 1].strip():
        closing -= 1
    lines[closing:closing] = [f"{k} = {_toml_dump_value(v)}" for k, v in remaining.items()]


def _toml_get_table(lines: list[str], header: str) -> dict[str, Any]:
    """只读地取出一个段的键值（值按字符串/布尔/数字做粗解析）。"""
    span = _toml_find_table(lines, header)
    if span is None:
        return {}
    out: dict[str, Any] = {}
    for line in lines[span[0] + 1:span[1]]:
        match = re.match(r"^\s*([A-Za-z0-9_\-\.]+)\s*=\s*(.+?)\s*$", line)
        if not match:
            continue
        raw = match.group(2)
        if raw.startswith('"') and raw.endswith('"'):
            out[match.group(1)] = raw[1:-1]
        elif raw.startswith("'") and raw.endswith("'"):
            out[match.group(1)] = raw[1:-1]
        elif raw in ("true", "false"):
            out[match.group(1)] = raw == "true"
        else:
            out[match.group(1)] = raw
    return out


# ---------------------------------------------------------------------------
# YAML 子集读写 (仅覆盖本工具所需的映射/列表/标量结构)
# ---------------------------------------------------------------------------


def _yaml_scalar(s: str) -> Any:
    s = s.strip()
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        text = yaml_unquote(s)
        # 带引号的正整数 / 小数 / 进制字面量都是字符串（"1000000" 就该读成 str）；
        # 只有「写入端为数字加过引号」的形态才还原成数字，否则 -1 会变成 "-1"。
        if _repr_quoted_number(text):
            return _numeric(text)
        return text
    return _numeric(s)


# 写入端会把这些形态加引号写出（安全校验器不接受它们的裸写形式），读取端必须
# 认识同一套形态才能把类型还原回来。顺序即优先级，第一个匹配的赢。
def _strip_underscores(text: str) -> str:
    return text.replace("_", "")


# (正则, 还原函数)。还原函数收 "[-+]?0x1f" 这种带符号 / 下划线的原文。
# 整数那条要带上 `+`：写入端会把 "+1" 这类字符串加引号写出，读取端得还原成 int。
_NUMBER_FORMS: tuple[tuple[re.Pattern, Any], ...] = (
    (re.compile(r"[-+]?\d[\d_]*"), lambda t: int(t)),
    (re.compile(r"[-+]?\d[\d_]*\.\d+"), lambda t: float(t)),
    (re.compile(r"[-+]?(?:\d[\d_]*\.?[\d_]*|\.\d+)[eE][-+]?\d+"), lambda t: float(t)),
    (re.compile(r"[-+]?(?:inf(?:inity)?|nan)", re.IGNORECASE), lambda t: float(t)),
    (re.compile(r"[-+]?\d[\d_]*\."), lambda t: float(t)),
    (re.compile(r"[-+]?0[bB][01_]+"), lambda t: int(_strip_underscores(t), 2)),
    (re.compile(r"[-+]?0[xX][0-9a-fA-F_]+"), lambda t: int(_strip_underscores(t), 16)),
    (re.compile(r"[-+]?0[oO][0-7_]+"), lambda t: int(_strip_underscores(t), 8)),
)


def _numeric(s: str) -> Any:
    """标量的数值还原（不能还原就原样返回字符串）。"""
    for pattern, convert in _NUMBER_FORMS:
        if pattern.fullmatch(s):
            try:
                return convert(s)
            except ValueError:
                return s
    return s


_YAML_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r",
                 "b": "\b", "f": "\f", "0": "\0", "a": "\a", "v": "\v", "e": "\x1b", " ": " "}


def yaml_unquote(raw: str) -> str:
    """去掉 YAML 标量的引号并按需反转义。

    双引号是 JSON 风格（写入端 ``json.dumps`` 用的就是它）：支持 ``\\"``、
    ``\\\\``、``\\n``、``\\t``、``\\r``、``\\uXXXX`` 等；单引号在 YAML 里没有
    转义，内容原样返回（因此 Windows 路径 ``'C:\\Program Files\\...'`` 可读）。
    无法确定语义的转义一律拒绝，不猜。
    """
    if len(raw) < 2 or raw[-1] != raw[0] or raw[0] not in "\"'":
        raise ValueError("Unsupported quoted YAML scalar")
    body = raw[1:-1]
    if raw[0] == "'":
        if "'" in body:
            raise ValueError("Unsupported quoted YAML scalar")
        return body
    out, index = [], 0
    while index < len(body):
        char = body[index]
        if char == '"':
            raise ValueError("Unsupported quoted YAML scalar")
        if char == "\\":
            index += 1
            if index >= len(body):
                raise ValueError("Unsupported quoted YAML scalar")
            esc = body[index]
            if esc == "u":
                digits = body[index + 1:index + 5]
                if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                    raise ValueError("Unsupported quoted YAML scalar")
                out.append(chr(int(digits, 16)))
                index += 5
                continue
            if esc not in _YAML_ESCAPES:
                raise ValueError("Unsupported quoted YAML scalar")
            out.append(_YAML_ESCAPES[esc])
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _yaml_key(raw: str) -> str:
    """映射键：去掉成对的引号（与安全校验器保持一致）。

    MiniMax 等客户端会写 `'1000000': hint` 这种带引号的键；两个解析器必须
    得到同一个字符串键，否则「严格子集可往返」的校验会误判配置不被支持。

    引号只能整段包住键（`"a:b"`）或出现在键中间（`x'y`）；首尾落单的引号
    （`"a: b`）必须报错，否则两个解析器会「成功」地读出不同的坏键——静默读坏
    比拒绝更危险。
    """
    key = raw.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        return yaml_unquote(key)
    if key[:1] in "\"'" or key[-1:] in "\"'":
        raise ValueError(f"Unsupported quoted YAML key: {raw!r}")
    return key


def _yaml_split_comment(line: str) -> str:
    """去掉行内注释，但不碰引号里面的 ``#``。

    旧实现用 ``re.sub(r"\\s+#.*$")`` 一刀切，会把 ``"has #comment"`` 截成
    ``"has``：写入端明明加了引号，读回来却成了坏标量。所以扫描时必须跟踪
    引号状态——引号内的 ``#`` 是内容，只有引号外、前面是空白的 ``#`` 才是注释。

    引号只在「标量开头」才算引号定界符（行首、``: `` 之后、``- `` 之后）；
    ``k: it's # 注释`` 里的撇号是普通内容，不能当成引号开引号状态，否则行内
    注释会被当成内容留下来。
    """
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is None:
            if char in "\"'" and _at_scalar_start(line, index):
                quote = char
            elif char == "#" and index > 0 and line[index - 1] in " \t":
                return line[:index].rstrip()
        elif quote == '"':
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quote = None
        elif char == "'":
            quote = None
        index += 1
    return line.rstrip()


def _at_scalar_start(line: str, index: int) -> bool:
    """index 处的引号是否处在「一个标量的开头」。"""
    before = line[:index]
    if not before.strip():
        return True                       # 行首（缩进之后）
    stripped = before.rstrip()
    if stripped.endswith(":"):
        return before != stripped         # `key: "…"`，冒号后必须有空白
    if stripped.endswith("-"):
        return before != stripped         # `- "…"`，横杠后必须有空白
    return False


def _yaml_split_key(text: str) -> tuple[str, str] | None:
    """在引号外的第一个 ``:`` 处切分「键: 值」；切不开或切出可疑结果返回 None。

    键可能自带冒号（``'a:b': 1``）或盘符（``"C:\\\\path"``），直接
    ``partition(":")`` 会在引号内部切断，于是键变成 ``"a``、值变成 ``b"``。

    只有「键的第一个字符是引号」才算引号键；键中间的引号（``a": 1``）是普通
    内容，否则这种裸键会被误判成引号键而读不回来。
    """
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is None:
            if char in "\"'" and not text[:index].strip():
                quote = char
            elif char == ":":
                key = text[:index].strip()
                rest = text[index + 1:].strip()
                if not key:
                    return None
                # 引号必须对称。合法形态只有两种：
                #   1. 整段被同一种引号包住（`"a:b"`）；
                #   2. 键的首尾都不是引号（引号只能出现在中间，如 `a"b`）。
                # 首尾出现单个引号（`"a`、`a"`）说明两个读取端会读出不同结果，
                # 交给上层 fail-closed。
                if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                    pass
                elif key[0] in "\"'" or key[-1] in "\"'":
                    return None
                return key, rest
        elif quote == '"':
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quote = None
        elif char == "'":
            quote = None
        index += 1
    return None


def _yaml_has_key(text: str) -> bool:
    return _yaml_split_key(text) is not None


def parse_yaml(text: str) -> Any:
    """极简 YAML 解析器: 支持嵌套映射、列表、标量、行内 {} / []。"""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # 去掉行内注释 (引号内的 # 是内容，不算注释)
        stripped = _yaml_split_comment(raw.rstrip())
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.strip()))

    def parse_block(idx: int, indent: int) -> tuple[Any, int]:
        if idx >= len(lines):
            return None, idx
        # 列表?
        if lines[idx][1].startswith("- ") or lines[idx][1] == "-":
            items: list[Any] = []
            while idx < len(lines) and lines[idx][0] == indent and (
                lines[idx][1].startswith("- ") or lines[idx][1] == "-"
            ):
                content = lines[idx][1][2:] if lines[idx][1].startswith("- ") else ""
                if not content:
                    # 嵌套块
                    val, idx = parse_block(idx + 1, indent + 2)
                    items.append(val)
                elif not content.startswith(("{", "[")) and _yaml_has_key(content):
                    # 列表项是映射的第一个键
                    key, rest = _yaml_split_key(content)  # type: ignore[misc]
                    obj: dict[str, Any] = {}
                    if rest:
                        obj[_yaml_key(key)] = _yaml_scalar(rest) if not rest.startswith(("{", "[")) else _parse_flow(rest)
                        idx += 1
                    else:
                        idx += 1
                        sub, idx = parse_block(idx, indent + 2)
                        obj[_yaml_key(key)] = sub
                    # 同缩进后续键
                    while idx < len(lines) and lines[idx][0] >= indent + 2 and not lines[idx][1].startswith("- "):
                        split2 = _yaml_split_key(lines[idx][1])
                        if split2 is None:
                            # 不能 break：那会把 idx 留在原地，顶层
                            # `parse_block(0, ...)` 丢掉返回的游标，于是这一行
                            # 之后的整个文档被静默吞掉。读不懂就报错。
                            raise ValueError(
                                f"Unsupported YAML line in list item: {lines[idx][1]!r}")
                        k2, r2 = split2
                        if r2:
                            obj[_yaml_key(k2)] = (
                                _yaml_scalar(r2) if not r2.startswith(("{", "[")) else _parse_flow(r2)
                            )
                            idx += 1
                        else:
                            idx += 1
                            sub, idx = parse_block(idx, lines[idx - 1][0] + 2 if False else indent + 4)
                            obj[_yaml_key(k2)] = sub
                    items.append(obj)
                else:
                    items.append(_yaml_scalar(content) if not content.startswith(("{", "[")) else _parse_flow(content))
                    idx += 1
            return items, idx

        # 映射
        obj = {}
        while idx < len(lines) and lines[idx][0] == indent and not lines[idx][1].startswith("- "):
            split = _yaml_split_key(lines[idx][1])
            if split is None:
                # 既不是「键: 值」也不是列表项：子集解析器读不懂，抛错让上层
                # fail-closed，而不是悄悄产出一个坏结果。
                raise ValueError(f"Unsupported YAML line: {lines[idx][1]!r}")
            key, rest = split
            key = _yaml_key(key)
            if rest:
                obj[key] = _yaml_scalar(rest) if not rest.startswith(("{", "[")) else _parse_flow(rest)
                idx += 1
            else:
                idx += 1
                if idx < len(lines) and lines[idx][0] > indent:
                    sub, idx = parse_block(idx, lines[idx][0])
                    obj[key] = sub
                else:
                    obj[key] = None
        return obj, idx

    def _parse_flow(s: str) -> Any:
        s = s.strip()
        if s.startswith("{") and s.endswith("}"):
            inner = s[1:-1].strip()
            if not inner:
                return {}
            result = {}
            for part in _split_flow(inner):
                # 行内映射的键也要按引号规则切分：键含冒号（{"a:b": 1}）时
                # partition(":") 会在键内部切断。
                pair = _yaml_split_key(part)
                if pair is None:
                    raise ValueError(f"Unsupported YAML flow mapping: {part!r}")
                k, v = pair
                result[_yaml_key(k)] = _yaml_scalar(v) if not v.startswith(("[", "{")) else _parse_flow(v)
            return result
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            return [_yaml_scalar(p) if not p.strip().startswith(("[", "{")) else _parse_flow(p) for p in _split_flow(inner)]
        return _yaml_scalar(s)

    def _split_flow(s: str) -> list[str]:
        """按顶层逗号切分行内集合；引号内的逗号是内容。

        旧实现只跟踪 ``{}``/``[]`` 深度，`["x,y"]` 会被切成两个元素，
        `{b: "x,y"}` 会切出一个坏片段（写入端产出的文本读不回来）。
        """
        parts, depth, cur = [], 0, []
        quote: str | None = None
        index = 0
        while index < len(s):
            ch = s[index]
            if quote is not None:
                cur.append(ch)
                if quote == '"':
                    if ch == "\\":
                        if index + 1 < len(s):
                            cur.append(s[index + 1])
                            index += 2
                            continue
                    elif ch == '"':
                        quote = None
                elif ch == "'":
                    quote = None
                index += 1
                continue
            if ch in "\"'":
                quote = ch
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
            index += 1
        if cur:
            parts.append("".join(cur))
        return parts

    # 空文档写成 `{}` / `[]`（dump_yaml 对空容器的产物）：安全校验器有专门的
    # 分支接受它们，宽松解析器也必须接受，否则写入端自己的输出读不回来。
    if len(lines) == 1 and lines[0][1] in ("{}", "[]"):
        return {} if lines[0][1] == "{}" else []
    data, end = parse_block(0, lines[0][0] if lines else 0)
    if end != len(lines):
        # 游标没走完说明有内容被静默丢弃；宁可报错，也不能让调用方拿到半份配置。
        raise ValueError(f"Unconsumed YAML content at line {end + 1}: {lines[end][1]!r}")
    return data


def dump_yaml(obj: Any, indent: int = 0) -> str:
    """把 dict/list/标量转成 YAML 文本。"""
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = []
        for k, v in obj.items():
            if isinstance(v, dict) and v:
                lines.append(f"{pad}{_fmt_key(k)}:")
                lines.append(dump_yaml(v, indent + 1))
            elif isinstance(v, list) and v:
                lines.append(f"{pad}{_fmt_key(k)}:")
                lines.append(dump_yaml(v, indent + 1))
            elif isinstance(v, dict):
                lines.append(f"{pad}{_fmt_key(k)}: {{}}")
            elif isinstance(v, list):
                lines.append(f"{pad}{_fmt_key(k)}: []")
            elif v is None:
                lines.append(f"{pad}{_fmt_key(k)}: null")
            elif isinstance(v, bool):
                lines.append(f"{pad}{_fmt_key(k)}: {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{pad}{_fmt_key(k)}: {_fmt_number(v)}")
            else:
                lines.append(f"{pad}{_fmt_key(k)}: {_fmt_str(v)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return f"{pad}[]"
        lines = []
        for item in obj:
            if isinstance(item, dict):
                keys = list(item.keys())
                if not keys:
                    lines.append(f"{pad}- {{}}")
                    continue
                first = keys[0]
                rest = {k: item[k] for k in keys[1:]}
                # 列表项里映射的第一个键走 _fmt_key：键含冒号时裸写会让读取端
                # 在错误的位置切开（`- a:b: {k: v}` 被读成 a -> 'b: {k: v}'）。
                head = f"{pad}- {_fmt_key(first)}: {_fmt_val_inline(item[first])}"
                lines.append(head)
                if rest:
                    sub = dump_yaml(rest, indent + 1)
                    # 把续行对齐到 "- " 之后
                    sub_lines = sub.splitlines()
                    for i, sl in enumerate(sub_lines):
                        if i == 0:
                            lines.append(sl)
                        else:
                            lines.append(sl)
            elif isinstance(item, list):
                if not item:
                    # 空列表占位必须写成 `- []`：裸写 `[]` 会变成一行独立的
                    # `[]`，两个读取端都读不懂。
                    lines.append(f"{pad}- []")
                else:
                    lines.append(f"{pad}-")
                    lines.append(dump_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}- {_fmt_val_inline(item)}")
        return "\n".join(lines)
    return f"{pad}{_fmt_val_inline(obj)}"


def _fmt_key(key: Any) -> str:
    """映射键的 YAML 写法。

    含 ``:``、引号、空白+#、控制字符、以 YAML 指示符或 ``#`` 开头、或本身像
    布尔 / 空值的键必须加引号：不加引号时安全校验读不回来（``#lead:`` 整行会被
    当成注释丢掉，``ovh'cloud:`` 会让两个读取端读出不同结果），工具自己的输出
    必须能被自己安全地读回。模型 ID（含 ``[ ] /`` 等）不需要引号。
    """
    s = str(key)
    if (not s or ":" in s or s[0] in _UNQUOTED_START or any(c.isspace() for c in s)
            or _needs_escaped_quote(s) or _NEEDS_QUOTE.search(s)
            or s.lower() in _YAML_WORDS or _numberish(s)):
        return _quoted(s)
    return s


# 看起来像布尔 / 空值 / 数字的字符串：必须加引号，否则读回来类型就变了。
# 单个数字形态统一由 _NUMBER_FORMS 判定，避免两处列表各自漂移。
_YAML_WORDS = frozenset((
    "true", "false", "null", "~", "yes", "no", "on", "off"))


def _bare_number(text: str) -> bool:
    """裸写形态会被安全校验器接受、且读成数字的形态。

    校验器对以 ``-`` / ``+`` / ``?`` / ``.`` 开头的标量一律判为 Ambiguous，
    所以只有无符号的整数与小数能裸写：``128000`` / ``1.5`` 可以，``-1`` 不行。
    这类字符串必须加引号才能保住字符串类型（裸写会被读成数字）。
    """
    return bool(re.fullmatch(r"\d[\d_]*", text)
                or re.fullmatch(r"[\d_]*\.[\d_]+", text))


def _repr_quoted_number(text: str) -> bool:
    """写入端为「数字」加过引号的形态：只有这些读回来才还原成数字。

    即负数、科学计数法（``1e+30`` / ``1e-07``）与 ``inf`` / ``nan``——它们裸写
    会被校验器拒绝或读成字符串，所以数字由 ``repr`` 写出、读取端必须还原。
    ``0x1f`` / ``1.`` / ``0b101`` 只是「长得像数字的字符串」，写入端照常加引号
    当字符串，读回来也保持字符串。
    """
    return bool(re.fullmatch(r"[+-][\d_]+", text)
                or re.fullmatch(r"[+-][\d_]*\.[\d_]+", text)
                or re.fullmatch(r"[+-]?(?:\d[\d_]*\.?[\d_]*|\.\d+)[eE][-+]?\d+", text)
                or re.fullmatch(r"[+-]?(?:inf(?:inity)?|nan)", text, re.IGNORECASE))


def _numberish(text: str) -> bool:
    """任何「看起来是数字」的形态：这类字符串加引号以保住字符串类型。"""
    if _bare_number(text):
        return True
    return _repr_quoted_number(text) or bool(
        re.fullmatch(r"[+-]?0[bB][01_]+", text)
        or re.fullmatch(r"[+-]?0[xX][0-9a-fA-F_]+", text)
        or re.fullmatch(r"[+-]?0[oO][0-7_]+", text)
        or re.fullmatch(r"[+-]?[\d_]+\.", text))


_UNQUOTED_START = "-?:,[]{}#&*!|>'\"%@`"
# 包含它们就必须加引号：流式集合、注释「 #」、键值分隔符、引号。
# 模型 ID 常含 [ ]（如 anthropic/stealth/space-bunny-alpha[1m]），不加引号会让
# 安全校验读不回来；`#` 会被当成行内注释截断，也需要引号。
_NEEDS_QUOTE = re.compile(r"""[:\[\]{}&*!|>%@`,#'"]""")
# 行分隔符 + 其它控制字符：裸写会被 str.splitlines() 切断（U+2028 等 json.dumps
# 不会转义），读回来只剩前半截；TAB 则直接被安全校验拒绝。
_YAML_ESCAPE_ALWAYS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0085\u2028\u2029\t]")


def _needs_escaped_quote(s: str) -> bool:
    """必须用双引号 + 转义写出的字符：引号、控制字符、行分隔符、非 ASCII 换行。

    ``json.dumps`` 会把它们转义成 ``\\n`` / ``\\t`` / ``\\uXXXX``，两个读取端都认。
    """
    if any(c in s for c in "\"'") or any(c.isspace() and c != " " for c in s):
        return True
    return bool(_YAML_ESCAPE_ALWAYS.search(s))


# json.dumps(ensure_ascii=False) 不会转义这几个：U+0085/U+2028/U+2029 是
# Unicode 行分隔符，而 str.splitlines() 会在它们处断行——原样写出去会把一个
# 标量劈成两行，读回来只剩前半截。所以写出前显式转成 \uXXXX。
_UNESCAPED_LINE_BREAKS = re.compile(r"[\u0085\u2028\u2029]")


def _quoted(s: str) -> str:
    """安全的双引号 YAML/JSON 标量：json.dumps + 手工补上 Unicode 行分隔符。"""
    return _UNESCAPED_LINE_BREAKS.sub(
        lambda m: "\\u%04x" % ord(m.group(0)), json.dumps(s, ensure_ascii=False))


def _fmt_number(v: Any) -> str:
    """数字的 YAML 写法：裸写形态必须能被两个读取端还原成同一个值。

    整数与普通小数（含负数，如 ``-1`` / ``-3.25``）裸写即可，校验器接受。
    科学计数法（``1e+30``）、进制字面量、``inf`` / ``nan`` 裸写会被校验器拒绝或
    读成字符串，所以加引号写出，再由 ``_numeric`` 还原类型。万一还原不回去就退回
    字符串写法，绝不写出会静默变类型的文本。
    """
    plain = str(v) if isinstance(v, int) and not isinstance(v, bool) else repr(v)
    if _bare_number(plain) and _numeric(plain) == v:
        return plain
    quoted = _quoted(plain)
    return quoted if _numeric(plain) == v else _quoted(str(v))


def _fmt_str(v: Any) -> str:
    s = str(v)
    if (not s or s.strip() != s or _needs_escaped_quote(s)
            or s.lower() in _YAML_WORDS or _numberish(s)
            or s[0] in _UNQUOTED_START or _NEEDS_QUOTE.search(s)):
        return _quoted(s)
    return s


def _fmt_val_inline(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _fmt_number(v)
    if isinstance(v, dict):
        if not v:
            return "{}"
        inner = ", ".join(f"{_fmt_key(k)}: {_fmt_val_inline(val)}" for k, val in v.items())
        return "{" + inner + "}"
    if isinstance(v, list):
        if not v:
            return "[]"
        return "[" + ", ".join(_fmt_val_inline(i) for i in v) + "]"
    return _fmt_str(v)


# ---------------------------------------------------------------------------
# Hub 核心
# ---------------------------------------------------------------------------


class DshModelPlan:
    """dsh 的模型映射：provider 路由名、API Key 环境变量名，以及主模型与子代理档位。

    dsh 的 llm-pi-ai 插件只接受 `apiKeyEnv`（凭据引用，不读明文 key），所以每个
    provider 都需要一个环境变量名：历史上的 4 个供应商沿用自己的名字，新增供应
    商按 `<PID>_API_KEY` 推导，密钥同时写进 ~/.dsh/.credentials.yaml。
    """

    LEGACY_ENV = {
        "firework": "FIREWORK_API_KEY",
        "deepseek": "MCODE_DEEPSEEK_API_KEY",
        "stepfun": "STEPFUN_API_KEY",
        "openrouter": "MYOPENROUTER_API_KEY",
    }

    def __init__(self, hub: "Hub") -> None:
        self.providers = hub.providers_for("dsh")
        self.config = hub.cfg
        # Dsh treats models: [] as the entire installed provider catalog.
        # Remove empty Hub routes from every persisted layer instead of sending
        # an empty list or merely omitting it from the newly generated blocks.
        self.empty_routes = {
            pid for pid, p in hub.providers.items()
            if provider_targets(p, "dsh") and agent_connection_ready(p, "dsh")
            and not agent_connection(p, "dsh")["models"]
        }
        # 已删除供应商的残留路由同样要清理：否则删除 provider 后，旧路由
        # （含 baseUrl 与 apiKeyEnv 引用）会一直留在 dsh 三层配置里。
        self.removed_routes = {
            rid for rid in (hub.cfg.get("removedProviders") or {})
            if isinstance(rid, str)
        }
        self.routes: dict[str, str] = {}
        for pid, p in self.providers.items():
            if not p.get("enabled", True) or pid in self.empty_routes:
                continue
            conn = agent_connection(p, "dsh")
            if not conn["baseUrl"]:
                raise ValueError(f"dsh 需要 {pid} 的 baseUrl（OpenAI 兼容端点）")
            self.routes[pid] = pid

    def env_name(self, pid: str) -> str:
        if pid in self.LEGACY_ENV:
            return self.LEGACY_ENV[pid]
        return re.sub(r"[^A-Za-z0-9_]", "_", pid).upper() + "_API_KEY"

    def block(self, pid: str) -> dict:
        p = self.providers[pid]
        conn = agent_connection(p, "dsh")
        models = [
            {
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "contextWindow": m.get("contextWindow", 128000),
                "maxTokens": m.get("maxTokens", 32768),
                "input": m.get("input", ["text"]),
                "reasoningEfforts": {"off": None, "low": "low", "high": "high", "max": "max"},
            }
            for m in conn["models"]
        ]
        return {
            "displayName": p.get("displayName") or p.get("name"),
            "apiKeyEnv": self.env_name(pid),
            "api": conn["api"],
            "baseURL": conn["baseUrl"],
            **({"compat": conn["compat"]} if conn.get("compat") else {}),
            "models": models,
        }

    def blocks(self) -> dict[str, dict]:
        return {pid: self.block(pid) for pid in self.routes}

    def credentials(self) -> dict[str, str]:
        out = {}
        for pid in self.routes:
            key = agent_connection(self.providers[pid], "dsh")["apiKey"]
            if isinstance(key, str) and key.strip():
                out[self.env_name(pid)] = key
        return out

    def selection(self) -> dict[str, str] | None:
        """modelSync 指向的 (provider, model)；未配置或模型不在库里时返回 None。"""
        tier = agent_model_tiers(self.config, "dsh").get("agent")
        if not tier:
            return None
        pid, mid = tier["provider"], tier["model"]
        if pid not in self.routes:
            raise ValueError(f"modelSync 指向的供应商 {pid} 未启用或未同步到 dsh")
        if not any(m.get("id") == mid for m in agent_connection(self.providers[pid], "dsh")["models"]):
            raise ValueError(f"modelSync 指向的模型 {mid} 不在 {pid} 的模型库里，未写入 dsh")
        return {"provider": self.routes[pid], "model": mid,
                "reasoningEffort": tier.get("reasoningEffort")}


def _dsh_find_block(items: list, block_id: str) -> dict | None:
    for item in items:
        if isinstance(item, dict) and item.get("id") == block_id:
            return item
    return None


def _dsh_ensure_block(items: list, block_id: str, name: str, config: dict) -> dict:
    """取到指定 id 的 patch 块，没有就插到 llm-pi-ai 后面（保持文件结构稳定）。"""
    block = _dsh_find_block(items, block_id)
    if isinstance(block, dict):
        block.setdefault("config", config)
        return block
    block = {"id": block_id, "name": name, "config": config}
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("id") == "llm-pi-ai":
            items.insert(index + 1, block)
            return block
    items.append(block)
    return block


def _pi_model_reference(provider: str, model: str) -> str:
    """Canonical Pi scope reference, preserving literal bracket model IDs.

    Pi's current resolver tries a canonical exact match before minimatch, even
    for '[' IDs. Escaping brackets bypasses that exact match and can select a
    different provider whose bare model ID equals this qualified reference.
    The glob branch strips a valid thinking suffix *before* exact matching;
    bracket IDs ending in that suffix cannot be represented safely here.
    """
    reference = f"{provider}/{model}"
    if any(char in reference for char in "*?\\"):
        raise ValueError("pi 模型 ID 含通配符或反斜杠，无法安全生成 enabledModels 精确范围")
    if "[" in reference and reference.rsplit(":", 1)[-1] in {
        "off", "minimal", "low", "medium", "high", "xhigh", "max"
    }:
        raise ValueError("pi 带方括号的模型 ID 以推理档位后缀结尾，原生范围解析有歧义；请先调整该模型配置")
    return reference


class Hub:
    def __init__(self) -> None:
        self.cfg = load_json(HUB_CONFIG)
        if not self.cfg:
            raise SystemExit(f"无法读取统一配置: {HUB_CONFIG}")
        self.state = load_json(HUB_STATE, default={"hashes": {}, "lastSync": None})
        self.providers: dict[str, dict] = self.cfg.get("providers", {})

    # -- state --
    def save_state(self) -> None:
        self.state["lastSync"] = now_iso()
        save_json(HUB_STATE, self.state)

    def record_hashes(self) -> None:
        hashes = {}
        targets = self.cfg.get("sync", {}).get("targets", {})
        for tname, tcfg in targets.items():
            if tcfg.get("enabled") is not True:
                continue
            for f in target_files(tname, tcfg):
                p = expand(f)
                hashes[str(p)] = file_hash(p)
        self.state["hashes"] = hashes

    def changed_files(self) -> list[str]:
        out = []
        hashes = self.state.get("hashes", {})
        targets = self.cfg.get("sync", {}).get("targets", {})
        # SQLite 等二进制库打开即变，不参与外部变更检测 (它们是 hub 的写入目标)
        skip_names = {"cc-switch.db"}
        for tname, tcfg in targets.items():
            if tcfg.get("enabled") is not True:
                continue
            for f in target_files(tname, tcfg):
                p = expand(f)
                if p.name in skip_names:
                    continue
                sp = str(p)
                if hashes.get(sp, "missing") != file_hash(p):
                    out.append(sp)
        return out

    # -- 自定义供应商接入 --
    def providers_for(self, target: str) -> dict:
        """按目标平台过滤 provider。

        provider 可以用 "targets": ["claude"] 把自己限制到指定平台；
        未声明时进入所有平台。这样 Anthropic 专用网关不会污染 pi/mcode/zcode
        等 OpenAI 兼容平台的配置。

        同时要求该 Agent 的连接就绪（端点 + 协议匹配）：给 Claude Code 用需要
        Anthropic Messages 端点，给 Codex 第三方用需要 Responses 端点；
        按 Agent 覆盖（agents.<agent>）可以为同一供应商补齐这些连接。
        """
        canonical = canonical_target(target)
        return {
            pid: p
            for pid, p in self.providers.items()
            if provider_targets(p, canonical)
            and p.get("enabled", True)
            and agent_connection_ready(p, canonical)
        }

    # -- push adapters --
    def preview_push(self, only: list[str] | None = None, *, reconcile=False) -> dict:
        from sync_transaction import preview
        return preview(self, only, reconcile=reconcile)

    def apply_preview(self, plan_id: str) -> dict:
        from sync_transaction import apply
        return apply(self, plan_id)

    def restore_transaction(self, transaction_id: str) -> dict:
        from sync_transaction import restore
        return restore(self, transaction_id)

    def push_all(self, only: list[str] | None = None) -> list[str]:
        """Validate every selected output before a recoverable grouped apply."""
        try:
            plan = self.preview_push(only)
            result = self.apply_preview(plan["planId"])
            self.last_transaction = result["transactionId"]
            return result["results"]
        except Exception as exc:
            return [f"  ✗ sync: {exc}"]

    # ---- pi ----
    def push_pi(self, *, warnings=None) -> None:
        models_path = HOME / ".pi" / "agent" / "models.json"
        auth_path = HOME / ".pi" / "agent" / "auth.json"
        settings_path = HOME / ".pi" / "agent" / "settings.json"

        backup_file(models_path)
        backup_file(auth_path)

        models = load_json(models_path, default={}) or {}
        auth = load_json(auth_path, default={}) or {}
        settings = load_json(settings_path, default={}) or {}

        models.setdefault("providers", {})
        providers = models["providers"]
        # 移除已删除 provider 与历史别名 (firework 取代 volcano, stepfun 取代 cc-switch-step-fun)
        removed = self.cfg.get("removedProviders", {})
        legacy = list(removed.keys()) + [
            "qwen-token-plan-cn",
            "volcano",
            "cc-switch-step-fun",
            "provider-0b8770",
        ]
        for rid in legacy:
            providers.pop(rid, None)
            auth.pop(rid, None)

        pi_providers = self.providers_for("pi")
        scope = []
        for pid, p in pi_providers.items():
            if not p.get("enabled", True):
                continue
            conn = agent_connection(p, "pi")
            if not isinstance(conn["apiKey"], str) or not conn["apiKey"].strip():
                raise ValueError(f"pi 供应商 {pid} 缺少 apiKey；此版本的 pi 适配器不解析 Codex 环境变量引用")
            # 以 pi 现有条目为底稿：hub 只覆盖自己管理的键，
            # pi 侧独有的 cost / headers / oauth 等字段与用户手工参数不会被抹掉。
            previous = providers.get(pid)
            entry = dict(previous) if isinstance(previous, dict) else {}
            entry.update(
                {
                    "name": p.get("displayName") or p.get("name"),
                    "baseUrl": conn["baseUrl"],
                    "api": conn["api"],
                    "apiKey": conn["apiKey"],
                }
            )
            # 供应商级 compat 必须下发：maxTokensField / supportsDeveloperRole /
            # thinkingFormat 等缺失会让 pi 发出的请求被网关 400 拒绝。
            if conn.get("compat"):
                entry["compat"] = copy.deepcopy(conn["compat"])
            pi_models_by_id = {
                m.get("id"): m
                for m in (previous.get("models") if isinstance(previous, dict) else None) or []
                if isinstance(m, dict)
            }
            entry["models"] = []
            for m in conn["models"]:
                reference = _pi_model_reference(pid, m["id"])
                scope.append(reference)
                old = pi_models_by_id.get(m["id"]) or {}
                item = dict(old)
                item.update(
                    {
                        "id": m["id"],
                        "name": m.get("name", m["id"]),
                        "reasoning": m.get("reasoning", True),
                        # pi 的 models.json 用数组表达输入模态，不能写成 "text image"
                        "input": list(m.get("input") or ["text"]),
                        "contextWindow": m.get("contextWindow", 128000),
                        "maxTokens": m.get("maxTokens", 32768),
                    }
                )
                # hub 里显式配置的高级字段优先于 pi 侧旧值
                for key in ("cost", "compat"):
                    if m.get(key) is not None:
                        item[key] = copy.deepcopy(m[key])
                entry["models"].append(item)
            providers[pid] = entry
            auth[pid] = {"type": "api_key", "key": conn["apiKey"]}

        # 多 agent 模型同步：只有显式配置了 modelSync 才动 settings.json 的默认模型，
        # 否则保留用户自己挑的 defaultProvider / defaultModel。
        tier = agent_model_tiers(self.cfg, "pi").get("agent")
        if tier and tier["provider"] in {pid for pid, p in self.providers_for("pi").items()
                                         if p.get("enabled", True)}:
            settings["defaultProvider"] = tier["provider"]
            settings["defaultModel"] = tier["model"]
            if tier.get("reasoningEffort"):
                settings["defaultThinkingLevel"] = tier["reasoningEffort"]

        # models.json augments Pi's built-in catalog; it does not whitelist it.
        # Scope the picker/cycling to the Hub library without deleting catalogs,
        # credentials, or user scopes for providers outside Hub's Pi ownership.
        managed = {pid.casefold() for pid, p in self.providers.items()
                   if provider_targets(p, "pi") and agent_connection_ready(p, "pi")}
        managed.update(rid.casefold() for rid in legacy)
        old_scope = settings.get("enabledModels", [])
        if not isinstance(old_scope, list) or not all(isinstance(item, str) for item in old_scope):
            raise ValueError("pi enabledModels 必须是字符串数组；请先修复原配置")
        retained = [item for item in old_scope
                    if item.strip().split("/", 1)[0].casefold() not in managed]
        had_managed_scope = len(retained) != len(old_scope)
        # A saved default outside Hub remains usable at startup, even when there
        # was previously no explicit scope. Never add unselected managed models.
        default_provider = settings.get("defaultProvider")
        default_model = settings.get("defaultModel")
        default_ref = None
        if (pi_providers or had_managed_scope) and isinstance(default_provider, str) and isinstance(default_model, str):
            default_ref = _pi_model_reference(default_provider, default_model)
            if default_provider.casefold() not in managed:
                retained.append(default_ref)
        if pi_providers or had_managed_scope:
            scope = list(dict.fromkeys(scope + retained))
            if not scope:
                # [] means ALL in Pi, not NONE. Do not silently widen access.
                raise ValueError("pi 没有可同步的模型范围；enabledModels 空数组会启用全部模型，请先选择模型")
            if default_ref in scope:
                scope.remove(default_ref)
                scope.insert(0, default_ref)
            settings["enabledModels"] = scope
            if any("/" not in item or any(char in item for char in "*?[") for item in retained):
                message = ("pi 保留了用户原有的简写或通配模型范围；这些规则可能让 scoped 列表"
                           "包含 Hub 模型库之外的模型。可在 /scoped-models 检查。")
                if warnings is not None:
                    warnings.append(message)

        save_json(models_path, models)
        save_json(auth_path, auth)
        save_json(settings_path, settings)

    # ---- mcode (MiniMax) ----
    def push_mcode(self) -> None:
        path = HOME / ".minimax" / "config.yaml"
        if not path.exists():
            raise FileNotFoundError(path)
        backup_file(path)
        text = path.read_text(encoding="utf-8")
        data = parse_yaml(text) or {}

        # 清掉 qwen-token-plan 与历史别名
        custom = data.get("custom_provider") or {}
        legacy_mcode = {"provider-0b8770", "cc-switch-step-fun"}
        for rid in list(custom.keys()):
            name_l = (custom[rid].get("name") or rid).lower()
            base = (custom[rid].get("options") or {}).get("baseURL") or ""
            if (
                "token-plan" in base
                or "token-plan" in name_l
                or rid.startswith("qwen-token")
                or rid in legacy_mcode
            ):
                custom.pop(rid, None)
        data["custom_provider"] = custom

        # 写入/更新 4 个 provider
        for pid, p in self.providers_for("mcode").items():
            if not p.get("enabled", True):
                continue
            conn = agent_connection(p, "mcode")
            key = pid if pid in ("deepseek", "stepfun") else (
                "volcano" if pid == "firework" else pid
            )
            models_entry = {}
            for m in conn["models"]:
                mod: dict[str, Any] = {
                    "name": m.get("name", m["id"]),
                    "attachment": "image" in m.get("input", []),
                    "reasoning": m.get("reasoning", True),
                    "temperature": True,
                    "tool_call": True,
                    "limit": {
                        "context": m.get("contextWindow", 128000),
                        "output": m.get("maxTokens", 32768),
                    },
                    "modalities": {
                        "input": m.get("input", ["text"]),
                        "output": ["text"],
                    },
                }
                models_entry[m["id"]] = mod
            custom[key] = {
                "name": p.get("displayName") or p.get("name"),
                "kind": "custom",
                "enabled": True,
                "api": conn["api"],
                "options": {
                    "baseURL": conn["baseUrl"],
                    "authMode": "api-key",
                    "apiKey": conn["apiKey"],
                },
                "models": models_entry,
            }
        data["custom_provider"] = custom
        path.write_text(dump_yaml(data) + "\n", encoding="utf-8")

    # ---- zcode ----
    def push_zcode(self, *, reconcile: bool = False, warnings: list | None = None) -> None:
        path = HOME / ".zcode" / "v2" / "provider_config.json"
        if not path.exists():
            raise FileNotFoundError(path)
        # 与 Codex 相同的双模式门控：official 恢复 Hub 接管前的默认选择，
        # third-party 才写 defaultModelSelection。缺省按 official 处理，
        # 旧配置不会在升级后被隐式接管。
        target_cfg = self.cfg.get("sync", {}).get("targets", {}).get("zcode") or {}
        mode = target_cfg.get("mode", "official")
        if mode not in ("official", "third-party"):
            raise ValueError("ZCode mode 必须是 official 或 third-party")
        state_path = HOME / ".zcode" / "v2" / ".agent-hub-state.json"
        saved = load_json(state_path)
        if saved is not None and (not isinstance(saved, dict) or saved.get("version") != 1):
            raise ValueError("ZCode 恢复记录无效，请先检查备份")
        active = bool(saved and saved.get("active"))
        if active:
            official_saved = saved.get("official")
            if (not isinstance(official_saved, dict) or set(official_saved) != {"present", "value"}
                    or not isinstance(official_saved.get("present"), bool)
                    or (official_saved["present"] and not isinstance(official_saved.get("value"), dict))
                    or not isinstance(saved.get("selectionHash"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", saved["selectionHash"])):
                raise ValueError("ZCode 托管记录无效，请先检查备份")
        backup_file(path)
        data = load_json(path) or {}
        cfg = data.setdefault("config", {})
        rules = cfg.setdefault("providerConfigRules", {}).setdefault("providerRules", [])
        model_rules = cfg.setdefault("modelConfigRules", {}).setdefault("providerModelRules", [])
        order = cfg.setdefault("providerOrder", [])

        # 索引现有
        by_id = {r.get("providerId"): r for r in rules if isinstance(r, dict)}
        by_name = {r.get("providerName"): r for r in rules if isinstance(r, dict)}

        # 删除 qwen-token-plan
        for r in list(rules):
            if "qwen-token" in (r.get("providerId") or "") or "qwen-token" in (r.get("providerName") or ""):
                rules.remove(r)
                pid = r.get("providerId")
                if pid in order:
                    order.remove(pid)
        model_rules[:] = [
            r for r in model_rules if "qwen-token" not in (r.get("providerId") or "")
        ]

        # zcode 映射: firework→火山, 其余同名。ZCode 自带的 provider ID
        # 可能与 Hub ID 不同（例如火山的固定 UUID），所以默认模型选择
        # 也必须通过同一张映射表落盘。
        name_map = {"firework": "火山", "openrouter": "OpenRouter", "deepseek": "DeepSeek", "stepfun": "StepFun"}
        id_map = {"firework": "a4d4d39c-5404-48db-b49a-e46d34df4525"}
        zcode_ids: dict[str, str] = {}

        for pid, p in self.providers_for("zcode").items():
            if not p.get("enabled", True):
                continue
            conn = agent_connection(p, "zcode")
            requested_zid = id_map.get(pid, pid)
            zname = name_map.get(pid, p.get("displayName") or p.get("name"))
            model_entries = [m for m in conn["models"]
                             if isinstance(m, dict) and isinstance(m.get("id"), str)
                             and m["id"].strip()]
            model_ids = [m["id"] for m in model_entries]

            existing = by_id.get(requested_zid) or by_name.get(zname)
            # If a user already renamed/re-keyed a ZCode provider, retain its
            # native ID so a later sync does not create a duplicate provider.
            zid = ((existing.get("providerId") or requested_zid)
                   if isinstance(existing, dict) else requested_zid)
            zcode_ids[pid] = zid
            api_type = ZCODE_API_TYPES[conn["api"]]
            override = (p.get("agents") or {}).get("zcode") or {}
            explicit_api = bool(override.get("api"))
            if pid == "firework" and not explicit_api and conn["api"] == "openai-completions":
                api_type = "openai-responses"
            # 保留原有 api type 偏好
            previous_model_ids: set[str] = set()
            if existing:
                old_api = ((existing.get("config") or {}).get("api") or {}).get("type")
                if old_api and not explicit_api:
                    api_type = old_api
                cfg_inner = existing.setdefault("config", {})
                previous_model_ids = {
                    model_id for model_id in (cfg_inner.get("personalModelIds") or [])
                    if isinstance(model_id, str)
                }
            else:
                cfg_inner = {}
                existing = {
                    "providerId": zid,
                    "providerName": zname,
                    "config": cfg_inner,
                }
                if pid in ("deepseek", "openrouter"):
                    existing["templateId"] = pid
                rules.append(existing)
                by_id[zid] = existing
                by_name[zname] = existing

            existing["providerId"] = zid
            existing["providerName"] = zname
            cfg_inner.setdefault("group", "standard-personal")
            cfg_inner["access"] = {"type": "api-key", "apiKey": conn["apiKey"]}
            cfg_inner["api"] = {"type": api_type, "baseUrl": conn["baseUrl"]}
            cfg_inner["personalModelIds"] = model_ids
            cfg_inner["modelOrder"] = model_ids
            if zid not in order:
                order.append(zid)

            # Hub 管理的旧个人模型规则如果已经从该连接移除，也一并收回；
            # 其它 ZCode 内置/手工规则保留，避免刷新供应商时误删原生目录。
            if previous_model_ids:
                model_rules[:] = [
                    r for r in model_rules
                    if not (isinstance(r, dict) and r.get("providerId") == zid
                            and r.get("modelId") in previous_model_ids
                            and r.get("modelId") not in model_ids)
                ]

            # 模型规则按 zcode 连接的模型库同步，而不是顶层默认连接的库。
            for m in model_entries:
                mr = next(
                    (x for x in model_rules if isinstance(x, dict)
                     and x.get("modelId") == m["id"] and x.get("providerId") == zid),
                    None,
                )
                if not mr:
                    mr = {"modelId": m["id"], "providerId": zid, "config": {}}
                    model_rules.append(mr)
                properties = mr.setdefault("config", {}).setdefault("properties", {})
                properties["contextWindow"] = m.get("contextWindow", 128000)
                if "input" in m:
                    properties.setdefault("inputFormat", {})["supportsImage"] = "image" in m["input"]

        # 默认模型选择：official/third-party 双模式。third-party 下 modelSync
        # 显式解析出主模型才写 config.defaultModelSelection（没有 modelSync 时
        # 保留用户现有选择，仅同步供应商目录不改动 ZCode 的当前会话入口）。
        # 首次写入前把当前选择（含「没有选择」）快照进 state 文件；official
        # 恢复快照。外部改过选择时 fail-closed，只有显式预览 reconcile。
        live_selection = cfg.get("defaultModelSelection")
        next_state = saved
        selection = resolve_agent_model(self.cfg, "zcode") if mode == "third-party" else None
        if mode == "official":
            if active:
                if fingerprint(live_selection) == saved["selectionHash"]:
                    # Hub 写入的选择还在：恢复接管前的快照，缺省恢复成缺省
                    if saved["official"]["present"]:
                        cfg["defaultModelSelection"] = copy.deepcopy(saved["official"]["value"])
                    else:
                        cfg.pop("defaultModelSelection", None)
                elif warnings is not None:
                    # 用户已在 ZCode 内自行改选：保留当前选择作为新基线，不复活旧快照
                    warnings.append("ZCode: 默认模型选择已在外部更改；保留当前选择，不再恢复接管前的快照。")
                next_state = {"version": 1, "active": False}
        elif selection:
            pid = selection["provider"]
            zid = zcode_ids.get(pid)
            if not zid:
                raise ValueError(f"modelSync 指向的供应商 {pid} 未启用或未同步到 zcode")
            conn = agent_connection(self.providers[pid], "zcode")
            if not any(isinstance(m, dict) and m.get("id") == selection["model"]
                       for m in conn["models"]):
                raise ValueError(f"modelSync 指向的模型 {selection['model']} 不在 {pid} 的 zcode 模型库里，未写入 zcode")
            chosen = {"providerId": zid, "modelId": selection["model"]}
            effort = selection.get("reasoningEffort")
            if effort:
                chosen["options"] = {"reasoningLevel": effort}
            if active:
                official_next = saved["official"]
                if fingerprint(live_selection) != saved["selectionHash"]:
                    if not reconcile:
                        raise ValueError("ZCode 默认模型选择已在外部更改；请在同步页生成新预览并确认应用，自动同步不会覆盖外部编辑")
                    if warnings is not None:
                        warnings.append("ZCode: 检测到默认模型选择被外部更改。本次预览将按 Hub 选择重新设置；确认应用前不会写入。")
            else:
                official_next = {"present": isinstance(live_selection, dict),
                                 "value": copy.deepcopy(live_selection) if isinstance(live_selection, dict) else None}
            if cfg.get("defaultModelSelection") != chosen:
                cfg["defaultModelSelection"] = chosen
            next_state = {"version": 1, "active": True, "official": official_next,
                          "selectionHash": fingerprint(chosen)}

        save_json(path, data)
        if next_state != saved:
            save_json(state_path, next_state)

    def zcode_import_candidates(self) -> list[dict]:
        """读取 ~/.zcode/v2/provider_config.json 里 Hub 尚未管理的个人供应商。

        反向同步的读取侧：ZCode 的 provider 目录往往是用户最先配好的地方，
        这里把它还原成 Hub 供应商形状（端点 / 密钥 / 协议 / 模型库），导入后
        即可同步给 pi / dsh / Claude Code 等其它平台。只认个人目录里的
        api-key 条目；OAuth/订阅登录与内置条目不属于 Hub 的密钥模型，跳过。
        """
        path = HOME / ".zcode" / "v2" / "provider_config.json"
        data = load_json(path) or {}
        config = data.get("config") or {}
        rules = (config.get("providerConfigRules") or {}).get("providerRules") or []
        model_rules = (config.get("modelConfigRules") or {}).get("providerModelRules") or []
        name_map = {"firework": "火山", "openrouter": "OpenRouter", "deepseek": "DeepSeek", "stepfun": "StepFun"}
        known_ids: set[str] = set()
        known_names: set[str] = set()
        for pid, p in self.providers.items():
            known_ids.add(pid)
            known_names.add(pid.casefold())
            for label in (p.get("displayName"), p.get("name")):
                if isinstance(label, str) and label.strip():
                    known_names.add(label.casefold())
            legacy = name_map.get(pid)
            if legacy:
                known_names.add(legacy.casefold())
        candidates: list[dict] = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            zid = rule.get("providerId")
            name = rule.get("providerName") or zid
            cfg = rule.get("config") or {}
            group = cfg.get("group") or "standard-personal"
            if group != "standard-personal":
                continue
            access = cfg.get("access") or {}
            if access.get("type") != "api-key":
                continue
            api = cfg.get("api") or {}
            wire = ZCODE_API_REVERSE.get(api.get("type"))
            base_url = api.get("baseUrl") if isinstance(api.get("baseUrl"), str) else ""
            if not wire or not base_url.strip():
                continue
            if zid in known_ids or (isinstance(name, str) and name.casefold() in known_names):
                continue
            rule_models = [r for r in model_rules
                           if isinstance(r, dict) and r.get("providerId") == zid]
            disabled = {r.get("modelId") for r in rule_models
                        if (r.get("config") or {}).get("enabled") is False}
            properties = {r.get("modelId"): (r.get("config") or {}).get("properties") or {}
                          for r in rule_models}
            models = []
            for mid in (cfg.get("modelOrder") or cfg.get("personalModelIds") or []):
                if not isinstance(mid, str) or not mid.strip() or mid in disabled:
                    continue
                props = properties.get(mid) or {}
                models.append({
                    "id": mid, "name": mid,
                    "contextWindow": props.get("contextWindow", 128000),
                    "maxTokens": 32768,
                    "input": ["text", "image"] if (props.get("inputFormat") or {}).get("supportsImage") else ["text"],
                    "reasoning": True,
                })
            slug = re.sub(r"[^a-z0-9]+", "-", str(name or zid or "zcode").lower()).strip("-") or "zcode-provider"
            pid, n = slug, 2
            while pid in known_ids:
                pid, n = f"{slug}-{n}", n + 1
            known_ids.add(pid)
            candidates.append({"id": pid, "name": str(name or zid), "baseUrl": base_url,
                               "apiKey": access.get("apiKey") if isinstance(access.get("apiKey"), str) else "",
                               "api": wire, "modelCount": len(models), "models": models})
        return candidates

    # ---- dsh ----
    def push_dsh(self) -> None:
        plan = DshModelPlan(self)
        selection = plan.selection()

        # 1) credentials.yaml — 只补/更新 apiKeyEnv 对应的 refs，其它凭据原样保留
        cred_path = HOME / ".dsh" / ".credentials.yaml"
        refs = plan.credentials()
        if cred_path.exists() and refs:
            backup_file(cred_path)
            cred_text = cred_path.read_text(encoding="utf-8")
            for key, value in refs.items():
                pattern = rf"(?m)^(\s*{re.escape(key)}:\s*).*$"
                if re.search(pattern, cred_text):
                    cred_text = re.sub(pattern, rf"\g<1>{value}", cred_text)
                elif re.search(r"(?m)^refs:\s*$", cred_text):
                    cred_text = re.sub(r"(?m)^(refs:\s*)$", rf"\1\n  {key}: {value}", cred_text, count=1)
                else:
                    cred_text = cred_text.rstrip("\n") + f"\nrefs:\n  {key}: {value}\n"
            cred_path.write_text(cred_text, encoding="utf-8")

        blocks = plan.blocks()

        # 2) settings.yaml.imported — 重写 llm-pi-ai.providers
        imported = HOME / ".dsh" / "settings.yaml.imported"
        if imported.exists():
            backup_file(imported)
            data = parse_yaml(imported.read_text(encoding="utf-8")) or {}
            llm = data.setdefault("llm-pi-ai", {}).setdefault("providers", {})
            for rid in list(llm.keys()):
                if "qwen-token" in rid or rid in plan.empty_routes or rid in plan.removed_routes:
                    llm.pop(rid)
            llm.update(copy.deepcopy(blocks))
            data["llm-pi-ai"]["providers"] = llm
            imported.write_text(dump_yaml(data) + "\n", encoding="utf-8")

        # 3) cordis.patch.yml — llm-pi-ai.providers + agent-default-model + 子代理可选模型
        patch = HOME / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
        if patch.exists():
            backup_file(patch)
            try:
                items = parse_yaml(patch.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                items = None
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("id") == "llm-pi-ai":
                        cfg = item.setdefault("config", {})
                        providers = cfg.setdefault("providers", {})
                        for rid in list(providers.keys()):
                            if "qwen-token" in rid or rid in plan.empty_routes or rid in plan.removed_routes:
                                providers.pop(rid)
                        providers.update(copy.deepcopy(blocks))
                        cfg["providers"] = providers

                # 主 agent 默认模型：只有配置了 modelSync 才接管，否则保持用户当前选择
                if selection:
                    block = _dsh_ensure_block(
                        items, "agent-default-model", "@deepseek-ai/dsh-agent-default-model",
                        {"provider": selection["provider"], "model": selection["model"]},
                    )
                    config = block.setdefault("config", {})
                    config["provider"] = selection["provider"]
                    config["model"] = selection["model"]
                    if selection.get("reasoningEffort"):
                        config["reasoningEffort"] = selection["reasoningEffort"]
                    # 子代理可选模型：允许的 (provider, model) 组合
                    settings = _dsh_ensure_block(
                        items, "subagent-model-selection-settings",
                        "@deepseek-ai/dsh-tool-subagent/model-selection-settings",
                        {"enabled": True, "allowedModels": []},
                    )
                    sync_config = settings.setdefault("config", {})
                    sync_config["enabled"] = True
                    sync_config["allowedModels"] = [
                        {"provider": selection["provider"], "model": selection["model"]}
                    ]

                patch.write_text(_dump_yaml_list(items) + "\n", encoding="utf-8")

        # 4) settings.yaml — 桌面 Models 页用户层 (dsh 桌面版会合并此层)
        user_settings = HOME / ".dsh" / "settings.yaml"
        if user_settings.exists():
            backup_file(user_settings)
            existing = parse_yaml(user_settings.read_text(encoding="utf-8")) or {}
            if isinstance(existing, dict):
                merged_providers = (existing.get("llm-pi-ai") or {}).get("providers") or {}
                for rid in list(merged_providers.keys()):
                    if "qwen-token" in rid or rid in plan.empty_routes or rid in plan.removed_routes:
                        merged_providers.pop(rid)
                merged_providers.update(copy.deepcopy(blocks))
                existing.setdefault("llm-pi-ai", {})["providers"] = merged_providers
                user_settings.write_text(dump_yaml(existing) + "\n", encoding="utf-8")
            else:
                user_settings.write_text(dump_yaml({"llm-pi-ai": {"providers": blocks}}) + "\n",
                                         encoding="utf-8")
        else:
            user_settings.write_text(dump_yaml({"llm-pi-ai": {"providers": blocks}}) + "\n",
                                     encoding="utf-8")

    # ---- claude (Claude Code) ----
    def push_claude(self) -> None:
        """直接配置 Claude Code；绝不隐式写入其他工具的数据库。

        Claude Code 与其它 Agent 走同一套规则：只有 modelSync 为它解析出主模型
        时才写入 ~/.claude/settings.json 的 ANTHROPIC_*；没有配置就保持现状。
        连接走 ``agents.claude``（缺省时用 anthropicBaseUrl 兼容字段），透传字段也是
        该连接自己的 env / settings。

        平台级特性开关（featureEnv）属于 Claude Code 本身，即使没有选定模型也会
        写入，否则「切到别的供应商后特性丢失」的事故会重演。
        """
        settings_path = HOME / ".claude" / "settings.json"
        tiers = agent_model_tiers(self.cfg, "claude")
        main = tiers.get("model")
        # 平台级特性开关：不属于任何 provider，切换 provider 不收回
        feature_env = (self.cfg.get("sync", {}).get("targets", {}).get("claude") or {}).get("featureEnv") or {}
        if not main and not feature_env:
            return
        if settings_path.exists():
            backup_file(settings_path)
            data = load_json(settings_path, default={}) or {}
        else:
            data = {"$schema": "https://json.schemastore.org/claude-code-settings.json"}
        env = data.setdefault("env", {})
        if main is None:
            # 没有选定模型：只补平台特性开关，不猜供应商，也不动任何 ANTHROPIC_* 键
            for k, v in self._feature_env_values(feature_env).items():
                env[k] = v
            data["env"] = env
            save_json(settings_path, data)
            return

        active = main["provider"]
        anthropic_providers = claude_providers(self.providers)
        p = anthropic_providers.get(active)
        if p is None:
            raise ValueError(f"Claude 供应商 {active} 没有可用的 Anthropic 连接")
        conn = agent_connection(p, "claude")
        if not conn["baseUrl"].strip():
            raise ValueError(f"Claude 供应商 {active} 缺少 Anthropic 端点")
        if not conn["apiKey"].strip():
            raise ValueError(f"Claude 供应商 {active} 缺少 apiKey；Codex 环境变量引用不适用于此适配器")
        # 1) ~/.claude/settings.json — 只改 env 中的 ANTHROPIC_*，保留 hooks/permissions 等
        #    （载入与备份已在上面的 featureEnv 分支之前完成）
        def tier_model(tier: str):
            """档位模型：只有 modelSync 显式指定过的档位才写。"""
            resolved = tiers.get(tier)
            return resolved["model"] if resolved else None

        main_model = tiers.get("model", {}).get("model")
        # 模型别名
        model_map = {
            "ANTHROPIC_MODEL": main_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": tier_model("opus"),
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": tier_model("opus"),
            "ANTHROPIC_DEFAULT_SONNET_MODEL": tier_model("sonnet"),
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": tier_model("sonnet"),
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": tier_model("haiku"),
            "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": tier_model("haiku"),
            "ANTHROPIC_DEFAULT_FABLE_MODEL": tier_model("fable"),
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": tier_model("fable"),
            "CLAUDE_CODE_SUBAGENT_MODEL": tier_model("subagent"),
        }
        env = data.setdefault("env", {})
        # 自定义供应商：连接上声明式透传任意 env 变量 (CLAUDE_CODE_*、ENABLE_TOOL_SEARCH 等)
        extra_env = {
            k: v for k, v in conn["env"].items()
            if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
        }
        written_env = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}
        env["ANTHROPIC_AUTH_TOKEN"] = conn["apiKey"]
        env["ANTHROPIC_BASE_URL"] = conn["baseUrl"]
        for k, v in model_map.items():
            if v:
                env[k] = v
                written_env.add(k)
        for k, v in extra_env.items():
            env[k] = v
            written_env.add(k)
        # hub 自己管理的 env 键命名空间（闭集）：固定别名 + 各 provider 的 Claude 连接
        # 声明过的 env。用它来收回残留：hub 声明过的键就归 hub 管，用户手工加的键
        # 不在其中，永不受影响。
        owned_env = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", *model_map}
        for q in anthropic_providers.values():
            owned_env.update(k for k in agent_connection(q, "claude")["env"]
                             if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"))
        # 切换 provider 时收回上一个 provider 写入、当前 provider 未声明的 env 键：
        # 否则切到不声明 fable 的供应商后，ANTHROPIC_DEFAULT_FABLE_MODEL 还留着上一家的模型名。
        for k in owned_env:
            if k not in written_env:
                env.pop(k, None)
        data["env"] = env
        # 平台级特性开关最后写入：即使某个连接的 env 声明过同名键、刚才被 owned_env
        # 收回，这里也按平台配置补齐，切换 provider 不丢特性。
        for k, v in self._feature_env_values(feature_env).items():
            env[k] = v

        # 自定义供应商：连接上声明式透传任意顶层 settings 字段
        # (model / effortLevel / permissions / includeCoAuthoredBy / skipDangerousModePermissionPrompt ...)
        settings_map = {
            k: v for k, v in conn["settings"].items()
            if k not in ("env", "$schema")
        }
        # model 有两个来源：modelSync 主模型（→ env.ANTHROPIC_MODEL）和
        # 连接 settings 里的 model（→ 顶层 model）。同时声明时以 modelSync 为准，
        # 否则出现「切换了模型但顶层 model 还指着旧型号」的漂移。
        if main_model and "model" in settings_map:
            settings_map["model"] = main_model
        # 顶层键同理：任一 provider 的 Claude 连接声明过的键都归 hub 管，切换时收回
        # 未声明的，避免残留上一家的 model / effortLevel。用户从未被任何 provider
        # 声明过的键（hooks / statusLine / modelSettings …）不在命名空间里，永不受影响。
        # 注意 env / $schema 不能进这个命名空间——它们由 settings_map 的过滤条件始终排除，
        # 一旦进来就会被下面这段循环误删。
        owned_keys = set()
        for q in anthropic_providers.values():
            owned_keys.update(agent_connection(q, "claude")["settings"].keys())
        for k in owned_keys:
            if k not in settings_map:
                data.pop(k, None)
        for k, v in settings_map.items():
            data[k] = v
        save_json(settings_path, data)

    # ---- codex (Codex 桌面版 / CLI) ----
    def push_codex(self, *, reconcile=False, warnings=None) -> None:
        """Compile base config only; native auth and named profiles are never opened."""
        from codex_config import compile_config
        config_path = HOME / ".codex" / "config.toml"
        state_path = HOME / ".codex" / ".agent-hub-state.json"
        target = self.cfg.get("sync", {}).get("targets", {}).get("codex") or {}
        mode = target.get("mode", "official")
        if not config_path.exists():
            raise FileNotFoundError(f"没有找到 Codex 配置: {config_path}（先在 Codex 中设置官方配置）")
        tier = agent_model_tiers(self.cfg, "codex").get("agent") if mode == "third-party" else None
        provider = None
        if mode == "third-party":
            if tier is None:
                raise ValueError("Codex 第三方模式需要选择模型")
            entry = self.providers_for("codex").get(tier["provider"])
            if not isinstance(entry, dict) or entry.get("enabled", True) is False:
                raise ValueError("Codex 选定的供应商未启用或不适用于此平台")
            conn = agent_connection(entry, "codex")
            if not any(m.get("id") == tier["model"] for m in conn["models"]):
                raise ValueError("Codex 选定的模型不在该供应商的 Codex 连接模型列表中")
            # codex_config 只认归一化后的连接形状：端点、协议与授权三者缺一不可
            provider = dict(entry, baseUrl=conn["baseUrl"], api=conn["api"],
                            apiKeyEnv=conn["apiKeyEnv"], apiKey=conn["apiKey"], openaiApiKey=conn["apiKey"],
                            codexWireApi="responses")
        text = config_path.read_text(encoding="utf-8")
        saved = load_json(state_path)
        rendered, next_state = compile_config(text, saved, mode, provider, tier, reconcile=reconcile, warnings=warnings)
        # All compilation and validation above is write-free. The transaction
        # runner calls this only inside its isolated staging directory.
        if rendered != text:
            backup_file(config_path)
            config_path.write_text(rendered, encoding="utf-8")
        if next_state != saved:
            save_json(state_path, next_state)

    # ---- cc-switch (SQLite) ----
    # created_at 是 INTEGER（epoch 毫秒）列。cc-switch 启动时按整数读，遇到
    # TEXT 值会直接崩溃（"Invalid column type Text ... name: created_at"）。
    # hub 只用 now_epoch_ms() 写入整数；若因历史数据/其它工具留下 TEXT，
    # 立刻报错并指向修复脚本，而不是留给用户一个打不开的 cc-switch。
    @staticmethod
    def _verify_cc_switch_created_at(con: sqlite3.Connection) -> None:
        cur = con.cursor()
        bad = cur.execute(
            "SELECT count(*) FROM providers WHERE typeof(created_at) = 'text'"
        ).fetchone()[0]
        if bad:
            raise RuntimeError(
                f"cc-switch 数据库 providers.created_at 仍有 {bad} 行 TEXT 值，"
                "cc-switch 将无法启动。请运行: "
                "python scripts/fix_cc_switch_created_at.py"
            )

    # ---- 平台级特性开关 (featureEnv) ----
    # Agent Teams (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS) 这类开关属于 Claude Code
    # 的平台能力，不属于任何单个 provider。只写在某个 provider 的 claudeEnv 里时，
    # 用户在 cc-switch 切到别的预设后 settings.json 被预设的 settings_config 整份
    # 覆盖，特性就丢了。featureEnv 让这类开关落在 sync.targets.claude 上：
    # 每次 push 都写进 ~/.claude/settings.json，并注入 cc-switch 的每一个
    # claude 预设（含 app 自建与 GUI 手建条目）。
    @staticmethod
    def _feature_env_values(feature_env: dict) -> dict:
        """env 值统一成字符串（Claude Code 按字符串解析环境变量）。"""
        out = {}
        for k, v in feature_env.items():
            if isinstance(v, bool):
                out[k] = "true" if v else "false"
            else:
                out[k] = v if isinstance(v, str) else str(v)
        return out

    @classmethod
    def _apply_cc_switch_feature_env(cls, cur: sqlite3.Cursor, feature_env: dict) -> None:
        if not feature_env:
            return
        flags = cls._feature_env_values(feature_env)
        rows = cur.execute(
            "SELECT id, settings_config FROM providers WHERE app_type='claude'"
        ).fetchall()
        for row_id, payload in rows:
            try:
                preset = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(preset, dict):
                continue
            env_block = preset.get("env")
            if not isinstance(env_block, dict):
                if env_block is not None:
                    continue  # 历史怪数据，不猜语义，留给校验报错
                env_block = {}
                preset["env"] = env_block
            if all(env_block.get(k) == v for k, v in flags.items()):
                continue  # 已就位，避免无谓写入
            for k, v in flags.items():
                env_block[k] = v
            cur.execute(
                "UPDATE providers SET settings_config=? WHERE id=?",
                (json.dumps(preset, ensure_ascii=False), row_id),
            )

    @classmethod
    def _verify_cc_switch_feature_env(cls, cur: sqlite3.Cursor, feature_env: dict) -> None:
        """任何一个 claude 预设缺开关都必须报错——漏一个，切过去特性就丢一次。"""
        if not feature_env:
            return
        flags = cls._feature_env_values(feature_env)
        missing = []
        rows = cur.execute(
            "SELECT id, settings_config FROM providers WHERE app_type='claude'"
        ).fetchall()
        for row_id, payload in rows:
            try:
                preset = json.loads(payload)
            except (TypeError, ValueError):
                missing.append(f"{row_id}(settings_config 不是 JSON)")
                continue
            env_block = (preset or {}).get("env")
            if not isinstance(env_block, dict):
                missing.append(f"{row_id}(env 不是对象)")
                continue
            absent = [k for k, v in flags.items() if env_block.get(k) != v]
            if absent:
                missing.append(f"{row_id} 缺 {','.join(absent)}")
        if missing:
            raise RuntimeError(
                "cc-switch 以下 claude 预设缺少特性开关，切换过去会丢失: "
                + "; ".join(missing)
            )

    def push_cc_switch(self) -> None:
        db_path = HOME / ".cc-switch" / "cc-switch.db"
        if not db_path.exists():
            raise FileNotFoundError(db_path)
        backup_file(db_path)
        con = sqlite3.connect(str(db_path))
        try:
            cur = con.cursor()
            # 删除 qwen-token-plan
            cur.execute(
                "DELETE FROM providers WHERE lower(name) LIKE '%qwen%token%' OR lower(name) LIKE '%token%plan%'"
            )
            # 删除/更新我们管理的 provider (按 name 匹配)
            managed_names = {
                "stepfun": ["StepFun", "StepFun Step Plan", "StepFun Step Plan (China)"],
                "firework": ["Volcano Gateway", "volcano", "Firework"],
                "deepseek": ["DeepSeek"],
                "openrouter": ["OpenRouter"],
            }
            for pid, p in self.providers_for("cc-switch").items():
                if not p.get("enabled", True):
                    continue
                for app_type, name in [
                    ("pi", pid),
                    ("mcode", p.get("displayName") or p.get("name")),
                ]:
                    # mcode 条目
                    pass
            # 统一策略: 为 pi / mcode 各维护一份
            for app_type in ("pi", "mcode"):
                for pid, p in self.providers_for("cc-switch").items():
                    if not p.get("enabled", True):
                        continue
                    conn = agent_connection(p, app_type)
                    display = p.get("displayName") or p.get("name")
                    if app_type == "pi":
                        row_name = pid if pid != "firework" else "volcano"
                        settings_config = {
                            "baseUrl": conn["baseUrl"],
                            "api": conn["api"],
                            "apiKey": conn["apiKey"],
                            "models": [
                                {
                                    "id": m["id"],
                                    "name": m.get("name", m["id"]),
                                    "reasoning": m.get("reasoning", True),
                                    "input": " ".join(m.get("input", ["text"])),
                                    "contextWindow": m.get("contextWindow", 128000),
                                    "maxTokens": m.get("maxTokens", 32768),
                                }
                                for m in conn["models"]
                            ],
                        }
                    else:
                        row_name = "Volcano Gateway" if pid == "firework" else display
                        settings_config = {
                            "name": row_name,
                            "kind": "custom",
                            "enabled": True,
                            "api": conn["api"],
                            "options": {
                                "baseURL": conn["baseUrl"],
                                "authMode": "api-key",
                                "apiKey": conn["apiKey"],
                            },
                            "models": {
                                m["id"]: {
                                    "name": m.get("name", m["id"]),
                                    "attachment": "image" in m.get("input", []),
                                    "reasoning": m.get("reasoning", True),
                                    "temperature": True,
                                    "tool_call": True,
                                    "limit": {
                                        "context": m.get("contextWindow", 128000),
                                        "output": m.get("maxTokens", 32768),
                                    },
                                    "modalities": {
                                        "input": m.get("input", ["text"]),
                                        "output": ["text"],
                                    },
                                }
                                for m in conn["models"]
                            },
                        }
                    cur.execute(
                        "SELECT id FROM providers WHERE app_type=? AND name=?",
                        (app_type, row_name),
                    )
                    row = cur.fetchone()
                    payload = json.dumps(settings_config, ensure_ascii=False)
                    if row:
                        cur.execute(
                            "UPDATE providers SET settings_config=? WHERE id=?",
                            (payload, row[0]),
                        )
                    else:
                        cur.execute(
                            "INSERT INTO providers (id, app_type, name, settings_config, category, sort_index, is_current, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                            (
                                f"hub-{app_type}-{pid}",
                                app_type,
                                row_name,
                                payload,
                                "custom",
                                100,
                                now_epoch_ms(),
                            ),
                        )
            # 清理同名重复里带 qwen-token 的
            cur.execute(
                "DELETE FROM providers WHERE lower(settings_config) LIKE '%token-plan%' AND lower(settings_config) LIKE '%qwen%'"
            )
            con.commit()
            self._verify_cc_switch_created_at(con)
        finally:
            con.close()

    # ---- omp ----
    def push_omp(self) -> None:
        """omp 使用 modelRoles 引用 provider/model；仅确保配置文件可写，不强行改用户角色。"""
        path = HOME / ".omp" / "agent" / "config.yml"
        if not path.exists():
            return
        # omp 的 modelRoles 由用户/UI 管理，这里只做存在性检查
        # 真正需要时可通过 hub status 观察
        return

    # ---- pull ----
    def pull_from(self, agent: str) -> None:
        """从 agent 读取 provider 并合并回 hub (以 hub 的模型列表为准, 更新 apiKey/baseUrl)。

        必须先从磁盘重新读取配置：本方法会在常驻进程 (hub watch / hub sync / GUI) 里
        调用，直接回写进程启动时的内存快照会把其它进程刚写入的模型列表整段抹掉。
        """
        disk = load_json(HUB_CONFIG, default=None)
        if not isinstance(disk, dict):
            disk = copy.deepcopy(self.cfg)
        before = copy.deepcopy(disk)
        providers = disk.setdefault("providers", {})
        removed = disk.get("removedProviders") or {}
        if agent == "pi":
            models = load_json(HOME / ".pi" / "agent" / "models.json", default={}) or {}
            for pid, entry in (models.get("providers") or {}).items():
                if pid in removed:
                    continue
                if pid not in providers:
                    # 新发现的 provider 只记录 key/baseUrl
                    providers[pid] = {
                        "name": pid,
                        "baseUrl": entry.get("baseUrl", ""),
                        "apiKey": entry.get("apiKey", ""),
                        "api": entry.get("api", "openai-completions"),
                        "enabled": True,
                        "models": [],
                    }
                else:
                    # 写回真正持有该端点的那一层：agents.<agent> 覆盖或顶层默认连接
                    override = (providers[pid].get("agents") or {}).get("pi")
                    override = override if isinstance(override, dict) else {}
                    target = override if override.get("baseUrl") else providers[pid]
                    if entry.get("apiKey"):
                        target["apiKey"] = entry["apiKey"]
                    if entry.get("baseUrl"):
                        target["baseUrl"] = entry["baseUrl"]
        elif agent == "dsh":
            imported = HOME / ".dsh" / "settings.yaml.imported"
            if imported.exists():
                data = parse_yaml(imported.read_text(encoding="utf-8")) or {}
                for pid, entry in ((data.get("llm-pi-ai") or {}).get("providers") or {}).items():
                    if pid in providers and entry.get("baseURL"):
                        override = (providers[pid].get("agents") or {}).get("dsh")
                        override = override if isinstance(override, dict) else {}
                        target = override if override.get("baseUrl") else providers[pid]
                        target["baseUrl"] = entry["baseURL"]
        elif agent == "claude":
            settings_path = HOME / ".claude" / "settings.json"
            if settings_path.exists():
                data = load_json(settings_path, default={}) or {}
                env = data.get("env") or {}
                token = env.get("ANTHROPIC_AUTH_TOKEN")
                base = env.get("ANTHROPIC_BASE_URL")
                if token and base:
                    # 反查哪个 provider 的 Claude 连接端点匹配（连接可能来自
                    # agents.claude 覆盖，也可能是旧的 anthropicBaseUrl）
                    for pid, p in providers.items():
                        conn = agent_connection(p, "claude")
                        override = (p.get("agents") or {}).get("claude")
                        override = override if isinstance(override, dict) else {}
                        if conn["baseUrl"] != base:
                            continue
                        if conn["apiKey"] == token:
                            break  # 已匹配，无需改
                        # base 能对上但 key 变了 → 写回真正持有该端点的那一层
                        if override.get("baseUrl") == base:
                            override["apiKey"] = token
                        else:
                            p["apiKey"] = token
                        break
        disk["providers"] = providers
        disk["updatedAt"] = now_iso()
        # pull 只更新 apiKey/baseUrl，绝不允许模型列表变少
        assert_expected_model_changes(before, disk)
        self.cfg = disk
        self.providers = providers
        save_json(HUB_CONFIG, disk)

    # ---- remove provider ----
    def remove_provider(self, pid: str) -> list[str]:
        msgs = [f"删除 provider: {pid}"]
        disk = load_json(HUB_CONFIG, default=None)
        if not isinstance(disk, dict):
            disk = copy.deepcopy(self.cfg)
        before = copy.deepcopy(disk)
        disk.setdefault("removedProviders", {})[pid] = {
            "reason": "manual remove",
            "removedAt": now_iso()[:10],
        }
        disk.setdefault("providers", {}).pop(pid, None)
        disk["updatedAt"] = now_iso()
        # 只允许这一个 provider 整体消失，其它供应商的模型一个都不能少
        assert_expected_model_changes(before, disk, removed_providers={pid})
        self.cfg = disk
        self.providers = disk["providers"]
        backup_file(HUB_CONFIG)
        save_json(HUB_CONFIG, disk)

        # 从各 agent 清除
        # pi
        for fname in ("models.json", "auth.json", "settings.json"):
            p = HOME / ".pi" / "agent" / fname
            if p.exists():
                data = load_json(p, default={}) or {}
                changed = False
                if isinstance(data, dict):
                    for container in ("providers",):
                        if container in data and pid in (data.get(container) or {}):
                            data[container].pop(pid)
                            changed = True
                    if pid in data:
                        data.pop(pid)
                        changed = True
                if changed:
                    backup_file(p)
                    save_json(p, data)
                    msgs.append(f"  pi/{fname} 已清除")

        # mcode custom_provider
        mcode = HOME / ".minimax" / "config.yaml"
        if mcode.exists():
            text = mcode.read_text(encoding="utf-8")
            if pid in text or "qwen-token" in text:
                backup_file(mcode)
                data = parse_yaml(text) or {}
                custom = data.get("custom_provider") or {}
                for rid in list(custom.keys()):
                    if rid == pid or "token-plan" in (custom[rid].get("options") or {}).get("baseURL", ""):
                        custom.pop(rid)
                        msgs.append(f"  mcode custom_provider.{rid} 已清除")
                data["custom_provider"] = custom
                mcode.write_text(dump_yaml(data) + "\n", encoding="utf-8")

        # zcode
        zcode = HOME / ".zcode" / "v2" / "provider_config.json"
        if zcode.exists():
            data = load_json(zcode, default={}) or {}
            rules = data.get("config", {}).get("providerConfigRules", {}).get("providerRules", [])
            before = len(rules)
            rules[:] = [
                r
                for r in rules
                if r.get("providerId") != pid and "qwen-token" not in (r.get("providerName") or "")
            ]
            if len(rules) != before:
                backup_file(zcode)
                save_json(zcode, data)
                msgs.append("  zcode 已清除")

        # dsh
        imported = HOME / ".dsh" / "settings.yaml.imported"
        if imported.exists():
            data = parse_yaml(imported.read_text(encoding="utf-8")) or {}
            llm = (data.get("llm-pi-ai") or {}).get("providers") or {}
            if pid in llm or any("qwen-token" in k for k in llm):
                backup_file(imported)
                for rid in list(llm.keys()):
                    if rid == pid or "qwen-token" in rid:
                        llm.pop(rid)
                        msgs.append(f"  dsh providers.{rid} 已清除")
                data["llm-pi-ai"]["providers"] = llm
                imported.write_text(dump_yaml(data) + "\n", encoding="utf-8")

        # cc-switch
        db_path = HOME / ".cc-switch" / "cc-switch.db"
        if db_path.exists():
            con = sqlite3.connect(str(db_path))
            try:
                cur = con.cursor()
                cur.execute(
                    "DELETE FROM providers WHERE name LIKE ? OR lower(settings_config) LIKE ?",
                    (f"%{pid}%", f"%{pid}%"),
                )
                cur.execute(
                    "DELETE FROM providers WHERE lower(name) LIKE '%qwen%token%' OR lower(settings_config) LIKE '%qwen-token%'"
                )
                n = cur.rowcount
                con.commit()
                if n:
                    msgs.append(f"  cc-switch 删除 {n} 行")
            finally:
                con.close()

        msgs.append("完成。重启相关 agent 后生效。")
        return msgs

    # ---- status ----
    def status(self) -> str:
        lines = ["Agent Hub 状态", "=" * 60]
        lines.append(f"统一配置: {HUB_CONFIG}")
        lines.append(f"Provider 数: {len(self.providers)}")
        for pid, p in self.providers.items():
            lines.append(
                f"  [{pid}] {p.get('displayName') or p.get('name')}  "
                f"models={len(p.get('models', []))}  enabled={p.get('enabled', True)}"
            )
        removed = self.cfg.get("removedProviders", {})
        if removed:
            lines.append(f"已移除: {', '.join(removed.keys())}")
        lines.append("")
        lines.append("多 agent 模型 (modelSync):")
        for target, label in (("pi", "pi"), ("dsh", "dsh / dsh 桌面版"),
                              ("zcode", "ZCode"), ("claude", "Claude Code")):
            tiers = agent_model_tiers(self.cfg, target)
            main = tiers.get("agent") or tiers.get("model")
            if not main:
                lines.append(f"  {label:18} 未配置（保留客户端当前模型）")
                continue
            suffix = "" if main.get("known") else "  ⚠ 不在模型库中"
            lines.append(f"  {label:18} {main['provider']} / {main['model']}{suffix}")
            if tiers.get("subagent"):
                sub = tiers["subagent"]
                lines.append(f"  {'':18} └ 子代理: {sub['provider']} / {sub['model']}")
        codex_mode = self.cfg.get("sync", {}).get("targets", {}).get("codex", {}).get("mode", "official")
        codex_tier = agent_model_tiers(self.cfg, "codex").get("agent")
        if codex_mode == "official":
            lines.append("  Codex 官方订阅：恢复已保存的官方配置；登录由原生客户端管理")
        elif codex_tier:
            suffix = "" if codex_tier.get("known") else "  ⚠ 不在模型库中"
            lines.append(f"  {'Codex 第三方':18} {codex_tier['provider']} / {codex_tier['model']}{suffix}")
        else:
            lines.append(f"  {'Codex 官方':18} 未配置（保留客户端当前模型与登录态）")
        lines.append("")
        lines.append("目标 agent 落地情况:")
        targets = self.cfg.get("sync", {}).get("targets", {})
        for tname, tcfg in targets.items():
            en = tcfg.get("enabled") is True
            files = target_files(tname, tcfg)
            status_parts = []
            for f in files:
                p = expand(f)
                status_parts.append(f"{'✓' if p.exists() else '✗'} {p.name}")
            extra = ""
            if tname == "codex":
                extra = f"  mode={tcfg.get('mode', 'official')}"
            if tname == "claude":
                caps = claude_providers(self.providers)
                extra = f"  claude-ready={','.join(caps) or 'none'}"
            if tcfg.get("aliasOf"):
                extra += f"  (alias of {tcfg['aliasOf']})"
            lines.append(f"  {tname:12} enabled={en}  {', '.join(status_parts)}{extra}")
        lines.append("")
        lines.append("按 Agent 的连接 (agents.<agent> 覆盖默认连接):")
        for pid, p in self.providers.items():
            parts = []
            for name in AGENT_CONNECTION_TARGETS:
                conn = agent_connection(p, name)
                mark = "*" if name in provider_agents(p) else " "
                parts.append(f"{mark}{name}={'✓' if agent_connection_ready(p, name) else '—'}"
                             f"/{len(conn['models'])}模型")
            lines.append(f"  {pid:12} {'  '.join(parts)}")
        # 检查 qwen-token-plan 是否残留
        lines.append("")
        lines.append("qwen-token-plan 残留检查:")
        leftovers = []
        checks = [
            ("pi/auth.json", HOME / ".pi" / "agent" / "auth.json", "qwen-token"),
            ("pi/models-store.json", HOME / ".pi" / "agent" / "models-store.json", "qwen-token"),
            ("mcode/config.yaml", HOME / ".minimax" / "config.yaml", "token-plan"),
        ]
        for label, path, needle in checks:
            if path.exists() and needle in path.read_text(encoding="utf-8", errors="ignore"):
                leftovers.append(label)
        if leftovers:
            lines.append(f"  ⚠ 仍存在: {', '.join(leftovers)}  → 运行 hub remove-provider qwen-token-plan-cn")
        else:
            lines.append("  ✓ 无残留")
        return "\n".join(lines)


def _dump_yaml_list(items: list) -> str:
    """把 parse_yaml 解析出的顶层列表写回 YAML (patch 文件用)。"""
    return dump_yaml(items, indent=0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_status(hub: Hub, _args) -> int:
    print(hub.status())
    return 0


def print_lines(lines: list[str]) -> None:
    for line in lines:
        print(console_safe(line))


def cmd_push(hub: Hub, args) -> int:
    only = args.targets if getattr(args, "targets", None) else None
    print("推送 providers.json → 各 agent ...")
    results = hub.push_all(only)
    print_lines(results)
    if not results or any("✗" in line for line in results):
        return 1
    hub.record_hashes()
    hub.save_state()
    if getattr(hub, "last_transaction", None):
        print("事务:", hub.last_transaction)
    print("完成。")
    return 0


def cmd_preview(hub: Hub, args) -> int:
    try:
        plan = hub.preview_push(args.targets or None)
        # IDs are short-lived and process-local. CLI preview is informational;
        # push recompiles and revalidates before writing.
        print(json.dumps({k: v for k, v in plan.items() if k != "planId"}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(console_safe(f"预览失败，未写入客户端: {exc}"), file=sys.stderr)
        return 1


def cmd_restore_transaction(hub: Hub, args) -> int:
    try:
        result = hub.restore_transaction(args.transaction_id)
        print_lines(result["results"])
        print("恢复事务:", result["transactionId"])
        return 0
    except Exception as exc:
        print(console_safe(f"恢复失败: {exc}"), file=sys.stderr)
        return 1


def cmd_pull(hub: Hub, args) -> int:
    agent = getattr(args, "agent", None) or "pi"
    print(f"从 {agent} 拉取 provider 变更 ...")
    hub.pull_from(agent)
    print("已合并到", HUB_CONFIG)
    return 0


def cmd_sync(hub: Hub, args) -> int:
    changed = hub.changed_files()
    if changed:
        print("检测到外部变更:")
        for c in changed:
            print(console_safe(f"  · {c}"))
        # 判断变更来源，先把 apiKey/baseUrl 拉回 hub，再统一下推
        changed_agents = set()
        for c in changed:
            cl = c.lower()
            if f"{os.sep}.pi{os.sep}" in cl:
                changed_agents.add("pi")
            elif "minimax" in cl:
                changed_agents.add("mcode")
            elif "zcode" in cl:
                changed_agents.add("zcode")
            elif f"{os.sep}.dsh{os.sep}" in cl:
                changed_agents.add("dsh")
            elif f"{os.sep}.claude{os.sep}" in cl or "claude" in Path(c).name.lower():
                changed_agents.add("claude")
        for agent in changed_agents:
            try:
                print(f"从 {agent} 拉取变更合并到 hub ...")
                hub.pull_from(agent)
            except Exception as e:  # noqa: BLE001
                print(f"  pull {agent} 失败: {e}")
        print("统一下推到所有目标 ...")
        results = hub.push_all()
        print_lines(results)
    else:
        print("无外部变更，执行常规推送 ...")
        results = hub.push_all()
        print_lines(results)
    if not results or any("✗" in line for line in results):
        return 1
    hub.record_hashes()
    hub.save_state()
    print("同步完成。")
    return 0


def cmd_watch(hub: Hub, args) -> int:
    interval = hub.cfg.get("sync", {}).get("watchIntervalSeconds", 5)
    print(f"监听模式启动 (每 {interval}s 检查一次, Ctrl+C 退出)")
    hub.record_hashes()
    hub.save_state()
    try:
        while True:
            time.sleep(interval)
            changed = hub.changed_files()
            if changed:
                print(f"\n[{now_iso()}] 检测到变更:")
                for c in changed:
                    print(console_safe(f"  · {c}"))
                changed_agents = set()
                for c in changed:
                    cl = c.lower()
                    if f"{os.sep}.pi{os.sep}" in cl:
                        changed_agents.add("pi")
                    elif "minimax" in cl:
                        changed_agents.add("mcode")
                    elif "zcode" in cl:
                        changed_agents.add("zcode")
                    elif f"{os.sep}.dsh{os.sep}" in cl:
                        changed_agents.add("dsh")
                    elif f"{os.sep}.claude{os.sep}" in cl:
                        changed_agents.add("claude")
                for agent in changed_agents:
                    try:
                        hub.pull_from(agent)
                        print(console_safe(f"  ← 已从 {agent} 合并"))
                    except Exception as e:  # noqa: BLE001
                        print(f"  pull {agent} 失败: {e}")
                for line in hub.push_all():
                    print(console_safe(line))
                hub.record_hashes()
                hub.save_state()
                print("已同步。")
    except KeyboardInterrupt:
        print("\n已停止监听。")
    return 0


def cmd_remove(hub: Hub, args) -> int:
    for line in hub.remove_provider(args.provider_id):
        print(line)
    return 0


def cmd_model(hub: Hub, args) -> int:
    """设置多 agent 使用的模型（写入 providers.json 的 modelSync）。

    只改配置，不写客户端；需要再执行 `hub push` 才落到各 agent。
    """
    targets = args.target or []
    canonical = {canonical_target(t) for t in targets}
    unknown = sorted(canonical - {"pi", "dsh", "zcode", "claude", "codex"})
    if unknown:
        print(f"未知目标: {', '.join(unknown)}（可用: pi / dsh / zcode / claude / codex，缺省表示全局）", file=sys.stderr)
        return 1
    pid = args.provider
    if pid not in hub.providers:
        print(f"供应商不存在: {pid}", file=sys.stderr)
        return 1
    if hub.providers[pid].get("enabled") is False:
        print(f"供应商已停用: {pid}", file=sys.stderr)
        return 1
    entry = {"provider": pid, "model": args.model}
    if args.reasoning_effort:
        entry["reasoningEffort"] = args.reasoning_effort
    disk = load_json(HUB_CONFIG, default=None)
    if isinstance(disk, dict):
        hub.cfg = copy.deepcopy(disk)
        hub.providers = hub.cfg.setdefault("providers", {})
        if pid not in hub.providers:
            print(f"供应商已被其它进程删除: {pid}", file=sys.stderr)
            return 1
    # 平台覆盖按该 Agent 自己的连接模型库判断；全局模型仍按默认连接
    # 兼容旧命令行为。手填模型只给出提示，不阻止写入。
    known = set()
    if canonical:
        for target in canonical:
            known.update(m.get("id") for m in agent_connection(hub.providers[pid], target)["models"]
                         if isinstance(m, dict))
    else:
        known.update(m.get("id") for m in hub.providers[pid].get("models") or []
                     if isinstance(m, dict))
    sync = hub.cfg.setdefault("modelSync", {})
    if canonical:
        overrides = sync.setdefault("overrides", {})
        for target in sorted(canonical):
            overrides[target] = copy.deepcopy(entry)
        where = "、".join(sorted(canonical))
    else:
        sync["default"] = copy.deepcopy(entry)
        where = "全局"
    backup_file(HUB_CONFIG)
    save_json(HUB_CONFIG, hub.cfg)
    print(console_safe(f"✓ {where} 模型设为 {pid} / {args.model}"))
    if args.model not in known:
        print(console_safe(f"  ⚠ 该模型不在 {pid} 的模型库里，客户端可能拒绝这个 ID"))
    print("  下一步: hub push  # 或在工作台「平台同步」里执行同步")
    return 0


def cmd_backup(hub: Hub, _args) -> int:
    print("备份目标配置 ...")
    targets = hub.cfg.get("sync", {}).get("targets", {})
    for tname, tcfg in targets.items():
        for f in target_files(tname, tcfg):
            p = expand(f)
            if p.exists():
                backup_file(p)
                print(console_safe(f"  ✓ {p}"))
    print("备份目录:", HUB_BACKUP)
    return 0


def cmd_restore_gold(hub: Hub, _args) -> int:
    """从 providers.gold.json 恢复模型列表（不覆盖 apiKey/baseUrl）。"""
    gold_path = HUB_DIR / "providers.gold.json"
    if not gold_path.exists():
        print(f"缺少黄金模板: {gold_path}", file=sys.stderr)
        return 1
    gold = load_json(gold_path, default={}) or {}
    disk = load_json(HUB_CONFIG, default=None)
    if not isinstance(disk, dict):
        disk = copy.deepcopy(hub.cfg)
    before = copy.deepcopy(disk)
    providers = disk.setdefault("providers", {})
    allowed: dict[str, set[str]] = {}
    restored = 0
    for pid, entry in (gold.get("providers") or {}).items():
        if pid not in providers:
            continue
        models = copy.deepcopy(entry.get("models") or [])
        if models:
            # 这是显式恢复命令：允许按黄金模板重写该供应商的模型列表
            allowed[pid] = {m.get("id") for m in (providers[pid].get("models") or [])}
            # 黄金模板通常只记录 id/name/上下文，恢复时保留模型已有的 cost/compat
            current = {m.get("id"): m for m in (providers[pid].get("models") or [])}
            for model in models:
                old = current.get(model.get("id")) or {}
                for key in ("cost", "compat"):
                    if model.get(key) is None and old.get(key) is not None:
                        model[key] = copy.deepcopy(old[key])
            providers[pid]["models"] = models
            restored += 1
            print(console_safe(f"  ✓ {pid}: {len(models)} models"))
    disk["providers"] = providers
    disk["updatedAt"] = now_iso()
    assert_expected_model_changes(before, disk, allowed_removed=allowed)
    hub.cfg = disk
    hub.providers = providers
    backup_file(HUB_CONFIG)
    save_json(HUB_CONFIG, disk)
    print(f"已恢复 {restored} 个 provider 的模型列表")
    return 0


def cmd_save_gold(hub: Hub, _args) -> int:
    """把当前模型列表存为黄金模板。"""
    gold = {"providers": {pid: {"models": copy.deepcopy(p.get("models") or [])} for pid, p in hub.providers.items()}}
    save_json(HUB_DIR / "providers.gold.json", gold)
    print(f"已写入 {HUB_DIR / 'providers.gold.json'}")
    for pid, p in hub.providers.items():
        print(f"  {pid}: {len(p.get('models') or [])} models")
    return 0


def cmd_migrate_agents(hub: Hub, _args) -> int:
    """把旧版扁平字段迁移成「默认连接 + agents.<agent> 覆盖」。

    - ``anthropicBaseUrl`` → ``agents.claude.baseUrl``（协议记为 anthropic-messages）
    - ``claudeEnv`` / ``claudeSettings`` → ``agents.claude.env`` / ``settings``
    - ``claudeModels`` → ``modelSync.overrides.claude``（主模型 + 各档位）与
      ``agents.claude.models``（该 Anthropic 端点上的模型目录）
    - ``openaiApiKey`` / ``apiKeyEnv`` / ``codexWireApi`` → ``agents.codex``
    - 删除 ``sync.targets.claude.activeProvider``：Claude Code 与其它 Agent 一样，
      只认 modelSync 解析结果

    重复执行是幂等的；写入前照例备份 providers.json。
    """
    disk = load_json(HUB_CONFIG, default=None)
    if not isinstance(disk, dict):
        disk = copy.deepcopy(hub.cfg)
    before = copy.deepcopy(disk)
    providers = disk.setdefault("providers", {})
    targets = disk.setdefault("sync", {}).setdefault("targets", {})
    active = (targets.get("claude") or {}).get("activeProvider")
    active = active if isinstance(active, str) and active in providers else None

    def legacy_tier_models(p: dict) -> list[dict]:
        out, seen = [], set()
        for value in (p.get("claudeModels") or {}).values():
            if isinstance(value, str) and value.strip() and value not in seen:
                seen.add(value)
                out.append({"id": value, "name": value})
        return out

    for pid, p in providers.items():
        agents = p.setdefault("agents", {}) if isinstance(p, dict) else {}
        if not isinstance(p, dict):
            continue
        migrated = []
        base = p.get("anthropicBaseUrl")
        if isinstance(base, str) and base.strip():
            claude = agents.setdefault("claude", {})
            claude.setdefault("baseUrl", base)
            claude["api"] = "anthropic-messages"
            migrated.append("agents.claude.baseUrl")
        if isinstance(p.get("claudeEnv"), dict):
            agents.setdefault("claude", {}).setdefault("env", copy.deepcopy(p["claudeEnv"]))
            migrated.append("agents.claude.env")
        if isinstance(p.get("claudeSettings"), dict):
            agents.setdefault("claude", {}).setdefault("settings", copy.deepcopy(p["claudeSettings"]))
            migrated.append("agents.claude.settings")
        # claudeModels：活跃供应商的档位搬进 modelSync，模型 ID 进入 Claude 连接目录
        if isinstance(p.get("claudeModels"), dict) and pid == active:
            models = legacy_tier_models(p)
            if models:
                existing = [m.get("id") for m in agents.setdefault("claude", {}).get("models", [])]
                for m in models:
                    if m["id"] not in existing:
                        agents["claude"].setdefault("models", []).append(m)
                migrated.append("agents.claude.models")
            overrides = disk.setdefault("modelSync", {}).setdefault("overrides", {})
            claude_override = overrides.setdefault("claude", {})
            tiers = claude_override.setdefault("tiers", {})
            for key, value in p["claudeModels"].items():
                if not isinstance(value, str) or not value.strip():
                    continue
                tiers.setdefault(key, {"provider": pid, "model": value.strip()})
            if not claude_override.get("model"):
                main = p["claudeModels"].get("model") or p["claudeModels"].get("sonnet")
                if isinstance(main, str) and main.strip():
                    claude_override["provider"] = pid
                    claude_override["model"] = main.strip()
            migrated.append("modelSync.overrides.claude")
        codex_keys = [k for k in ("openaiApiKey", "apiKeyEnv", "codexWireApi") if p.get(k)]
        if codex_keys:
            codex = agents.setdefault("codex", {})
            if p.get("openaiApiKey"):
                codex.setdefault("apiKey", p["openaiApiKey"])
            if p.get("apiKeyEnv"):
                codex.setdefault("apiKeyEnv", p["apiKeyEnv"])
            if p.get("codexWireApi") == "responses":
                codex.setdefault("api", "openai-responses")
            migrated.append("agents.codex")
        for field in ("anthropicBaseUrl", "claudeEnv", "claudeSettings", "claudeModels",
                      "openaiApiKey", "apiKeyEnv", "codexWireApi"):
            p.pop(field, None)
        if not agents:
            p.pop("agents", None)
        if migrated:
            print(console_safe(f"  ✓ {pid}: {', '.join(sorted(set(migrated)))}"))
    if active:
        (targets.get("claude") or {}).pop("activeProvider", None)
        print(console_safe(f"  ✓ sync.targets.claude.activeProvider({active}) 已移除"))
    disk["updatedAt"] = now_iso()
    assert_expected_model_changes(before, disk)
    hub.cfg = disk
    hub.providers = disk["providers"]
    backup_file(HUB_CONFIG)
    save_json(HUB_CONFIG, disk)
    print("已迁移为「默认连接 + agents.<agent>」结构。下一步: hub push  # 或在工作台「平台同步」里执行同步")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(prog="hub", description="Agent Hub 统一 provider 同步")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看状态")
    p_push = sub.add_parser("push", help="推送统一配置到各 agent")
    p_push.add_argument("targets", nargs="*", help="仅推送指定 agent")
    p_preview = sub.add_parser("preview", help="只预览和校验，不写入客户端")
    p_preview.add_argument("targets", nargs="*")
    p_restore = sub.add_parser("restore-transaction", help="恢复指定同步事务，保留可逆备份")
    p_restore.add_argument("transaction_id")
    p_pull = sub.add_parser("pull", help="从 agent 拉取变更")
    p_pull.add_argument("agent", nargs="?", default="pi")
    sub.add_parser("sync", help="检测变更并对齐")
    sub.add_parser("watch", help="持续监听自动同步")
    p_rm = sub.add_parser("remove-provider", help="删除 provider")
    p_rm.add_argument("provider_id")
    sub.add_parser("backup", help="备份目标配置")
    sub.add_parser("save-gold", help="将当前模型列表存为黄金模板")
    sub.add_parser("restore-gold", help="从黄金模板恢复模型列表")
    sub.add_parser("migrate-agents", help="把旧的扁平字段迁移成 agents.<agent> 连接结构")
    p_model = sub.add_parser("model", help="设置多 agent 使用的模型（写入 modelSync）")
    p_model.add_argument("provider", help="供应商 ID")
    p_model.add_argument("model", help="模型 ID")
    p_model.add_argument("--target", action="append", choices=["pi", "dsh", "dsh-desktop", "zcode", "claude", "codex"],
                         help="只对指定客户端生效；不传表示全局主模型")
    p_model.add_argument("--reasoning-effort", choices=list(REASONING_EFFORTS),
                         help="推理强度（可选）")

    args = parser.parse_args(argv)
    hub = Hub()
    dispatch = {
        "status": cmd_status,
        "push": cmd_push,
        "preview": cmd_preview,
        "restore-transaction": cmd_restore_transaction,
        "pull": cmd_pull,
        "sync": cmd_sync,
        "watch": cmd_watch,
        "remove-provider": cmd_remove,
        "backup": cmd_backup,
        "save-gold": cmd_save_gold,
        "restore-gold": cmd_restore_gold,
        "migrate-agents": cmd_migrate_agents,
        "model": cmd_model,
    }
    return dispatch[args.cmd](hub, args)


if __name__ == "__main__":
    sys.exit(main())
