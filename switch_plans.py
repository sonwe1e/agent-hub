# -*- coding: utf-8 -*-
"""切换方案（switchPlans）：一次选择，明确应用到多个 Agent。

设计边界
--------
本模块**只做数据变换**：把一份「切换方案」编译成现有的 ``modelSync`` 结构，
并给出每个目标会发生的实际变化与兼容性问题。它不写文件、不读磁盘、不调用客户端；
落盘、预览、事务与回滚全部复用现有实现（``server._save_config`` /
``sync_transaction``）。因此方案本身不引入第二套同步引擎。

为什么需要新结构（而不是把方案塞进 modelSync）
----------------------------------------------
``modelSync`` 是**当前生效的选择**（"现在用哪个模型"），一份。用户还需要
**多份可命名、可复用、可失效检查的意图**（"工作模式 / 省钱模式"），并且要能
表达三件 modelSync 表达不了的事：

1. 本次切换**参与哪些 Agent**（modelSync 里没有"范围"这个概念）；
2. 档位与推理强度在"本次不发这道指令"时的语义（modelSync 只有"有值/无值"）；
3. 方案被保存后，其引用的供应商 / 模型可能被删掉或改名，需要**显式失效**
   而不是悄悄换成别的模型。

所以方案存在 ``switchPlans`` 里，应用时**编译**成 modelSync。适配器、
预览、事务、备份、恢复的代码路径一个都不变。

字段语义（全部显式，无隐式覆盖）
--------------------------------
``base``
    本次要用的主模型（供应商 + 模型 + 可选推理强度）。``null`` 表示本方案
    不设定全局主模型。

``agents.<agent>``
    ``null``      跟随 ``base``（"跟随本次选择"）；
    对象          该 Agent 单独指定：``policy`` 取 ``fixed`` | ``follow`` | ``keep``。

``policy``／档位
    ``fixed``   写入指定值；
    ``follow``  跟随 ``base``；
    ``keep``    本次不发指令（保留客户端当前值；不清除、不覆盖）。

``reasoningEffort``
    取值词表按目标适配器区分：Codex 用 ``none/minimal/.../ultra``（没有 ``off``），
    其余用 ``off/minimal/.../ultra``。把某个 Agent 不认识的强度当成功写入是
    本模块明确拒绝的行为：编译期就会报 ``effort-vocabulary``。
    ``null`` + ``policy: fixed`` 表示"明确移除 Hub 的推理强度覆盖"，
    与 ``policy: keep``（本次不动）是两件事，界面必须分开显示。

``tiers``（仅 Claude Code）
    ``{policy: explicit, provider, model, reasoningEffort?}`` 写入该档位；
    ``{policy: clear}`` 清除该档位；缺省 = 本次不动该档位。
"""
from __future__ import annotations

import copy
import re
from typing import Any

# 复用 hub.py 的连接解析与档位解析：这里不复制第二套实现。
from hub import (AGENT_CONNECTION_TARGETS, CODEX_REASONING_EFFORTS, REASONING_EFFORTS,
                 agent_connection, agent_connection_ready, agent_model_tiers, canonical_target)

# ---------------------------------------------------------------------------
# 适配范围
# ---------------------------------------------------------------------------

#: 本阶段接入的 Agent（顺序 = 界面与状态展示顺序）。
PLAN_TARGETS: tuple[str, ...] = ("claude", "codex", "pi", "dsh", "opencode")

TARGET_LABELS = {"claude": "Claude Code", "codex": "Codex", "pi": "pi",
                 "dsh": "dsh（含桌面版）", "zcode": "ZCode",
                 "opencode": "OpenCode"}

EFFORT_BY_TARGET: dict[str, tuple[str, ...]] = {
    "codex": CODEX_REASONING_EFFORTS,
    "claude": REASONING_EFFORTS,
    "pi": REASONING_EFFORTS,
    "dsh": REASONING_EFFORTS,
    "zcode": REASONING_EFFORTS,
    "opencode": REASONING_EFFORTS,
}

#: 该 Agent 能否表达推理强度，落到原生配置的哪个字段，改完要不要客户端动手。
#: ``support=False`` 的目标会把强度标为"该客户端没有这个参数"，
#: 绝不显示成"已成功应用"。
EFFORT_TRAITS: dict[str, dict[str, Any]] = {
    "claude": {"support": "applied", "native": "~/.claude/settings.json 的 env.ANTHROPIC_*",
               "note": "Claude Code 的强度由模型与 /config 决定，Hub 写的是模型档位"},
    "codex": {"support": "applied", "native": "~/.codex/config.toml 的 model_reasoning_effort",
              "note": "Codex 第三方的强度按官方字段写入"},
    # pi 的强度落到 settings.json 的 defaultThinkingLevel（旧版本可能忽略该键）
    "pi": {"support": "applied", "native": "~/.pi/agent/settings.json 的 defaultThinkingLevel",
           "note": "pi 写入 defaultThinkingLevel；旧版本或项目级设置可能覆盖它"},
    "dsh": {"support": "applied", "native": "~/.dsh 三层配置里的模型选择与子代理可选模型",
            "note": "dsh 的强度随模型选择一起写入"},
    "zcode": {"support": "applied", "native": "~/.zcode/v2/provider_config.json 的 options.reasoningLevel",
              "note": "ZCode 第三方模式下写入 reasoningLevel"},
    "opencode": {"support": "unsupported", "native": "~/.config/opencode/opencode.json 的 model",
                 "note": "Hub 只写入 OpenCode 的模型选择，不设置推理强度"},
}

#: 写入后需要用户自己再做一步的客户端。
CLIENT_ACTION: dict[str, str] = {
    "claude": "写入的是 settings.json；已在运行的 Claude Code 会话要重启后才读取新模型。",
    "codex": "写入的是 config.toml；正在运行的 Codex 会话与项目级配置可能覆盖它，需要重启确认。",
    "pi": "写入后需重启 pi；旧版本或项目级 enabledModels / --models 可能覆盖全局设置。",
    "dsh": "写入的是 dsh 配置文件；正在运行的 dsh 会话要重启后才会用新模型。",
    "zcode": "第三方模式下写入默认模型选择；正在运行的 ZCode 会话与账号官方线路需要重启确认。",
    "opencode": "写入后请重新加载或重启 OpenCode 服务，再确认模型列表。",
}

#: 官方模式门控：这些客户端处于官方模式时，方案只更新供应商目录，不改运行中的模型指针。
MODE_GATED: dict[str, dict[str, str]] = {
    "codex": {"mode": "official",
              "text": "Codex 当前是官方线路：方案不会改写 model_provider / model，"
                      "需在平台同步里切到第三方线路。"},
    "zcode": {"mode": "official",
              "text": "ZCode 当前是官方模式：方案只维护供应商目录，不改 defaultModelSelection，"
                      "需在平台同步里切到第三方模式。"},
}

TIER_STATES = ("explicit", "keep", "clear")
CLAUDE_TIERS: tuple[str, ...] = ("opus", "sonnet", "haiku", "subagent")

#: modelSync.default 的"清除"哨兵：空 provider/model 触发 _normalize_modelsync 删除该键。
DEFAULT_CLEAR = {"provider": "", "model": ""}

_PID = re.compile(r"^[A-Za-z0-9_-]+$")


class PlanError(ValueError):
    """方案结构非法，或编译期发现目标不兼容。绝不静默降级。"""

    def __init__(self, message: str, issues: list[dict] | None = None) -> None:
        super().__init__(message)
        self.issues = issues or []


def _issue(code: str, message: str, *, target: str | None = None, severity: str = "error",
           fix: str = "") -> dict:
    out = {"code": code, "severity": severity, "message": message}
    if target:
        out["target"] = target
    if fix:
        out["fix"] = fix
    return out


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def normalize_plan(raw: Any) -> dict:
    """校验并归一化一份方案；结构非法时抛 PlanError（消息带字段路径）。"""
    if not isinstance(raw, dict):
        raise PlanError("方案必须是对象")
    unknown = sorted(set(raw) - {"id", "name", "description", "targets", "base", "agents",
                                 "fallback", "updatedAt"})
    if unknown:
        raise PlanError(f"方案包含未知字段: {', '.join(unknown)}")

    pid = raw.get("id")
    if pid is not None and (not isinstance(pid, str) or not pid.strip()):
        raise PlanError("方案 id 必须是非空字符串")
    pid = pid.strip() if isinstance(pid, str) else ""
    if pid and not _PID.match(pid):
        raise PlanError("方案 id 只能包含字母、数字、下划线和连字符")

    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise PlanError("方案 name 必须是字符串")
    name = (name or "").strip()
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise PlanError("方案 description 必须是字符串")

    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise PlanError("方案 targets 必须是非空数组（本次切换参与哪些 Agent）")
    targets: list[str] = []
    for item in targets_raw:
        if not isinstance(item, str):
            raise PlanError("方案 targets 里的目标必须是字符串")
        canonical = canonical_target(item)
        if canonical not in PLAN_TARGETS:
            raise PlanError(f"不支持的方案目标: {item}（只支持 {', '.join(PLAN_TARGETS)}）")
        if canonical not in targets:
            targets.append(canonical)

    base_raw = raw.get("base")
    base = None
    if base_raw is not None:
        if not isinstance(base_raw, dict):
            raise PlanError("方案 base 必须是对象或 null")
        unknown = sorted(set(base_raw) - {"provider", "model", "reasoningEffort"})
        if unknown:
            raise PlanError(f"方案 base 包含未知字段: {', '.join(unknown)}")
        provider = base_raw.get("provider")
        model = base_raw.get("model")
        if not isinstance(provider, str) or not provider.strip():
            raise PlanError("方案 base.provider 必须是非空字符串")
        if not isinstance(model, str) or not model.strip():
            raise PlanError("方案 base.model 必须是非空字符串")
        effort = base_raw.get("reasoningEffort")
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            raise PlanError("方案 base.reasoningEffort 必须是字符串或 null")
        base = {"provider": provider.strip(), "model": model.strip(),
                "reasoningEffort": effort.strip() if isinstance(effort, str) else None}

    agents_raw = raw.get("agents") or {}
    if not isinstance(agents_raw, dict):
        raise PlanError("方案 agents 必须是对象")
    agents: dict[str, dict] = {}
    for key, value in agents_raw.items():
        canonical = canonical_target(str(key))
        if canonical not in PLAN_TARGETS:
            raise PlanError(f"方案 agents 包含不支持的目标: {key}")
        if canonical not in targets:
            raise PlanError(f"方案 agents.{key} 不在 targets 里：要么加入本次范围，要么删掉这条设置")
        if value is None:
            agents[canonical] = {"policy": "follow"}
            continue
        if not isinstance(value, dict):
            raise PlanError(f"方案 agents.{key} 必须是对象或 null")
        agents[canonical] = _normalize_agent(canonical, value, f"agents.{key}")

    fallback = _normalize_fallback(raw.get("fallback"), targets)
    updated = raw.get("updatedAt")
    if updated is not None and not isinstance(updated, str):
        raise PlanError("方案 updatedAt 必须是字符串")
    return {"id": pid, "name": name, "description": (description or "").strip(),
            "targets": targets, "base": base, "agents": agents, "fallback": fallback,
            "updatedAt": updated}


def _normalize_agent(target: str, value: dict, where: str) -> dict:
    allowed = {"policy", "provider", "model", "reasoningEffort", "allowUnknownModel", "tiers"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlanError(f"方案 {where} 包含未知字段: {', '.join(unknown)}")
    policy = value.get("policy") or "fixed"
    if policy not in ("fixed", "follow", "keep"):
        raise PlanError(f"方案 {where}.policy 只能是 fixed / follow / keep")
    out: dict[str, Any] = {"policy": policy}
    if policy == "fixed":
        provider = value.get("provider")
        model = value.get("model")
        if not isinstance(provider, str) or not provider.strip():
            raise PlanError(f"方案 {where}.provider 必须是非空字符串（policy=fixed）")
        if not isinstance(model, str) or not model.strip():
            raise PlanError(f"方案 {where}.model 必须是非空字符串（policy=fixed）")
        out["provider"] = provider.strip()
        out["model"] = model.strip()
    elif any(value.get(k) is not None for k in ("provider", "model")):
        raise PlanError(f"方案 {where} policy={policy} 时不能同时指定 provider / model")
    effort = value.get("reasoningEffort")
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise PlanError(f"方案 {where}.reasoningEffort 必须是字符串或 null")
    out["reasoningEffort"] = effort.strip() if isinstance(effort, str) and effort.strip() else None
    allow = value.get("allowUnknownModel", False)
    if not isinstance(allow, bool):
        raise PlanError(f"方案 {where}.allowUnknownModel 必须是布尔值")
    out["allowUnknownModel"] = allow
    if "tiers" in value:
        if target != "claude":
            raise PlanError(f"方案 {where}.tiers 只有 Claude Code 有档位")
        tiers = value["tiers"]
        if not isinstance(tiers, dict):
            raise PlanError(f"方案 {where}.tiers 必须是对象")
        unknown_tiers = sorted(set(tiers) - set(CLAUDE_TIERS))
        if unknown_tiers:
            raise PlanError(f"方案 {where}.tiers 包含未知档位: {', '.join(unknown_tiers)}")
        normalized: dict[str, dict] = {}
        for tier, item in tiers.items():
            normalized[tier] = _normalize_tier(item, f"{where}.tiers.{tier}")
        out["tiers"] = normalized
    return out


def _normalize_tier(item: Any, where: str) -> dict:
    if not isinstance(item, dict):
        raise PlanError(f"方案 {where} 必须是对象")
    unknown = sorted(set(item) - {"policy", "provider", "model", "reasoningEffort"})
    if unknown:
        raise PlanError(f"方案 {where} 包含未知字段: {', '.join(unknown)}")
    policy = item.get("policy") or "explicit"
    if policy not in TIER_STATES:
        raise PlanError(f"方案 {where}.policy 只能是 explicit / keep / clear")
    out: dict[str, Any] = {"policy": policy}
    if policy == "explicit":
        provider = item.get("provider")
        model = item.get("model")
        if not isinstance(provider, str) or not provider.strip():
            raise PlanError(f"方案 {where}.provider 必须是非空字符串（policy=explicit）")
        if not isinstance(model, str) or not model.strip():
            raise PlanError(f"方案 {where}.model 必须是非空字符串（policy=explicit）")
        out["provider"] = provider.strip()
        out["model"] = model.strip()
    elif any(item.get(k) is not None for k in ("provider", "model")):
        raise PlanError(f"方案 {where} policy={policy} 时不能同时指定 provider / model")
    effort = item.get("reasoningEffort")
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise PlanError(f"方案 {where}.reasoningEffort 必须是字符串或 null")
    # 档位的强度词表就是 Claude 的词表（只有 Claude Code 有档位）。
    if isinstance(effort, str) and effort.strip() and effort.strip() not in REASONING_EFFORTS:
        raise PlanError(f"方案 {where}.reasoningEffort 不支持: {effort}"
                        f"（Claude Code 只接受 {'/'.join(REASONING_EFFORTS)}）")
    out["reasoningEffort"] = effort.strip() if isinstance(effort, str) and effort.strip() else None
    return out


def _normalize_fallback(raw: Any, targets: list[str]) -> dict:
    """备用线路白名单：有序、有约束条件；不参与自动执行，只产出诊断与建议。"""
    if raw is None:
        return {"routes": [], "require": {}, "allowCost": {}}
    if not isinstance(raw, dict):
        raise PlanError("方案 fallback 必须是对象")
    unknown = sorted(set(raw) - {"routes", "require", "allowCost"})
    if unknown:
        raise PlanError(f"方案 fallback 包含未知字段: {', '.join(unknown)}")
    routes_raw = raw.get("routes") or []
    if not isinstance(routes_raw, list):
        raise PlanError("方案 fallback.routes 必须是数组")
    routes: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(routes_raw):
        where = f"fallback.routes[{index}]"
        if not isinstance(item, dict):
            raise PlanError(f"方案 {where} 必须是对象")
        unknown = sorted(set(item) - {"provider", "model", "reasoningEffort", "label", "targets"})
        if unknown:
            raise PlanError(f"方案 {where} 包含未知字段: {', '.join(unknown)}")
        provider = item.get("provider")
        model = item.get("model")
        if not isinstance(provider, str) or not provider.strip():
            raise PlanError(f"方案 {where}.provider 必须是非空字符串")
        if not isinstance(model, str) or not model.strip():
            raise PlanError(f"方案 {where}.model 必须是非空字符串")
        effort = item.get("reasoningEffort")
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            raise PlanError(f"方案 {where}.reasoningEffort 必须是字符串或 null")
        route_targets = item.get("targets") or list(targets)
        if not isinstance(route_targets, list) or not route_targets:
            raise PlanError(f"方案 {where}.targets 必须是非空数组")
        cleaned_targets: list[str] = []
        for name in route_targets:
            if not isinstance(name, str):
                raise PlanError(f"方案 {where}.targets 里的目标必须是字符串")
            canonical = canonical_target(name)
            if canonical not in PLAN_TARGETS:
                raise PlanError(f"方案 {where}.targets 包含不支持的目标: {name}")
            if canonical not in cleaned_targets:
                cleaned_targets.append(canonical)
        key = (provider.strip(), model.strip())
        if key in seen:
            raise PlanError(f"方案 {where} 与前面的线路重复: {key[0]} / {key[1]}")
        seen.add(key)
        routes.append({"provider": key[0], "model": key[1],
                       "reasoningEffort": effort.strip() if isinstance(effort, str) and effort.strip() else None,
                       "label": str(item.get("label") or "").strip(),
                       "targets": cleaned_targets})
    require = raw.get("require") or {}
    if not isinstance(require, dict):
        raise PlanError("方案 fallback.require 必须是对象")
    unknown = sorted(set(require) - {"minContextWindow", "input", "reasoning"})
    if unknown:
        raise PlanError(f"方案 fallback.require 包含未知字段: {', '.join(unknown)}")
    min_context = require.get("minContextWindow")
    if min_context is not None and (not isinstance(min_context, int) or isinstance(min_context, bool)
                                    or min_context <= 0):
        raise PlanError("方案 fallback.require.minContextWindow 必须是正整数或 null")
    inputs = require.get("input") or []
    if not isinstance(inputs, list) or any(not isinstance(x, str) for x in inputs):
        raise PlanError("方案 fallback.require.input 必须是字符串数组")
    reasoning = require.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, bool):
        raise PlanError("方案 fallback.require.reasoning 必须是布尔值或 null")
    allow = raw.get("allowCost") or {}
    if not isinstance(allow, dict):
        raise PlanError("方案 fallback.allowCost 必须是对象")
    unknown = sorted(set(allow) - {"inputPerMillion", "outputPerMillion"})
    if unknown:
        raise PlanError(f"方案 fallback.allowCost 包含未知字段: {', '.join(unknown)}")
    cleaned_cost: dict[str, float] = {}
    for key in ("inputPerMillion", "outputPerMillion"):
        value = allow.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise PlanError(f"方案 fallback.allowCost.{key} 必须是非负数或 null")
        cleaned_cost[key] = float(value)
    return {"routes": routes,
            "require": {"minContextWindow": min_context,
                        "input": [x.strip() for x in inputs if x.strip()],
                        "reasoning": reasoning},
            "allowCost": cleaned_cost}


# ---------------------------------------------------------------------------
# 连接与模型可用性（同一模型名不代表每条连接都有权限）
# ---------------------------------------------------------------------------


def model_entry(cfg: dict, target: str, provider: str, model: str) -> dict | None:
    """该目标连接上声明的模型条目；找不到返回 None。"""
    providers = (cfg or {}).get("providers") or {}
    entry = providers.get(provider)
    if not isinstance(entry, dict):
        return None
    conn = agent_connection(entry, canonical_target(target))
    for item in conn["models"]:
        if isinstance(item, dict) and item.get("id") == model:
            return item
    return None


def _provider(cfg: dict, pid: str) -> dict | None:
    entry = ((cfg or {}).get("providers") or {}).get(pid)
    return entry if isinstance(entry, dict) else None


def connection_snapshot(cfg: dict, target: str) -> dict:
    """该 Agent 每条就绪连接的模型集合（界面按连接过滤模型用，不含密钥）。"""
    canonical = canonical_target(target)
    out: dict[str, dict] = {}
    for pid, entry in (((cfg or {}).get("providers")) or {}).items():
        if not isinstance(entry, dict):
            continue
        conn = agent_connection(entry, canonical)
        out[pid] = {
            "ready": agent_connection_ready(entry, canonical),
            "enabled": entry.get("enabled", True) is not False,
            "api": conn["api"],
            "baseUrl": conn["baseUrl"],
            "hasKey": bool(conn["apiKey"].strip() or conn["apiKeyEnv"].strip()),
            "models": [m["id"] for m in conn["models"]
                       if isinstance(m, dict) and isinstance(m.get("id"), str)],
        }
    return out


def _ref_state(cfg: dict, provider: str, model: str, target: str | None) -> str:
    """一条引用当前的状态词。"""
    entry = _provider(cfg, provider)
    if entry is None:
        return "provider-missing"
    if entry.get("enabled", True) is False:
        return "provider-disabled"
    if target is not None:
        if not agent_connection_ready(entry, target):
            return "connection-not-ready"
        return "ok" if model_entry(cfg, target, provider, model) is not None else "model-missing"
    # 没有单一目标连接的引用（全局主模型）：任一目标连接可用即视为有效。
    for name in AGENT_CONNECTION_TARGETS:
        if agent_connection_ready(entry, name) and model_entry(cfg, name, provider, model):
            return "ok"
    return "model-missing"


def reference_states(cfg: dict, plan: dict) -> dict:
    """方案的引用是否还有效（供应商被删 / 未启用 / 连接不就绪 / 模型不在该连接）。

    返回 ``{"refs": [...], "stale": bool}``。失效引用**只是标记**，
    绝不自动改写成别的模型——这正是"保存下来的方案必须能显式失效"的要求。

    ``base`` 没有指定单一目标连接，所以按"该方案参与的 Agent 里有没有一个
    能真正用上它"来判断。只看供应商的并集会把失效方案显示成可用。
    """
    refs: list[dict] = []

    def add(where: str, provider: str, model: str, target: str | None) -> None:
        kind = _ref_state(cfg, provider, model, target)
        refs.append({"where": where, "target": target, "provider": provider, "model": model,
                     "state": kind, "valid": kind == "ok"})

    base = plan.get("base")
    if base:
        entry = _provider(cfg, base["provider"])
        if entry is None or entry.get("enabled", True) is False:
            add("base", base["provider"], base["model"], None)
        else:
            # base 的有效性取决于参与本次范围的 Agent：任何一个连接能用即有效。
            usable = [t for t in plan["targets"]
                      if (plan.get("agents", {}).get(t) or {}).get("policy") != "keep"
                      and _ref_state(cfg, base["provider"], base["model"], t) == "ok"]
            if usable:
                add("base", base["provider"], base["model"], usable[0])
            else:
                refs.append({"where": "base", "target": None, "provider": base["provider"],
                             "model": base["model"],
                             "state": _base_state(cfg, base, plan),
                             "valid": False, "targets": list(plan["targets"])})
    for target, spec in (plan.get("agents") or {}).items():
        if spec.get("policy") == "fixed":
            add(f"agents.{target}", spec["provider"], spec["model"], target)
        for tier, item in (spec.get("tiers") or {}).items():
            if item.get("policy") == "explicit":
                add(f"agents.{target}.tiers.{tier}", item["provider"], item["model"], "claude")
    for index, route in enumerate((plan.get("fallback") or {}).get("routes") or []):
        for target in route["targets"]:
            add(f"fallback.routes[{index}]", route["provider"], route["model"], target)
    return {"refs": refs, "stale": any(not r["valid"] for r in refs)}


def _base_state(cfg: dict, base: dict, plan: dict) -> str:
    """base 在所有参与目标上都不可用时的原因词（取第一个可解释的）。"""
    states = [_ref_state(cfg, base["provider"], base["model"], t)
              for t in plan["targets"]
              if (plan.get("agents", {}).get(t) or {}).get("policy") != "keep"]
    for wanted in ("provider-missing", "provider-disabled", "connection-not-ready", "model-missing"):
        if wanted in states:
            return wanted
    return "model-missing"


# ---------------------------------------------------------------------------
# 分析：写入前就该看到的兼容性、能力与费用问题
# ---------------------------------------------------------------------------


def _constraint_report(cfg: dict, target: str, provider: str, model: str,
                       require: dict, allow_cost: dict) -> tuple[list[dict], dict]:
    """按 fallback 的能力 / 费用约束检查一条线路；缺失信息如实标 unknown。"""
    issues: list[dict] = []
    entry = model_entry(cfg, target, provider, model)
    facts: dict[str, Any] = {"declared": entry is not None}
    if entry is None:
        facts["contextWindow"] = None
        facts["input"] = None
        facts["reasoning"] = None
        facts["cost"] = None
    else:
        facts["contextWindow"] = entry.get("contextWindow") if isinstance(entry.get("contextWindow"), int) else None
        inputs = entry.get("input")
        facts["input"] = [str(x) for x in inputs] if isinstance(inputs, list) else None
        facts["reasoning"] = entry.get("reasoning") if isinstance(entry.get("reasoning"), bool) else None
        cost = entry.get("cost")
        facts["cost"] = cost if isinstance(cost, dict) else None

    min_context = require.get("minContextWindow")
    if min_context is not None:
        if facts["contextWindow"] is None:
            issues.append(_issue("capability-unknown",
                                 f"{provider} / {model} 没有声明上下文窗口，无法确认满足 ≥{min_context}",
                                 target=target, severity="warning",
                                 fix="在模型库里补 contextWindow，或放宽该约束"))
        elif facts["contextWindow"] < min_context:
            issues.append(_issue("capability-below",
                                 f"{provider} / {model} 声明上下文窗口 {facts['contextWindow']} < {min_context}",
                                 target=target))
    for wanted in require.get("input") or []:
        if facts["input"] is None:
            issues.append(_issue("capability-unknown",
                                 f"{provider} / {model} 没有声明输入模态，无法确认支持 {wanted}",
                                 target=target, severity="warning",
                                 fix="在模型库里补 input，或放宽该约束"))
        elif wanted not in facts["input"]:
            issues.append(_issue("capability-below",
                                 f"{provider} / {model} 声明的输入模态里没有 {wanted}",
                                 target=target))
    if require.get("reasoning") is True:
        if facts["reasoning"] is None:
            issues.append(_issue("capability-unknown",
                                 f"{provider} / {model} 没有声明是否支持推理，无法确认",
                                 target=target, severity="warning"))
        elif not facts["reasoning"]:
            issues.append(_issue("capability-below",
                                 f"{provider} / {model} 声明不支持推理",
                                 target=target))
    cost = facts["cost"] or {}
    for key, label in (("inputPerMillion", "输入"), ("outputPerMillion", "输出")):
        limit = allow_cost.get(key)
        if limit is None:
            continue
        declared = cost.get(key) if isinstance(cost.get(key), (int, float)) else None
        if declared is None:
            issues.append(_issue("cost-unknown",
                                 f"{provider} / {model} 没有声明{label}单价，无法确认不超过 {limit}/百万 tokens",
                                 target=target, severity="warning",
                                 fix="在模型库里补 cost，或去掉该费用上限"))
        elif float(declared) > float(limit):
            issues.append(_issue("cost-exceeds",
                                 f"{provider} / {model} 声明的{label}单价 {declared} 超过上限 {limit}",
                                 target=target))
    return issues, facts


def _effort_view(cfg: dict, target: str, before: dict | None, after: dict | None) -> dict:
    """推理强度在某目标上的实际处置；不支持的参数绝不报成功。"""
    traits = EFFORT_TRAITS[target]
    requested = (after or {}).get("reasoningEffort")
    if traits["support"] == "unsupported":
        if requested:
            return {"support": "unsupported", "requested": requested,
                    "native": traits["native"], "note": traits["note"]}
        return {"support": "unsupported", "requested": None, "native": traits["native"],
                "note": traits["note"]}
    if requested is None:
        if before and before.get("reasoningEffort"):
            return {"support": "keep", "requested": None, "current": before["reasoningEffort"],
                    "native": traits["native"], "note": "本次不发强度指令，保留客户端当前值"}
        return {"support": "applied", "requested": None, "native": traits["native"],
                "note": "本次方案没有指定强度"}
    if before and before.get("reasoningEffort") == requested:
        return {"support": "applied", "requested": requested, "current": requested,
                "unchanged": True, "native": traits["native"], "note": "与当前设置相同"}
    return {"support": "applied", "requested": requested,
            "current": (before or {}).get("reasoningEffort"), "native": traits["native"],
            "note": traits["note"]}


def analyze_plan(cfg: dict, plan: dict, *, targets: list[str] | None = None,
                 mode_of=None) -> dict:
    """逐目标说明本次会写什么、跳过什么、缺什么；写入前就该拿到这份结果。

    ``mode_of(name)`` 返回该客户端当前的 official / third-party 模式，
    用于把「官方模式会跳过」显式告诉用户。
    """
    scope = [canonical_target(t) for t in (targets or plan["targets"])]
    base = plan.get("base")
    # 归一化后的方案一定带 agents；直接调用的调用方可能省掉它。
    agent_specs = plan.get("agents") or {}
    # 编译是唯一的判定路径：分析与实际写入共用同一份错误码，界面不另算一套。
    patched, compiled, issues, blocked = compile_plan(cfg, plan, targets=scope)
    before_tiers = {t: agent_model_tiers(cfg, t) for t in scope}
    after_tiers = {t: (agent_model_tiers(patched, t) if patched is not None else {}) for t in scope}

    details: list[dict] = []
    for target in scope:
        spec = agent_specs.get(target) or {}
        policy = spec.get("policy") or ("follow" if base else "keep")
        if spec.get("policy") == "fixed":
            provider, model = spec["provider"], spec["model"]
        elif base:
            provider, model = base["provider"], base["model"]
        else:
            provider, model = "", ""
        item: dict[str, Any] = {
            "target": target, "label": TARGET_LABELS.get(target, target), "policy": policy,
            "provider": provider, "model": model, "status": "pending", "reason": "",
            "before": before_tiers[target], "after": after_tiers[target],
            "effort": _effort_view(cfg, target, before_tiers[target].get(_main_tier(target)),
                                   after_tiers[target].get(_main_tier(target))),
            "needsClientAction": CLIENT_ACTION.get(target, ""),
        }
        item["modelKnown"] = model_entry(cfg, target, provider, model) is not None if provider else False

        def mark(status: str, reason: str = "") -> None:
            """状态只能从更差的方向被改写：编译期判定的 error 不会被后一步刷成 ok。"""
            if item["status"] == "error" and status != "error":
                return
            item["status"] = status
            if reason:
                item["reason"] = reason

        if policy == "keep":
            item["status"] = "skipped"
            item["reason"] = "方案里这一项是「保留客户端当前值」，本次不写这个 Agent"
            details.append(item)
            continue

        if target in blocked:
            reason = next((i["message"] for i in issues
                           if i.get("target") == target and i["severity"] == "error"), "")
            mark("error", reason or "该目标在写入前检查里不兼容")
        elif patched is not None:
            # 编译通过 = 连接就绪且模型在该连接上；手填放行时给明确提示。
            if not item["modelKnown"] and spec.get("allowUnknownModel"):
                mark("warning", f"{model} 不在 {provider} 该连接的模型库里，按手填值写入")
                issues.append(_issue("model-unknown", item["reason"], target=target,
                                     severity="warning"))
            else:
                mark("ok")
        else:
            # 整组不写：本目标自身没问题，是同一方案里别的目标不兼容。
            others = [t for t in blocked if t != target]
            mark("error", f"整组不写：{'、'.join(TARGET_LABELS.get(t, t) for t in others)} 不兼容。"
                          if others else "本方案没有可写入的模型选择")

        # 强度词表：把某个 Agent 不认识的强度当成功写入是禁止的
        effort = item["effort"].get("requested")
        if effort and effort not in EFFORT_BY_TARGET[target]:
            mark("error", f"{TARGET_LABELS.get(target, target)} 不接受推理强度 {effort}"
                          f"（可用 {'/'.join(EFFORT_BY_TARGET[target])}）")
        elif effort and EFFORT_TRAITS[target]["support"] == "unsupported":
            item["effort"]["support"] = "unsupported"
            issues.append(_issue("effort-unsupported", item["effort"]["note"], target=target,
                                 severity="warning",
                                 fix="把该 Agent 移出本次范围，或接受它只换模型"))

        # 档位：只列显式指定或清除的档位；档位不兼容的问题由 compile_plan 统一报出。
        tier_changes: list[dict] = []
        for tier, tier_spec in (spec.get("tiers") or {}).items():
            if tier_spec.get("policy") == "explicit":
                tier_changes.append({"tier": tier, "action": "set",
                                     "provider": tier_spec["provider"], "model": tier_spec["model"]})
            elif tier_spec.get("policy") == "clear":
                tier_changes.append({"tier": tier, "action": "clear"})
        if tier_changes:
            item["tiers"] = tier_changes

        # 官方模式门控：写是安全的，但运行中的客户端不会换线路
        if mode_of is not None and target in MODE_GATED:
            mode = mode_of(target)
            item["mode"] = mode
            if mode != "third-party":
                item["modeGated"] = True
                if item["status"] in ("ok", "warning"):
                    mark("skipped", MODE_GATED[target]["text"])
                issues.append(_issue("mode-gated", MODE_GATED[target]["text"], target=target,
                                     severity="warning",
                                     fix="在「平台同步」里把该客户端切到第三方模式再应用"))
        details.append(item)

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    # 「会写入」包含手填放行（warning）：它照样写，只是明确标出模型不在该连接模型库里。
    ready = [d["target"] for d in details if d["status"] in ("ok", "warning")]
    blocked_targets = [d["target"] for d in details if d["status"] == "error"]
    skipped = [d["target"] for d in details if d["status"] == "skipped"]
    writable = patched is not None and not blocked and bool(ready)
    return {
        "plan": plan, "targets": scope, "details": details, "issues": issues,
        "errors": errors, "warnings": warnings,
        "summary": {"ready": ready, "blocked": blocked_targets, "skipped": skipped,
                    "errorCount": len(errors), "warningCount": len(warnings)},
        "writable": writable,
        "compiledModelSync": None if not writable else model_sync_patch_for_write(cfg, compiled),
        "clientActions": {d["target"]: d["needsClientAction"] for d in details
                          if d["needsClientAction"] and d["status"] != "skipped"},
        "disclaimer": "「可写入」只表示 Hub 会成功写出原生配置，不代表正在运行的会话已经换模。",
    }


def _main_tier(target: str) -> str:
    return "model" if canonical_target(target) == "claude" else "agent"


# ---------------------------------------------------------------------------
# 编译：方案 -> modelSync（唯一写入路径）
# ---------------------------------------------------------------------------


def compile_plan(cfg: dict, plan: dict, *, targets: list[str] | None = None) -> tuple[dict | None, dict | None, list[dict], set[str]]:
    """把方案编译成 modelSync 补丁，并返回应用补丁后的配置（不改原对象）。

    返回 ``(patched_cfg, patch, issues, blocked)``。``blocked`` 是**编译期就
    判定不能写入**的目标集合；只要它非空，调用方必须整组不写。只有全组
    目标都通过时才返回 patched/patch，因此不存在"一半目标被静默跳过"。

    这里是唯一的写入路径：分析结果与错误码都以本函数为准，界面不另算一套。
    """
    scope = [canonical_target(t) for t in (targets if targets is not None else plan["targets"])]
    base = plan.get("base")
    agent_specs = plan.get("agents") or {}
    issues: list[dict] = []
    blocked: set[str] = set()
    written_scope = [t for t in scope
                     if (agent_specs.get(t) or {}).get("policy") != "keep"]
    if not written_scope:
        return None, None, [_issue("nothing-to-write",
                                   "方案里没有任何一个 Agent 会被写入；请至少为一个参与的目标指定模型")], set()
    if base is None and not any(
            (agent_specs.get(t) or {}).get("policy") == "fixed" for t in written_scope):
        return None, None, [_issue("nothing-to-write",
                                   "方案既没有 base 主模型，也没有任何 fixed 目标：没有可写入的模型选择")], set()

    default_patch: dict
    if base is None:
        default_patch = dict(DEFAULT_CLEAR)
    else:
        default_patch = {"provider": base["provider"], "model": base["model"]}
        # base 也要按目标连接核对：供应商可能被删、被停用，或没有这个 Agent 的连接。
        # 只看"供应商还存在"会把失效方案当成可写入，这是同一模型名跨连接最典型的坑。
        base_entry = _provider(cfg, base["provider"])
        for target in written_scope:
            spec = agent_specs.get(target) or {}
            follows_base = spec.get("policy") in (None, "follow")
            if not follows_base:
                continue          # 单独指定了 provider 的目标在自己的分支里校验
            label = TARGET_LABELS.get(target, target)
            if base_entry is None:
                blocked.add(target)
                issues.append(_issue("provider-missing",
                                     f"供应商 {base['provider']} 不存在（可能被删除或改名）",
                                     target=target,
                                     fix="重新选择主模型的供应商，或把该 Agent 移出本次范围"))
            elif base_entry.get("enabled", True) is False:
                blocked.add(target)
                issues.append(_issue("provider-disabled",
                                     f"供应商 {base['provider']} 已停用", target=target))
            elif not agent_connection_ready(base_entry, target):
                blocked.add(target)
                issues.append(_issue("connection-not-ready",
                                     f"{base['provider']} 没有可用的 {label} 连接"
                                     f"（当前协议 {agent_connection(base_entry, target)['api']}）",
                                     target=target,
                                     fix="在供应商配置的「按 Agent 覆盖」里补端点与协议，"
                                         "或为该 Agent 单独指定另一个供应商"))
            elif model_entry(cfg, target, base["provider"], base["model"]) is None \
                    and not spec.get("allowUnknownModel"):
                blocked.add(target)
                issues.append(_issue("model-not-in-connection",
                                     f"{base['provider']} 的 {label} 连接模型库里没有 "
                                     f"{base['model']}", target=target,
                                     fix="先在这条连接上添加/检测该模型，或显式允许手填模型 ID"))
        if base.get("reasoningEffort"):
            effort = base["reasoningEffort"]
            if effort not in REASONING_EFFORTS:
                return None, None, [_issue("effort-vocabulary",
                                           f"base.reasoningEffort 不支持: {effort}")], set()
            for target in written_scope:
                spec = agent_specs.get(target) or {}
                # 只有"本次依赖 base 强度"的目标才会被 base 词表卡住：
                # 单独指定了强度的目标不跟随 base，不该被它拖累。
                follows_base_effort = spec.get("policy") in (None, "follow") \
                    or not spec.get("reasoningEffort")
                if follows_base_effort and effort not in EFFORT_BY_TARGET[target]:
                    blocked.add(target)
                    issues.append(_issue(
                        "effort-vocabulary",
                        f"推理强度 {effort} 在 {TARGET_LABELS.get(target, target)} 上不存在"
                        f"（可用 {'/'.join(EFFORT_BY_TARGET[target])}）",
                        target=target,
                        fix=f"为 {TARGET_LABELS.get(target, target)} 单独选一个它支持的强度，"
                            "或把它移出本次范围"))
            default_patch["reasoningEffort"] = effort

    overrides: dict[str, dict] = {}
    for target in written_scope:
        label = TARGET_LABELS.get(target, target)
        spec = agent_specs.get(target) or {"policy": "follow"}
        policy = spec.get("policy", "follow")
        if policy == "follow":
            if base is None:
                blocked.add(target)
                issues.append(_issue("no-base", f"agents.{target} 是 follow，但方案没有 base", target=target))
                continue
            if target in blocked:
                continue
            overrides[target] = {"provider": base["provider"], "model": base["model"]}
            if base.get("reasoningEffort"):
                if EFFORT_TRAITS[target]["support"] == "unsupported":
                    issues.append(_issue("effort-unsupported", EFFORT_TRAITS[target]["note"],
                                         target=target, severity="warning"))
                else:
                    overrides[target]["reasoningEffort"] = base["reasoningEffort"]
            if _apply_tiers(cfg, target, spec, overrides[target], issues):
                overrides.pop(target, None)
                blocked.add(target)
            continue
        if policy != "fixed":
            blocked.add(target)
            issues.append(_issue("bad-policy", f"agents.{target}.policy 不支持: {policy}", target=target))
            continue
        provider, model = spec["provider"], spec["model"]
        entry = _provider(cfg, provider)
        if entry is None:
            blocked.add(target)
            issues.append(_issue("provider-missing", f"供应商 {provider} 不存在（可能被删除或改名）",
                                 target=target, fix="改选一个仍然存在的供应商，或从本次范围里移除"))
            continue
        if entry.get("enabled", True) is False:
            blocked.add(target)
            issues.append(_issue("provider-disabled", f"供应商 {provider} 已停用", target=target))
            continue
        if not agent_connection_ready(entry, target):
            blocked.add(target)
            issues.append(_issue("connection-not-ready",
                                 f"{provider} 没有可用的 {label} 连接"
                                 f"（当前协议 {agent_connection(entry, target)['api']}）",
                                 target=target,
                                 fix="在供应商配置的「按 Agent 覆盖」里补端点与协议，或改选别的供应商"))
            continue
        if model_entry(cfg, target, provider, model) is None and not spec.get("allowUnknownModel"):
            blocked.add(target)
            issues.append(_issue("model-not-in-connection",
                                 f"{provider} 的 {label} 连接模型库里没有 {model}",
                                 target=target,
                                 fix="先在这条连接上添加/检测该模型，或显式允许手填模型 ID"))
            continue
        item: dict = {"provider": provider, "model": model}
        effort = spec.get("reasoningEffort")
        if effort:
            if effort not in EFFORT_BY_TARGET[target]:
                blocked.add(target)
                issues.append(_issue("effort-vocabulary", f"{label} 不接受推理强度 {effort}"
                                                          f"（可用 {'/'.join(EFFORT_BY_TARGET[target])}）",
                                     target=target))
                continue
            if EFFORT_TRAITS[target]["support"] == "unsupported":
                issues.append(_issue("effort-unsupported", EFFORT_TRAITS[target]["note"],
                                     target=target, severity="warning"))
            else:
                item["reasoningEffort"] = effort
        if _apply_tiers(cfg, target, spec, item, issues):
            blocked.add(target)
            continue
        overrides[target] = item

    if blocked:
        # 有任何一个目标不兼容就整组不写：不做"部分成功"的静默结果。
        return None, None, issues, blocked
    patch = {"default": default_patch, "overrides": overrides,
             "clear": []}
    # A plan's base is a choice for its explicit scope, not a replacement of
    # every other Agent's global default. All written targets have overrides.
    patch["default"] = copy.deepcopy((cfg.get("modelSync") or {}).get("default") or DEFAULT_CLEAR)
    return apply_patch(cfg, patch), patch, issues, set()


def _apply_tiers(cfg: dict, target: str, spec: dict, entry: dict,
                 issues: list[dict]) -> bool:
    """把档位写进该目标的 override；返回 True 表示该目标必须被整组拦下。

    档位与主模型是两条独立的写入指令，所以 follow 的目标也要检查自己的档位。
    """
    tiers_patch: dict[str, dict] = {}
    tier_blocked = False
    for tier, tier_spec in (spec.get("tiers") or {}).items():
        if tier_spec.get("policy") != "explicit":
            continue
        owner = _provider(cfg, tier_spec["provider"])
        if owner is None or owner.get("enabled", True) is False \
                or not agent_connection_ready(owner, "claude"):
            tier_blocked = True
            issues.append(_issue("tier-connection-not-ready",
                                 f"档位 {tier} 的供应商 {tier_spec['provider']} 没有可用的 Claude 连接",
                                 target=target))
            continue
        if model_entry(cfg, "claude", tier_spec["provider"], tier_spec["model"]) is None:
            issues.append(_issue("tier-model-not-in-connection",
                                 f"档位 {tier} 的模型 {tier_spec['model']} 不在 "
                                 f"{tier_spec['provider']} 的 Claude 连接模型库里，按手填值写入",
                                 target=target, severity="warning"))
        tier_entry: dict = {"provider": tier_spec["provider"], "model": tier_spec["model"]}
        if tier_spec.get("reasoningEffort"):
            tier_entry["reasoningEffort"] = tier_spec["reasoningEffort"]
        tiers_patch[tier] = tier_entry
    if tiers_patch and not tier_blocked:
        entry["tiers"] = tiers_patch
    preserved = [tier for tier, value in (spec.get("tiers") or {}).items() if value.get("policy") == "keep"]
    if preserved:
        entry["preserveTiers"] = preserved
    return tier_blocked


def apply_patch(cfg: dict, patch: dict) -> dict:
    """把编译出的补丁应用到一份配置副本上（不改原对象，不读磁盘）。"""
    out = copy.deepcopy(cfg or {})
    block = out.get("modelSync")
    block = block if isinstance(block, dict) else {}
    default = patch.get("default") or {}
    overrides = dict(block.get("overrides") or {})
    merged: dict = {}
    if default.get("provider") and default.get("model"):
        merged["default"] = {k: v for k, v in default.items() if v is not None}
    for target, entry in (patch.get("overrides") or {}).items():
        overrides[target] = dict(entry)
    for target in patch.get("clear") or []:
        overrides.pop(target, None)
    if overrides:
        merged["overrides"] = overrides
    out["modelSync"] = merged
    return out


def model_sync_patch_for_write(cfg: dict, patch: dict) -> dict:
    """把编译补丁落成可直接交给 server._normalize_modelsync 的整块 modelSync。"""
    return apply_patch(cfg, patch)["modelSync"]


# ---------------------------------------------------------------------------
# 备用线路诊断（只给有证据的建议，不自动执行）
# ---------------------------------------------------------------------------


def diagnose_fallback(cfg: dict, plan: dict, *, targets: list[str] | None = None) -> dict:
    """按白名单顺序评估备用线路，给出"现在能用哪条"的证据与建议。

    明确不做的事：不自动切换、不重试、不重放任务、不承诺会话无缝继续。
    """
    scope = [canonical_target(t) for t in (targets or plan["targets"])]
    fallback = plan.get("fallback") or {"routes": [], "require": {}, "allowCost": {}}
    require = fallback.get("require") or {}
    allow_cost = fallback.get("allowCost") or {}
    routes: list[dict] = []
    for index, route in enumerate(fallback.get("routes") or []):
        covered = [t for t in scope if t in route["targets"]]
        if not covered:
            continue
        checks: list[dict] = []
        for target in covered:
            entry = _provider(cfg, route["provider"])
            state, ok = "ok", True
            reason = ""
            if entry is None:
                state, ok, reason = "provider-missing", False, f"供应商 {route['provider']} 不存在"
            elif entry.get("enabled", True) is False:
                state, ok, reason = "provider-disabled", False, f"供应商 {route['provider']} 已停用"
            elif not agent_connection_ready(entry, target):
                state, ok = "connection-not-ready", False
                reason = f"没有可用的 {TARGET_LABELS.get(target, target)} 连接"
            elif model_entry(cfg, target, route["provider"], route["model"]) is None:
                state, ok = "model-not-in-connection", False
                reason = (f"{route['model']} 不在 {route['provider']} 的 "
                          f"{TARGET_LABELS.get(target, target)} 连接模型库里")
            constraint_issues, facts = _constraint_report(
                cfg, target, route["provider"], route["model"], require, allow_cost)
            hard = [i for i in constraint_issues if i["severity"] == "error"]
            if hard:
                ok = False
                state = hard[0]["code"]
                reason = hard[0]["message"]
            effort = route.get("reasoningEffort")
            effort_ok = True
            if effort and effort not in EFFORT_BY_TARGET[target]:
                effort_ok = False
                ok = False
                state = "effort-vocabulary"
                reason = f"{TARGET_LABELS.get(target, target)} 不接受强度 {effort}"
            checks.append({"target": target, "state": state, "usable": ok and effort_ok,
                           "reason": reason, "facts": facts,
                           "constraints": constraint_issues})
        routes.append({"index": index, "provider": route["provider"], "model": route["model"],
                       "reasoningEffort": route.get("reasoningEffort"),
                       "label": route.get("label") or f"{route['provider']} / {route['model']}",
                       "targets": covered, "checks": checks,
                       "usable": all(c["usable"] for c in checks) and bool(checks)})
    recommendation = next((r for r in routes if r["usable"]), None)
    return {
        "plan": plan["id"] or plan["name"],
        "targets": scope,
        "require": require,
        "allowCost": allow_cost,
        "routes": routes,
        "recommended": recommendation,
        "autoSwitch": False,
        "autoSwitchBlockedBy": [
            "Hub 没有各客户端的运行中会话句柄：写文件不等于下一次请求换线路。",
            "Hub 不读取配额与余额，无法在费用耗尽时判定故障。",
            "故障检测需要客户端自身错误反馈；Hub 只看配置文件。",
        ],
        "note": ("白名单与约束只用于给出「现在哪条能用」的证据和建议；"
                 "自动切换尚未实现，不会重试、不会重放任务。"),
        "unknowns": ["每条线路的实时额度与余额", "运行中会话是否已加载新配置"],
    }
