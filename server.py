#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent Hub 本地工作台：HTTP API + 无构建依赖的静态界面。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# Hub 自身目录不再绑定用户主目录：按本文件位置定位，
# 整个目录可以任意搬迁（例如从 ~/.agent-hub 移到 G:\workspaces\agent-hub）。
HUB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB_DIR))
from hub import (Hub, HUB_CONFIG, ModelLossError, assert_expected_model_changes, backup_file,
                 agent_connection, agent_connection_ready, agent_connections,
                 canonical_target, provider_targets,
                 configure_stdio, expand, file_hash, load_json, save_json, now_iso, target_files)  # noqa: E402
from discover import discover_models  # noqa: E402

# hub.py 是多客户端模型对齐的唯一实现，这里只调用、不复制。
# 用带保护的 import：hub.py 尚未落地这些接口时，受影响的两个端点给出明确错误，
# 其余 API 与界面仍可正常启动。
try:  # pragma: no cover - 正常仓库里始终走 try 分支
    from hub import (REASONING_EFFORTS, agent_model_options, agent_model_tiers_public,
                     resolve_agent_model)  # noqa: E402
except ImportError:  # pragma: no cover
    REASONING_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    agent_model_options = agent_model_tiers_public = resolve_agent_model = None

UI_DIR = HUB_DIR / "ui"
LOCK = threading.Lock()
SUPPORTED_TARGETS = {"pi", "mcode", "zcode", "dsh", "dsh-desktop", "claude", "cc-switch", "omp", "codex"}
PREVIEW_TARGETS = SUPPORTED_TARGETS - {"cc-switch"}
PROTOCOLS = {"openai-completions", "openai-responses", "openai-chat-completions", "anthropic-messages"}
# 每个 Agent 连接允许的协议：Claude Code 只走 Anthropic Messages，Codex 第三方只走 Responses。
AGENT_APIS = {"claude": {"anthropic-messages"}, "codex": {"openai-responses"}}
# providers.json 里可以按 Agent 覆盖连接的目标名（dsh-desktop 归一化成 dsh）。
AGENT_CONNECTION_KEYS = ("pi", "dsh", "claude", "codex")

# modelSync 可写的目标 -> 落盘时使用的键。dsh 桌面版与 CLI 共用同一份配置，
# 写入时归一化成 dsh，避免出现两份互相漂移的覆盖。
MODELSYNC_TARGETS = {"pi": "pi", "dsh": "dsh", "dsh-desktop": "dsh",
                     "claude": "claude", "codex": "codex"}
# status / GET 需要给出解析结果的目标，顺序即界面展示顺序。
MODELSYNC_RESOLVED_TARGETS = ("pi", "dsh", "dsh-desktop", "claude", "codex")
# 已经没有只读目标：codex 从 v2 起只写 model_provider / model 与 [model_providers.custom]，
# ChatGPT 登录态仍然由 Codex 自己维护。
MODELSYNC_READONLY_TARGETS: set[str] = set()
# Claude Code 可单独指定的档位；fable 是历史档位，保留以兼容既有 claudeModels。
CLAUDE_TIER_KEYS = ("model", "opus", "sonnet", "haiku", "subagent", "fable")
MODELSYNC_ENTRY_KEYS = ("provider", "model", "reasoningEffort")


def hub() -> Hub:
    return Hub()


def _save_config(h: Hub, allowed_removed: dict[str, set[str]] | None = None) -> None:
    """写入 providers.json。

    `allowed_removed` 声明本次操作明确要删除的模型；除此之外只要磁盘上
    已有模型会变少就拒绝写入，避免任何路径静默清空模型库。
    """
    h.cfg["providers"] = h.providers
    h.cfg["updatedAt"] = now_iso()
    disk = load_json(HUB_CONFIG, default=None)
    if isinstance(disk, dict):
        assert_expected_model_changes(disk, h.cfg, allowed_removed=allowed_removed)
    backup_file(HUB_CONFIG)
    save_json(HUB_CONFIG, h.cfg)


def _provider(h: Hub, pid: str) -> dict:
    if pid not in h.providers:
        raise ValueError(f"供应商不存在: {pid}")
    return h.providers[pid]


def _validate_provider(p: dict) -> None:
    if not isinstance(p.get("name"), str) or not p["name"].strip():
        raise ValueError("name 不能为空")
    # agents 先校验：后面的判定都建立在「每条覆盖都是对象」之上
    _validate_agents(p.get("agents"))
    agents = p.get("agents") if isinstance(p.get("agents"), dict) else {}
    claude_override = agents.get("claude")
    claude_override = claude_override if isinstance(claude_override, dict) else {}
    targets = p.get("targets")
    claude_only = (isinstance(targets, list) and bool(targets)
                   and all(canonical_target(target) == "claude" for target in targets))
    has_claude_connection = bool(str(claude_override.get("baseUrl") or "").strip())
    if not isinstance(p.get("baseUrl"), str):
        raise ValueError("baseUrl 必须是字符串")
    if not p["baseUrl"].strip() and not (has_claude_connection or (claude_only and p.get("anthropicBaseUrl"))):
        raise ValueError("baseUrl 不能为空；只给 Claude Code 用的供应商可只配置 Claude 连接或 anthropicBaseUrl")
    for field in ("apiKey", "openaiApiKey", "apiKeyEnv"):
        if p.get(field) is not None and not isinstance(p[field], str):
            raise ValueError(f"{field} 必须是字符串或 null")
    # 密钥可以只配在某个 Agent 连接上（同一网关给不同 Agent 发不同 key）
    agents_block = p.get("agents") if isinstance(p.get("agents"), dict) else {}
    agent_has_key = any((block or {}).get("apiKey") or (block or {}).get("apiKeyEnv")
                        for block in agents_block.values()
                        if isinstance(block, dict))
    if not any(p.get(field) for field in ("apiKey", "openaiApiKey", "apiKeyEnv")) and not agent_has_key:
        raise ValueError("请配置 API Key、Codex 专用 Key、API Key 环境变量名，或在某个 Agent 连接上配置密钥")
    if p.get("apiKeyEnv") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p["apiKeyEnv"]):
        raise ValueError("apiKeyEnv 必须是有效的环境变量名")
    if p.get("codexWireApi") not in (None, "", "responses"):
        raise ValueError("Codex 仅支持明确声明的 responses 协议")
    for field in ("displayName", "api", "anthropicBaseUrl", "notes"):
        if field in p and not isinstance(p[field], str):
            raise ValueError(f"{field} 必须是字符串")
    # 供应商推荐给 agent 用的模型：只能字符串或 null，回退逻辑在 hub.py。
    if p.get("agentModel") is not None and not isinstance(p["agentModel"], str):
        raise ValueError("agentModel 必须是字符串或 null")
    # Codex 专用 key：同一个网关给 Claude 与 Codex 发不同 key 时用，只能字符串或 null。
    if p.get("openaiApiKey") is not None and not isinstance(p["openaiApiKey"], str):
        raise ValueError("openaiApiKey 必须是字符串或 null")
    for field in ("baseUrl", "anthropicBaseUrl"):
        if p.get(field):
            u = urlparse(p[field])
            if u.scheme not in ("http", "https") or not u.netloc:
                raise ValueError(f"{field} 必须是有效的 HTTP / HTTPS 地址")
    if p.get("api") not in PROTOCOLS:
        raise ValueError("不支持的 API 协议")
    if "enabled" in p and not isinstance(p["enabled"], bool):
        raise ValueError("enabled 必须是布尔值")
    # 旧 Claude 专属字段：迁移命令会搬进 agents.claude，这里只做兼容校验。
    for field in ("claudeEnv", "claudeSettings", "claudeModels"):
        if field in p and not isinstance(p[field], dict):
            raise ValueError(f"{field} 必须是对象")
    if "claudeModels" in p:
        for k, v in p["claudeModels"].items():
            if v is not None and not isinstance(v, str):
                raise ValueError("claudeModels 的值必须是字符串或 null")
    for field in ("claudeEnv", "claudeSettings"):
        for k, v in (p.get(field) or {}).items():
            if not isinstance(k, str) or not k or k in ("env", "$schema"):
                raise ValueError(f"{field} 的键名无效")
            if not isinstance(v, (str, int, float, bool, dict, list)):
                raise ValueError(f"{field}[{k}] 的值类型不受支持")
    if "targets" in p:
        t = p["targets"]
        if not isinstance(t, list) or not all(isinstance(x, str) for x in t):
            raise ValueError("targets 必须是字符串数组")
        unknown = [x for x in t if x not in SUPPORTED_TARGETS]
        if unknown:
            raise ValueError(f"未知的目标平台: {', '.join(sorted(unknown))}")
    _validate_agents(p.get("agents"))


def _validate_agents(agents: object) -> None:
    """校验 agents.<agent> 按 Agent 覆盖：端点、密钥、协议与模型列表。"""
    if agents is None:
        return
    if not isinstance(agents, dict):
        raise ValueError("agents 必须是对象")
    for name, block in agents.items():
        if name not in AGENT_CONNECTION_KEYS:
            raise ValueError(f"未知的 Agent 覆盖: {name}（可用: {', '.join(AGENT_CONNECTION_KEYS)}）")
        if not isinstance(block, dict):
            raise ValueError(f"agents.{name} 必须是对象")
        unknown = sorted(set(block) - {"baseUrl", "apiKey", "apiKeyEnv", "api", "models",
                                       "compat", "env", "settings"})
        if unknown:
            raise ValueError(f"agents.{name} 包含未知字段: {', '.join(unknown)}")
        for field in ("baseUrl", "apiKey", "apiKeyEnv", "api"):
            if field in block and block[field] is not None and not isinstance(block[field], str):
                raise ValueError(f"agents.{name}.{field} 必须是字符串或 null")
        for field in ("baseUrl",):
            if block.get(field):
                u = urlparse(block[field])
                if u.scheme not in ("http", "https") or not u.netloc:
                    raise ValueError(f"agents.{name}.{field} 必须是有效的 HTTP / HTTPS 地址")
        if "api" in block and block["api"]:
            allowed = AGENT_APIS.get(name)
            if allowed and block["api"] not in allowed:
                raise ValueError(f"agents.{name}.api 只支持 {'/'.join(sorted(allowed))}")
        if block.get("apiKeyEnv") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", block["apiKeyEnv"]):
            raise ValueError(f"agents.{name}.apiKeyEnv 必须是有效的环境变量名")
        if "models" in block and block["models"] is not None:
            if not isinstance(block["models"], list):
                raise ValueError(f"agents.{name}.models 必须是数组")
            for m in block["models"]:
                if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"].strip():
                    raise ValueError(f"agents.{name}.models 的条目必须带非空 id")
        for field in ("env", "settings"):
            if field in block and block[field] is not None:
                if not isinstance(block[field], dict):
                    raise ValueError(f"agents.{name}.{field} 必须是对象")
                for k, v in block[field].items():
                    if not isinstance(k, str) or not k or k in ("env", "$schema"):
                        raise ValueError(f"agents.{name}.{field} 的键名无效")
                    if not isinstance(v, (str, int, float, bool, dict, list)):
                        raise ValueError(f"agents.{name}.{field}[{k}] 的值类型不受支持")


def _modelsync_block(cfg: dict) -> dict:
    block = cfg.get("modelSync")
    return block if isinstance(block, dict) else {}


def _modelsync_api_ready() -> bool:
    return not (resolve_agent_model is None or agent_model_tiers_public is None
                or agent_model_options is None)


def _require_modelsync_api() -> None:
    if not _modelsync_api_ready():
        raise RuntimeError("hub.py 未提供 modelSync 接口: "
                           "resolve_agent_model / agent_model_tiers_public / agent_model_options")


def _modelsync_view(cfg: dict) -> tuple[dict, dict]:
    """每个客户端实际会写入的模型（resolved）与各档位明细（tiers）。

    dsh-desktop 是 dsh 的别名，跟随 dsh；Codex 模型只在显式第三方模式写入。
    档位用 hub.py 的界面视图 agent_model_tiers_public：不含内部 agent 档与历史
    fable 档。适配器读的是 agent_model_tiers 本体，fable 因此不会被解析层吞掉。
    hub.py 接口缺失时降级为空结果，让状态接口仍可用。
    """
    resolved: dict[str, dict | None] = {t: None for t in MODELSYNC_RESOLVED_TARGETS}
    tiers: dict[str, dict] = {}
    if not _modelsync_api_ready():
        return resolved, tiers
    for target in MODELSYNC_RESOLVED_TARGETS:
        if target in MODELSYNC_READONLY_TARGETS:
            continue
        canonical = MODELSYNC_TARGETS.get(target, target)
        tiers[target] = agent_model_tiers_public(cfg, canonical)
        resolved[target] = resolve_agent_model(cfg, canonical)
    return resolved, tiers


def _validate_modelsync_entry(h: Hub, entry: object, where: str, *, allow_tiers: bool = False) -> dict:
    """校验 modelSync.default / overrides.<target> 的一条模型映射。"""
    if not isinstance(entry, dict):
        raise ValueError(f"{where} 必须是对象")
    allowed = set(MODELSYNC_ENTRY_KEYS) | ({"tiers"} if allow_tiers else set())
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ValueError(f"{where} 包含未知字段: {', '.join(unknown)}")
    pid = entry.get("provider")
    if not isinstance(pid, str) or not pid.strip():
        raise ValueError(f"{where}.provider 必须是非空字符串")
    pid = pid.strip()
    p = h.providers.get(pid)
    if p is None:
        raise ValueError(f"{where}.provider 不存在: {pid}")
    if p.get("enabled", True) is False:
        raise ValueError(f"{where}.provider 未启用: {pid}")
    model = entry.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"{where}.model 必须是非空字符串")
    effort = entry.get("reasoningEffort")
    efforts = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra") \
        if where == "modelSync.overrides.codex" else REASONING_EFFORTS
    if effort is not None and effort not in efforts:
        raise ValueError(f"{where}.reasoningEffort 只能是 {'/'.join(efforts)} 或 null")
    checked = {"provider": pid, "model": model.strip(), "reasoningEffort": effort}
    if "tiers" in entry:
        tiers = entry["tiers"]
        if not isinstance(tiers, dict):
            raise ValueError(f"{where}.tiers 必须是对象")
        unknown_tiers = sorted(set(tiers) - set(CLAUDE_TIER_KEYS))
        if unknown_tiers:
            raise ValueError(f"{where}.tiers 包含未知档位: {', '.join(unknown_tiers)}")
        checked["tiers"] = {name: _validate_modelsync_entry(h, tier, f"{where}.tiers.{name}")
                            for name, tier in tiers.items()}
    return checked


def _normalize_modelsync(h: Hub, raw: object) -> dict:
    """整块替换前的校验与归一化；返回最终落盘的结构。"""
    if not isinstance(raw, dict):
        raise ValueError("modelSync 必须是对象")
    unknown = sorted(set(raw) - {"default", "overrides"})
    if unknown:
        raise ValueError(f"modelSync 包含未知字段: {', '.join(unknown)}")
    out: dict = {}
    if "default" in raw:
        out["default"] = _validate_modelsync_entry(h, raw["default"], "modelSync.default")
    overrides = raw.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("modelSync.overrides 必须是对象")
    normalized: dict[str, dict] = {}
    for name, entry in overrides.items():
        if name not in MODELSYNC_TARGETS:
            raise ValueError(f"未知的 modelSync 目标: {name}"
                             f"（只允许 {', '.join(MODELSYNC_TARGETS)}）")
        canonical = MODELSYNC_TARGETS[name]
        checked = _validate_modelsync_entry(h, entry, f"modelSync.overrides.{name}",
                                            allow_tiers=canonical == "claude")
        if canonical in normalized and normalized[canonical] != checked:
            raise ValueError(f"modelSync.overrides 里 {canonical} 与 {name} 不能用不同内容同时指定")
        normalized[canonical] = checked
    if overrides:
        out["overrides"] = normalized
    return out


def _modelsync_models(block: dict) -> list[tuple[str, str, str]]:
    out = []
    default = block.get("default")
    if isinstance(default, dict):
        out.append(("modelSync.default", default["provider"], default["model"]))
    for name, entry in (block.get("overrides") or {}).items():
        out.append((f"modelSync.overrides.{name}", entry["provider"], entry["model"]))
        for tier, config in (entry.get("tiers") or {}).items():
            out.append((f"modelSync.overrides.{name}.tiers.{tier}", config["provider"], config["model"]))
    return out


def _modelsync_warnings(h: Hub, block: dict) -> list[str]:
    """手填模型不在对应连接的模型库里时给出提示；这是放行的手填值，不阻止写入。

    全局主模型按该供应商所有连接的并集判断；平台 / 档位只按目标 Agent 自己的
    连接判断——同一供应商给 Claude 与 Codex 配不同 key 时，模型集合本就不同。
    """
    warnings = []
    every = {pid: {m["id"] for m in models}
             for pid, models in agent_model_options(h.cfg).items()}
    per_target = {target: {pid: {m["id"] for m in models}
                           for pid, models in agent_model_options(h.cfg, target).items()}
                  for target in set(MODELSYNC_TARGETS.values())}
    for where, pid, model in _modelsync_models(block):
        parts = where.split(".")
        catalog = every.get(pid, set())
        if ".overrides." in where:
            catalog = per_target.get(parts[2], {}).get(pid, every.get(pid, set()))
        if model not in catalog:
            warnings.append(f"{where} 的模型 {model} 不在供应商 {pid} 的模型库中，按手填值写入")
    return warnings


def api_modelsync() -> dict:
    """当前 modelSync 配置 + 每个客户端实际会写入的模型，供界面预填与预览。"""
    _require_modelsync_api()
    h = hub()
    block = _modelsync_block(h.cfg)
    resolved, tiers = _modelsync_view(h.cfg)
    options = agent_model_options(h.cfg)
    # 每个 Agent 连接各自可见的模型：同一供应商给不同 Agent 配不同 key 时，
    # 模型下拉只应出现这条连接真正提供的模型。
    per_target = {target: agent_model_options(h.cfg, target)
                  for target in MODELSYNC_RESOLVED_TARGETS if target != "dsh-desktop"}
    return {"modelSync": block, "resolved": resolved, "tiers": tiers,
            "options": options, "optionsByTarget": per_target,
            "configured": bool(block.get("default") or block.get("overrides"))}


def api_update_modelsync(body: dict) -> dict:
    """整块替换 modelSync；写入走 _save_config，因此有备份与模型丢失护栏。"""
    if "modelSync" not in body:
        raise ValueError("缺少 modelSync 字段")
    _require_modelsync_api()
    with LOCK:
        h = hub()
        block = _normalize_modelsync(h, body["modelSync"])
        warnings = _modelsync_warnings(h, block)
        h.cfg["modelSync"] = block
        _save_config(h)
        resolved, tiers = _modelsync_view(h.cfg)
    return {"ok": True, "modelSync": block, "warnings": warnings,
            "resolved": resolved, "tiers": tiers}


def api_providers() -> dict:
    h = hub()
    return {"providers": h.providers, "removedProviders": h.cfg.get("removedProviders", {})}


def api_status() -> dict:
    h = hub()
    changed = set(h.changed_files())
    targets = {}
    for name, t in (h.cfg.get("sync", {}).get("targets") or {}).items():
        files = [{"path": str(expand(f)), "exists": expand(f).exists(),
                  "changed": str(expand(f)) in changed} for f in target_files(name, t)]
        targets[name] = {
            "enabled": t.get("enabled", False), "aliasOf": t.get("aliasOf"),
            "notes": t.get("notes"),
            "supported": name in PREVIEW_TARGETS, "files": files,
            "mode": t.get("mode", "official") if name == "codex" else None,
            "readonly": name not in PREVIEW_TARGETS,
        }
    model_sync = _modelsync_block(h.cfg)
    resolved, _tiers = _modelsync_view(h.cfg)
    # 每个 Agent 各自可用的供应商（连接就绪：端点 + 协议匹配，且 targets 允许）。
    capable = {name: [pid for pid, p in h.providers.items()
                      if p.get("enabled", True) and provider_targets(p, name)
                      and agent_connection_ready(p, name)]
               for name in AGENT_CONNECTION_KEYS}
    return {
        "app": "agent-hub", "uiVersion": 2,
        "statusText": h.status(), "providerCount": len(h.providers), "targets": targets,
        "anthropicCapable": capable["claude"], "agentCapable": capable,
        "connections": agent_connections(h.cfg),
        "configPath": str(HUB_CONFIG), "updatedAt": h.cfg.get("updatedAt"),
        "lastSync": h.state.get("lastSync"), "lastResults": h.state.get("lastResults", []),
        "lastTransaction": h.state.get("lastTransaction"),
        "pendingSync": h.state.get("configHash") != file_hash(HUB_CONFIG),
        "changedFiles": sorted(changed),
        "modelSync": model_sync, "modelSyncResolved": resolved,
    }


def _query_agent(query: dict | None) -> str | None:
    """从查询串里取 ?agent=<name>：模型按连接（key）组织时的连接选择。"""
    values = (query or {}).get("agent") or []
    value = values[0].strip() if values and isinstance(values[0], str) else ""
    return value or None


def _selected_targets(h: Hub, body: dict) -> list[str]:
    targets = h.cfg.get("sync", {}).get("targets", {})
    enabled = {name for name, t in targets.items()
               if name in PREVIEW_TARGETS and t.get("enabled", False)}
    requested = body.get("targets")
    if requested is not None and (not isinstance(requested, list) or not requested
                                  or any(not isinstance(n, str) for n in requested)):
        raise ValueError("请至少选择一个同步平台")
    names = set(requested) if requested is not None else enabled
    if not names or not names <= enabled:
        raise ValueError("同步目标为空、未启用或不受支持")
    selected = sorted({targets[n].get("aliasOf") or n for n in names})
    if any(n not in enabled for n in selected):
        raise ValueError("关联的主平台未启用")
    return selected


def _record_sync(h: Hub, results: list[str], changed: list[str] | None = None,
                 transaction_id: str | None = None) -> dict:
    details = []
    for result in results:
        text = result.strip()
        ok = text.startswith("✓")
        name, _, message = text[1:].strip().partition(":")
        details.append({"target": name.strip(), "ok": ok,
                        "message": message.strip() or "配置已写入；重启对应客户端后生效"})
    targets = h.cfg.get("sync", {}).get("targets", {})
    canonical = lambda name: targets[name].get("aliasOf") or name
    succeeded = {d["target"] for d in details if d["ok"]}
    hashes = h.state.setdefault("hashes", {})
    for name, target in targets.items():
        if canonical(name) in succeeded:
            for file in target_files(name, target):
                hashes[str(expand(file))] = file_hash(expand(file))
    all_enabled = {canonical(n) for n, t in targets.items()
                   if n in PREVIEW_TARGETS and t.get("enabled", False)}
    if all_enabled <= succeeded:
        h.state["configHash"] = file_hash(HUB_CONFIG)
    h.state["lastResults"] = details
    if transaction_id:
        h.state["lastTransaction"] = transaction_id
    h.save_state()
    return {"ok": bool(details) and all(d["ok"] for d in details),
            "results": results, "details": details, "changed": changed or [],
            "transactionId": transaction_id}


def _sync(body: dict, detect: bool = False) -> dict:
    # Legacy API: the workbench uses explicit preview/apply below.
    with LOCK:
        h = hub()
        selected = _selected_targets(h, body)
        changed = h.changed_files() if detect else []
        results = h.push_all(only=selected)
        return _record_sync(h, results, changed)


def api_preview(body: dict) -> dict:
    with LOCK:
        h = hub()
        selected = _selected_targets(h, body)
        plan = h.preview_push(only=selected)
        # A preview is metadata only. Never expose generated config bytes or keys.
        return {"planId": plan["planId"], "targets": plan["targets"],
                "files": [{"path": f["path"], "action": f["action"]}
                          for f in plan.get("files", [])],
                "warnings": plan.get("warnings", [])}


def api_apply(body: dict) -> dict:
    plan_id = body.get("planId")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise ValueError("请先生成并确认同步预览，再提交 planId")
    with LOCK:
        result = hub().apply_preview(plan_id)
        # Applying may update Hub configuration; reload before recording status.
        return _record_sync(hub(), result["results"], transaction_id=result["transactionId"])


def api_restore(body: dict) -> dict:
    transaction_id = body.get("transactionId")
    if not isinstance(transaction_id, str) or not transaction_id.strip():
        raise ValueError("缺少需要恢复的 transactionId")
    with LOCK:
        result = hub().restore_transaction(transaction_id)
        h = hub()
        h.state["configHash"] = None
        h.state["lastTransaction"] = None
        h.state["lastResults"] = [{"target": "restore", "ok": True,
                                  "message": "已恢复该次同步前的配置；请重新预览后再同步"}]
        h.save_state()
        return {"ok": True, **result}


def api_push(body: dict | None = None) -> dict:
    return _sync(body or {})


def api_sync(body: dict | None = None) -> dict:
    return _sync(body or {}, detect=True)


def api_update_targets(body: dict) -> dict:
    updates = body.get("targets")
    if not isinstance(updates, dict) or not updates:
        raise ValueError("targets 不能为空")
    with LOCK:
        h = hub()
        targets = h.cfg.get("sync", {}).get("targets", {})
        proposed = {name: dict(t) for name, t in targets.items()}
        for name, update in updates.items():
            if name not in targets or name not in PREVIEW_TARGETS or targets[name].get("aliasOf"):
                raise ValueError(f"无法单独设置此平台: {name}")
            if not isinstance(update, dict):
                raise ValueError("平台设置必须是对象")
            if "enabled" in update:
                if not isinstance(update["enabled"], bool):
                    raise ValueError("enabled 必须是布尔值")
                proposed[name]["enabled"] = update["enabled"]
            unknown = set(update) - {"enabled", "mode"}
            if unknown:
                raise ValueError("未知的平台设置字段")
            if "mode" in update:
                if name != "codex" or update["mode"] not in ("official", "third-party"):
                    raise ValueError("Codex mode 必须是 official 或 third-party")
                proposed[name]["mode"] = update["mode"]
        for name, t in proposed.items():
            if t.get("aliasOf"):
                t["enabled"] = proposed.get(t["aliasOf"], {}).get("enabled", False)
        h.cfg["sync"]["targets"] = proposed
        _save_config(h)
    return {"ok": True}


def api_add_provider(body: dict) -> dict:
    if not isinstance(body.get("id"), str):
        raise ValueError("供应商 ID 必须是字符串")
    pid = body["id"].strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", pid):
        raise ValueError("供应商 ID 只能包含字母、数字、_、-，且以字母或数字开头")
    p = {"name": body.get("name") or pid,
         "displayName": body.get("displayName") or body.get("name") or pid,
         "baseUrl": body.get("baseUrl") or "", "apiKey": body.get("apiKey") or "",
         "api": body.get("api") or "openai-completions", "enabled": True, "models": []}
    for field in ("anthropicBaseUrl", "notes", "agentModel", "openaiApiKey", "apiKeyEnv", "codexWireApi"):
        if body.get(field):
            p[field] = body[field]
    for field in ("claudeModels", "claudeEnv", "claudeSettings"):
        if field in body:
            p[field] = body[field]
    if body.get("agents"):
        p["agents"] = body["agents"]
    if "targets" in body:
        p["targets"] = body["targets"]
    _validate_provider(p)
    with LOCK:
        h = hub()
        if pid in h.providers:
            raise ValueError(f"供应商已存在: {pid}")
        h.providers[pid] = p
        h.cfg.get("removedProviders", {}).pop(pid, None)
        _save_config(h)
    return {"ok": True, "id": pid}


def api_update_provider(pid: str, body: dict) -> dict:
    with LOCK:
        h = hub()
        p = dict(_provider(h, pid))
        for field in ("name", "displayName", "baseUrl", "apiKey", "api", "notes", "anthropicBaseUrl",
                      "enabled", "agentModel", "openaiApiKey", "apiKeyEnv", "codexWireApi"):
            if field in body:
                p[field] = None if field == "codexWireApi" and body[field] == "" else body[field]
        if "claudeModels" in body and isinstance(body["claudeModels"], dict):
            p["claudeModels"] = body["claudeModels"]
        for field in ("claudeEnv", "claudeSettings"):
            if field in body:
                p[field] = body[field]
        if "agents" in body:
            p["agents"] = body["agents"]
        if "targets" in body:
            p["targets"] = body["targets"]
        _validate_provider(p)
        h.providers[pid] = p
        _save_config(h)
    return {"ok": True}


def api_delete_provider(pid: str) -> dict:
    with LOCK:
        h = hub()
        _provider(h, pid)
        backup_file(HUB_CONFIG)
        msgs = h.remove_provider(pid)
    return {"ok": True, "messages": msgs}


def _normalize_model(body: dict) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"].strip():
        raise ValueError("模型 ID 不能为空")
    if "name" in body and not isinstance(body["name"], str):
        raise ValueError("模型名称必须是字符串")
    inp = body.get("input", ["text"])
    if isinstance(inp, str):
        inp = [s.strip() for s in inp.split(",") if s.strip()]
    if not isinstance(inp, list) or not inp or any(not isinstance(v, str) or not v.strip() for v in inp):
        raise ValueError("至少选择一种输入类型")
    numbers = {}
    for field, default in (("contextWindow", 128000), ("maxTokens", 32768)):
        value = body.get(field, default)
        try:
            n = int(value)
        except (TypeError, ValueError, OverflowError) as e:
            raise ValueError(f"{field} 必须是正整数") from e
        if isinstance(value, bool) or n <= 0 or str(n) != str(value):
            raise ValueError(f"{field} 必须是正整数")
        numbers[field] = n
    if not isinstance(body.get("reasoning", True), bool):
        raise ValueError("reasoning 必须是布尔值")
    # 编辑时保留 cost / compat 等非 UI 字段，不悄悄丢弃高级配置。
    # agent 是「这条模型属于哪个连接」的路由参数，不是模型字段，不能落进配置。
    fields = {k: v for k, v in body.items() if k != "agent"}
    return {**fields, "id": body["id"].strip(), "name": body.get("name") or body["id"].strip(),
            **numbers, "input": list(dict.fromkeys(inp)), "reasoning": body.get("reasoning", True)}


def _connection_models(h: Hub, pid: str, agent: str | None) -> list[dict]:
    """某个连接上的模型列表：agent 为空表示默认连接。"""
    p = _provider(h, pid)
    if agent:
        name = canonical_target(agent)
        if name not in AGENT_CONNECTION_KEYS:
            raise ValueError(f"未知的连接: {agent}")
        return agent_connection(p, name)["models"]
    return p.get("models") or []


def _set_connection_models(h: Hub, pid: str, agent: str | None, models: list[dict]) -> None:
    """写回某个连接上的模型列表（agents.<agent>.models 或顶层 models）。"""
    p = _provider(h, pid)
    if agent:
        name = canonical_target(agent)
        if name not in AGENT_CONNECTION_KEYS:
            raise ValueError(f"未知的连接: {agent}")
        override = p.setdefault("agents", {}).setdefault(name, {})
        override["models"] = models
    else:
        p["models"] = models


def api_add_model(pid: str, body: dict) -> dict:
    new = _normalize_model(body)
    agent = body.get("agent") or None
    with LOCK:
        h = hub()
        models = _connection_models(h, pid, agent)
        if any(m.get("id") == new["id"] for m in models):
            raise ValueError(f"模型已存在: {new['id']}")
        models.append(new)
        _set_connection_models(h, pid, agent, models)
        _save_config(h)
    return {"ok": True, "id": new["id"]}


def api_update_model(pid: str, mid: str, body: dict) -> dict:
    agent = body.get("agent") or None
    with LOCK:
        h = hub()
        models = _connection_models(h, pid, agent)
        old = next((m for m in models if m.get("id") == mid), None)
        if old is None:
            raise ValueError(f"模型不存在: {mid}")
        new = _normalize_model({**old, **body})
        if any(m is not old and m.get("id") == new["id"] for m in models):
            raise ValueError(f"模型 ID 已存在: {new['id']}")
        old.clear()
        old.update(new)
        _save_config(h)
    return {"ok": True}


def api_delete_model(pid: str, mid: str, agent: str | None = None) -> dict:
    with LOCK:
        h = hub()
        models = _connection_models(h, pid, agent)
        if not any(m.get("id") == mid for m in models):
            raise ValueError(f"模型不存在: {mid}")
        _set_connection_models(h, pid, agent, [m for m in models if m.get("id") != mid])
        _save_config(h, allowed_removed={pid: {mid}})
    return {"ok": True}


def api_batch_delete_models(pid: str, body: dict) -> dict:
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
        raise ValueError("请提供要删除的模型 ID 列表")
    agent = body.get("agent") or None
    with LOCK:
        h = hub()
        models = _connection_models(h, pid, agent)
        remaining = [m for m in models if m.get("id") not in set(ids)]
        removed = len(models) - len(remaining)
        _set_connection_models(h, pid, agent, remaining)
        _save_config(h, allowed_removed={pid: set(ids)})
    return {"ok": True, "removed": removed}


def api_discover_models(pid: str, agent: str | None = None) -> dict:
    h = hub()
    models = discover_models(_provider(h, pid), agent)
    return {"ok": True, "count": len(models), "models": models}


def api_import_models(pid: str, body: dict) -> dict:
    models = body.get("models")
    mode = body.get("mode", "merge")
    if not isinstance(models, list) or not models:
        raise ValueError("请至少选择一个模型")
    if mode not in ("merge", "replace"):
        raise ValueError("导入模式必须为 merge 或 replace")
    agent = body.get("agent") or None
    normalized = [_normalize_model(m) for m in models]
    if len({m["id"] for m in normalized}) != len(normalized):
        raise ValueError("导入列表包含重复模型 ID")
    with LOCK:
        h = hub()
        existing = _connection_models(h, pid, agent)
        if mode == "replace" and len(existing) >= 5 and len(normalized) < len(existing) * 0.5 and not body.get("confirm"):
            raise ValueError("模型数量将减少一半以上；确认无误后传 confirm=true")
        by_id = {m["id"]: m for m in existing}
        added = sum(m["id"] not in by_id for m in normalized)
        if mode == "replace":
            merged = normalized
        else:
            for m in normalized:
                if m["id"] not in by_id:
                    by_id[m["id"]] = m
                elif body.get("updateExisting"):
                    by_id[m["id"]] = {**by_id[m["id"]], **m}
            merged = list(by_id.values())
        _set_connection_models(h, pid, agent, merged)
        allowed = {pid: {m.get("id") for m in existing}} if mode == "replace" else None
        _save_config(h, allowed_removed=allowed)
    return {"ok": True, "count": len(merged), "added": added}


# URL 编码必须在路由匹配之后解码：openrouter 等模型 ID 自带 /。
ROUTES = [
    ("GET", r"^/api/providers$", lambda m, b, q=None: api_providers()),
    ("GET", r"^/api/status$", lambda m, b, q=None: api_status()),
    ("POST", r"^/api/preview$", lambda m, b, q=None: api_preview(b)),
    ("POST", r"^/api/apply$", lambda m, b, q=None: api_apply(b)),
    ("POST", r"^/api/restore$", lambda m, b, q=None: api_restore(b)),
    ("POST", r"^/api/push$", lambda m, b, q=None: api_push(b)),
    ("POST", r"^/api/sync$", lambda m, b, q=None: api_sync(b)),
    ("PUT", r"^/api/targets$", lambda m, b, q=None: api_update_targets(b)),
    ("GET", r"^/api/modelsync$", lambda m, b, q=None: api_modelsync()),
    ("PUT", r"^/api/modelsync$", lambda m, b, q=None: api_update_modelsync(b)),
    ("POST", r"^/api/providers$", lambda m, b, q=None: api_add_provider(b)),
    ("PUT", r"^/api/providers/([^/]+)$", lambda m, b, q=None: api_update_provider(unquote(m[1]), b)),
    ("DELETE", r"^/api/providers/([^/]+)$", lambda m, b, q=None: api_delete_provider(unquote(m[1]))),
    ("POST", r"^/api/providers/([^/]+)/models$", lambda m, b, q=None: api_add_model(unquote(m[1]), b)),
    ("PUT", r"^/api/providers/([^/]+)/models/([^/]+)$", lambda m, b, q=None: api_update_model(unquote(m[1]), unquote(m[2]), b)),
    ("DELETE", r"^/api/providers/([^/]+)/models/([^/]+)$", lambda m, b, q=None: api_delete_model(unquote(m[1]), unquote(m[2]), _query_agent(q))),
    ("GET", r"^/api/providers/([^/]+)/discover$", lambda m, b, q=None: api_discover_models(unquote(m[1]), _query_agent(q))),
    ("POST", r"^/api/providers/([^/]+)/models/batch-delete$", lambda m, b, q=None: api_batch_delete_models(unquote(m[1]), b)),
    ("POST", r"^/api/providers/([^/]+)/models/import$", lambda m, b, q=None: api_import_models(unquote(m[1]), b)),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, code: int, obj: dict | list) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 2_000_000:
            raise ValueError("请求体过大或无效")
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError("请求必须是有效 JSON") from e
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body

    def _handle_api(self, method: str) -> None:
        # 本地工具不允许其他网页跨站触发配置写入。
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            self._send_json(403, {"error": "拒绝跨站请求"})
            return
        split = urlparse(self.path)
        path = split.path
        try:
            body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
            query = parse_qs(split.query)
            for verb, pattern, fn in ROUTES:
                match = re.match(pattern, path) if verb == method else None
                if match:
                    self._send_json(200, fn(match, body, query))
                    return
            self._send_json(404, {"error": f"unknown api: {method} {path}"})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except ModelLossError as e:
            # 护栏拒绝写入：说明这次请求会丢掉预期之外的模型
            self._send_json(409, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def _serve_ui(self) -> None:
        path = urlparse(self.path).path
        target = (UI_DIR / ("index.html" if path == "/" else unquote(path).lstrip("/"))).resolve()
        if not target.is_relative_to(UI_DIR.resolve()):
            self.send_error(403)
            return
        if not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlparse(self.path).path.startswith("/api/"):
            self._handle_api("GET")
        else:
            self._serve_ui()

    def do_POST(self):
        self._handle_api("POST")

    def do_PUT(self):
        self._handle_api("PUT")

    def do_DELETE(self):
        self._handle_api("DELETE")


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Agent Hub 管理工作台")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--open", action="store_true", default=True)
    parser.add_argument("--no-open", dest="open", action="store_false")
    args = parser.parse_args()
    if not UI_DIR.exists():
        print(f"缺少界面目录: {UI_DIR}", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Agent Hub 工作台: {url}\nCtrl+C 退出")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
