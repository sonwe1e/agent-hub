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


def file_hash(path: Path) -> str:
    if not path.exists():
        return "missing"
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8")
    # 容忍 JSONC 注释
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return json.loads(text)


def save_json(path: Path, data: Any, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 保护: 写 providers.json 前若模型数量大幅减少则先快照
    if path.name == "providers.json" and path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            old_n = sum(len((p or {}).get("models") or []) for p in (old.get("providers") or {}).values())
            new_n = sum(len((p or {}).get("models") or []) for p in (data.get("providers") or {}).values())
            dropped = old_n > 0 and new_n < old_n * 0.5
            if not dropped:
                # 总量没腰斩也要留证：单个供应商被清空同样是事故信号
                for pid, entry in (old.get("providers") or {}).items():
                    before = len((entry or {}).get("models") or [])
                    after = len((((data.get("providers") or {}).get(pid)) or {}).get("models") or [])
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
    entry = (cfg or {}).get("providers", {}).get(pid) or {}
    return {m.get("id") for m in (entry.get("models") or []) if isinstance(m, dict)}


# ---------------------------------------------------------------------------
# 多 agent 模型选择 (providers.json -> modelSync)
# ---------------------------------------------------------------------------

# 每个平台可单独指定的模型档位。pi 没有子代理概念，只有 agent 主模型；
# dsh 的 subagent 档位与 Claude Code 的 opus/sonnet/haiku/subagent 档位
# 各自落到客户端原生配置的对应字段。
AGENT_MODEL_TIERS: dict[str, tuple[str, ...]] = {
    "pi": ("agent",),
    "dsh": ("agent", "subagent"),
    "claude": ("agent", "model", "opus", "sonnet", "haiku", "fable", "subagent"),
    # Codex 只有一个顶层 model；codex-auto-review 之类子代理不在本工具管理范围。
    "codex": ("agent",),
}

REASONING_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def _norm_pid_model(pid: Any, model: Any) -> tuple[str, str] | None:
    if not isinstance(pid, str) or not pid.strip():
        return None
    if not isinstance(model, str) or not model.strip():
        return None
    return pid.strip(), model.strip()


def _cfg_providers(cfg: Any) -> dict:
    providers = (cfg or {}).get("providers")
    return providers if isinstance(providers, dict) else {}


def _legacy_tier(pid: str | None, claude_models: dict, tier: str) -> dict | None:
    """旧字段 provider.claudeModels 里某一档位的 {provider, model}。

    `model` / `agent` 两档都对应 ANTHROPIC_MODEL，`fable` 是历史档位，
    不再作为目标档位，但仍然被适配器接手。
    """
    if not pid:
        return None
    key = "model" if tier == "agent" else tier
    value = claude_models.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return {"provider": pid, "model": value.strip()}


def agent_model_tiers(cfg: Any, target: str) -> dict[str, dict]:
    """解析某平台每个档位实际要用的模型，不做写入。

    取值优先级（从高到低）：
      1. modelSync.overrides.<target>.tiers.<tier> —— 该档位被显式指定；
      2. provider.claudeModels —— 旧 Claude 档位字段，只认活跃供应商自己声明的档位；
      3. modelSync.overrides.<target>.{provider,model} —— 平台级指定；
      4. provider.agentModel —— 供应商声明的 agent 默认模型；
      5. modelSync.default.{provider,model} —— 全局主模型；
      6. 供应商 models 列表的第一项 —— 兜底。

    第 3~6 条只作用于「主模型」（pi 的 agent、dsh 的 agent、Claude Code 的 model 档）；
    Claude Code 的 opus/sonnet/haiku/fable/subagent 档位不会因为一个全局设置被改写。

    每个档位返回 {"provider","model","reasoningEffort","known","tier"}；
    `known` 表示该模型 ID 是否出现在供应商的模型库里（手填值会是 False）。
    """
    canonical = "dsh" if target == "dsh-desktop" else target
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

    def pick(*candidates: Any) -> dict | None:
        for candidate in candidates:
            if isinstance(candidate, dict):
                parsed = _norm_pid_model(candidate.get("provider"), candidate.get("model"))
                if parsed:
                    effort = candidate.get("reasoningEffort")
                    return {"provider": parsed[0], "model": parsed[1],
                            "reasoningEffort": effort if effort in REASONING_EFFORTS else None}
        return None

    def provider_models(pid: str) -> list[str]:
        entry = providers.get(pid)
        entry = entry if isinstance(entry, dict) else {}
        return [m["id"] for m in (entry.get("models") or [])
                if isinstance(m, dict) and isinstance(m.get("id"), str)]

    def fallback_for(pid: str) -> dict | None:
        entry = providers.get(pid)
        entry = entry if isinstance(entry, dict) else {}
        agent_model = entry.get("agentModel")
        if isinstance(agent_model, str) and agent_model.strip():
            return {"provider": pid, "model": agent_model.strip(), "reasoningEffort": None}
        first = provider_models(pid)
        if first:
            return {"provider": pid, "model": first[0], "reasoningEffort": None}
        return None

    # 谁才是 Claude Code 实际会用的供应商（与 push_claude 同一套判定）：activeProvider
    # 不可用时回退到第一个可用的 Anthropic 供应商；全局/平台级选择指向的那家有
    # Anthropic 端点时，适配器会切过去，所以这里也按它算。
    legacy_pid: str | None = None
    legacy_models: dict = {}
    active_claude_pid: str | None = None
    if canonical == "claude":
        configured_active = ((cfg or {}).get("sync", {}).get("targets", {}).get("claude")
                             or {}).get("activeProvider")

        def anthropic_ready(pid: Any) -> bool:
            entry = providers.get(pid) if isinstance(pid, str) else None
            return bool(isinstance(entry, dict) and entry.get("anthropicBaseUrl")
                        and entry.get("enabled", True))

        fallback_active = next((pid for pid, p in providers.items()
                                if isinstance(p, dict) and p.get("anthropicBaseUrl")
                                and p.get("enabled", True)), None)
        # 与 push_claude 同一套顺序：平台级/全局选择（覆盖）优先，其次平台配置里的
        # activeProvider，都不可用时回退到第一个可用的 Anthropic 供应商。
        if anthropic_ready(override.get("provider")):
            active_claude_pid = override["provider"]
        elif anthropic_ready(default.get("provider")):
            active_claude_pid = default["provider"]
        elif anthropic_ready(configured_active):
            active_claude_pid = configured_active
        else:
            active_claude_pid = fallback_active
        if isinstance(active_claude_pid, str):
            active_entry = providers.get(active_claude_pid)
            if isinstance(active_entry, dict) and isinstance(active_entry.get("claudeModels"), dict):
                legacy_pid, legacy_models = active_claude_pid, active_entry["claudeModels"]

    # 平台级覆盖：overrides.<target> 可带 tiers 子对象（每个档位单独指定）
    override_pid = override.get("provider")
    base = (pick(override)
            or (fallback_for(override_pid) if isinstance(override_pid, str) else None)
            or pick(default)
            or (fallback_for(default["provider"]) if isinstance(default.get("provider"), str) else None))

    out: dict[str, dict] = {}
    for tier in tier_names:
        resolved = None
        if canonical == "claude":
            # 1) 该档位被界面显式指定
            if tier in override_tiers:
                resolved = pick(override_tiers.get(tier))
            if resolved is None and tier == "model":
                # 2) 主模型档位：平台级覆盖 / 全局主模型，必须与「实际会用的 Anthropic
                #    供应商」是同一家（baseUrl 与 apiKey 必须配套，跨供应商套用模型名
                #    只会写出一个对方不认的模型）
                for candidate in (override, default):
                    picked = pick(candidate)
                    if picked and picked["provider"] == active_claude_pid:
                        resolved = picked
                        break
                # 3) 仍然没有就用该供应商自己声明的 claudeModels.model
                if resolved is None:
                    resolved = pick(_legacy_tier(legacy_pid, legacy_models, tier))
            elif resolved is None:
                # 别名档位（opus/sonnet/haiku/fable/subagent）只认活跃供应商自己声明的
                # claudeModels：未声明的档位不做兜底，否则会把 opus/haiku 悄悄指过去，
                # 等于替用户改了 Claude Code 的配置。
                resolved = pick(_legacy_tier(legacy_pid, legacy_models, tier))
        else:
            # pi 的 agent 档、dsh 的 agent/subagent 档都跟随同一个主模型
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

    与 agent_model_tiers 的区别：隐藏 `agent` 这一内部档位（它与 claude 的 `model`
    档同源），并隐藏历史档位 `fable`。适配器仍通过 agent_model_tiers 读到 fable，
    这样旧配置里的 ANTHROPIC_DEFAULT_FABLE_MODEL 该写就写、该收回就收回。
    """
    tiers = agent_model_tiers(cfg, target)
    canonical = "dsh" if target == "dsh-desktop" else target
    if canonical == "claude":
        tiers = {k: v for k, v in tiers.items() if k not in ("agent", "fable")}
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


def agent_model_options(cfg: Any) -> dict[str, list[dict]]:
    """每个供应商可供选择的模型（精简字段，供界面下拉使用）。"""
    out: dict[str, list[dict]] = {}
    for pid, entry in _cfg_providers(cfg).items():
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        models = []
        for m in entry.get("models") or []:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str):
                continue
            models.append({
                "id": m["id"],
                "name": m.get("name", m["id"]),
            })
        out[pid] = models
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
        old_ids = {m.get("id") for m in (entry.get("models") or []) if isinstance(m, dict)}
        if not old_ids:
            continue
        if pid not in new_providers:
            if pid not in removed_providers:
                raise ModelLossError(f"拒绝写入 providers.json：供应商 {pid} 会被整体删除")
            continue
        new_ids = {m.get("id") for m in ((new_providers.get(pid) or {}).get("models") or [])
                   if isinstance(m, dict)}
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
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    try:
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
    except ValueError:
        pass
    return s


def parse_yaml(text: str) -> Any:
    """极简 YAML 解析器: 支持嵌套映射、列表、标量、行内 {} / []。"""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # 去掉行内注释 (简单情况: 空格+#+空格)
        stripped = re.sub(r"\s+#.*$", "", raw.rstrip())
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
                elif ":" in content and not content.startswith("{") and not content.startswith("["):
                    # 列表项是映射的第一个键
                    key, _, rest = content.partition(":")
                    rest = rest.strip()
                    obj: dict[str, Any] = {}
                    if rest:
                        obj[key.strip()] = _yaml_scalar(rest) if not rest.startswith(("{", "[")) else _parse_flow(rest)
                        idx += 1
                    else:
                        idx += 1
                        sub, idx = parse_block(idx, indent + 2)
                        obj[key.strip()] = sub
                    # 同缩进后续键
                    while idx < len(lines) and lines[idx][0] >= indent + 2 and not lines[idx][1].startswith("- "):
                        k2, _, r2 = lines[idx][1].partition(":")
                        r2 = r2.strip()
                        if r2:
                            obj[k2.strip()] = (
                                _yaml_scalar(r2) if not r2.startswith(("{", "[")) else _parse_flow(r2)
                            )
                            idx += 1
                        else:
                            idx += 1
                            sub, idx = parse_block(idx, lines[idx - 1][0] + 2 if False else indent + 4)
                            obj[k2.strip()] = sub
                    items.append(obj)
                else:
                    items.append(_yaml_scalar(content) if not content.startswith(("{", "[")) else _parse_flow(content))
                    idx += 1
            return items, idx

        # 映射
        obj = {}
        while idx < len(lines) and lines[idx][0] == indent and not lines[idx][1].startswith("- "):
            key, _, rest = lines[idx][1].partition(":")
            key = key.strip()
            rest = rest.strip()
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
                k, _, v = part.partition(":")
                result[k.strip()] = _yaml_scalar(v) if not v.strip().startswith(("[", "{")) else _parse_flow(v)
            return result
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            return [_yaml_scalar(p) if not p.strip().startswith(("[", "{")) else _parse_flow(p) for p in _split_flow(inner)]
        return _yaml_scalar(s)

    def _split_flow(s: str) -> list[str]:
        parts, depth, cur = [], 0, []
        for ch in s:
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append("".join(cur))
        return parts

    data, _ = parse_block(0, lines[0][0] if lines else 0)
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
                lines.append(f"{pad}{k}:")
                lines.append(dump_yaml(v, indent + 1))
            elif isinstance(v, list) and v:
                lines.append(f"{pad}{k}:")
                lines.append(dump_yaml(v, indent + 1))
            elif isinstance(v, dict):
                lines.append(f"{pad}{k}: {{}}")
            elif isinstance(v, list):
                lines.append(f"{pad}{k}: []")
            elif v is None:
                lines.append(f"{pad}{k}: null")
            elif isinstance(v, bool):
                lines.append(f"{pad}{k}: {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{pad}{k}: {v}")
            else:
                lines.append(f"{pad}{k}: {_fmt_str(v)}")
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
                head = f"{pad}- {first}: {_fmt_val_inline(item[first])}"
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
                lines.append(f"{pad}-")
                lines.append(dump_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}- {_fmt_val_inline(item)}")
        return "\n".join(lines)
    return f"{pad}{_fmt_val_inline(obj)}"


def _fmt_str(v: Any) -> str:
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{},&*?|>!%@`]", s) or s.strip() != s:
        return json.dumps(s, ensure_ascii=False)
    return s


def _fmt_val_inline(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        if not v:
            return "{}"
        inner = ", ".join(f"{k}: {_fmt_val_inline(val)}" for k, val in v.items())
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
        self.routes: dict[str, str] = {}
        for pid, p in self.providers.items():
            if not p.get("enabled", True):
                continue
            if not p.get("baseUrl"):
                raise ValueError(f"dsh 需要 {pid} 的 baseUrl（OpenAI 兼容端点）")
            self.routes[pid] = pid

    def env_name(self, pid: str) -> str:
        if pid in self.LEGACY_ENV:
            return self.LEGACY_ENV[pid]
        return re.sub(r"[^A-Za-z0-9_]", "_", pid).upper() + "_API_KEY"

    def block(self, pid: str) -> dict:
        p = self.providers[pid]
        models = [
            {
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "contextWindow": m.get("contextWindow", 128000),
                "maxTokens": m.get("maxTokens", 32768),
                "input": m.get("input", ["text"]),
                "reasoningEfforts": {"off": None, "low": "low", "high": "high", "max": "max"},
            }
            for m in p.get("models", [])
        ]
        return {
            "displayName": p.get("displayName") or p.get("name"),
            "apiKeyEnv": self.env_name(pid),
            "api": p.get("api", "openai-completions"),
            "baseURL": p["baseUrl"],
            **({"compat": p["compat"]} if p.get("compat") else {}),
            "models": models,
        }

    def blocks(self) -> dict[str, dict]:
        return {pid: self.block(pid) for pid in self.routes}

    def credentials(self) -> dict[str, str]:
        out = {}
        for pid in self.routes:
            key = self.providers[pid].get("apiKey")
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
        if not any(m.get("id") == mid for m in self.providers[pid].get("models", [])):
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
            if not tcfg.get("enabled", True):
                continue
            for f in tcfg.get("files", []):
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
            if not tcfg.get("enabled", True):
                continue
            for f in tcfg.get("files", []):
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
        """
        return {
            pid: p
            for pid, p in self.providers.items()
            if not p.get("targets") or target in p["targets"]
        }

    # -- push adapters --
    def push_all(self, only: list[str] | None = None) -> list[str]:
        results = []
        targets = self.cfg.get("sync", {}).get("targets", {})
        dispatch = {
            "pi": self.push_pi,
            "mcode": self.push_mcode,
            "zcode": self.push_zcode,
            "dsh": self.push_dsh,
            "dsh-desktop": self.push_dsh,
            "claude": self.push_claude,
            "cc-switch": self.push_cc_switch,
            "omp": self.push_omp,
            "codex": self.push_codex,
        }
        for name, fn in dispatch.items():
            tcfg = targets.get(name, {})
            if not tcfg.get("enabled", True):
                continue
            if only and name not in only:
                continue
            # 别名目标在全量 push 时不重复执行 (dsh-desktop → dsh)
            if tcfg.get("aliasOf") and not only:
                continue
            try:
                fn()
                results.append(f"  ✓ {name}")
            except Exception as e:  # noqa: BLE001
                results.append(f"  ✗ {name}: {e}")
        return results

    # ---- pi ----
    def push_pi(self) -> None:
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

        for pid, p in self.providers_for("pi").items():
            if not p.get("enabled", True):
                continue
            # 以 pi 现有条目为底稿：hub 只覆盖自己管理的键，
            # pi 侧独有的 cost / headers / oauth 等字段与用户手工参数不会被抹掉。
            previous = providers.get(pid)
            entry = dict(previous) if isinstance(previous, dict) else {}
            entry.update(
                {
                    "name": p.get("displayName") or p.get("name"),
                    "baseUrl": p["baseUrl"],
                    "api": p.get("api", "openai-completions"),
                    "apiKey": p["apiKey"],
                }
            )
            # 供应商级 compat 必须下发：maxTokensField / supportsDeveloperRole /
            # thinkingFormat 等缺失会让 pi 发出的请求被网关 400 拒绝。
            if p.get("compat"):
                entry["compat"] = copy.deepcopy(p["compat"])
            pi_models_by_id = {
                m.get("id"): m
                for m in (previous.get("models") if isinstance(previous, dict) else None) or []
                if isinstance(m, dict)
            }
            entry["models"] = []
            for m in p.get("models", []):
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
            auth[pid] = {"type": "api_key", "key": p["apiKey"]}

        # 多 agent 模型同步：只有显式配置了 modelSync 才动 settings.json 的默认模型，
        # 否则保留用户自己挑的 defaultProvider / defaultModel。
        tier = agent_model_tiers(self.cfg, "pi").get("agent")
        if tier and tier["provider"] in {pid for pid, p in self.providers_for("pi").items()
                                         if p.get("enabled", True)}:
            settings["defaultProvider"] = tier["provider"]
            settings["defaultModel"] = tier["model"]
            if tier.get("reasoningEffort"):
                settings["defaultThinkingLevel"] = tier["reasoningEffort"]

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
            key = pid if pid in ("deepseek", "stepfun") else (
                "volcano" if pid == "firework" else pid
            )
            models_entry = {}
            for m in p.get("models", []):
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
                "api": p.get("api", "openai-completions"),
                "options": {
                    "baseURL": p["baseUrl"],
                    "authMode": "api-key",
                    "apiKey": p["apiKey"],
                },
                "models": models_entry,
            }
        data["custom_provider"] = custom
        path.write_text(dump_yaml(data) + "\n", encoding="utf-8")

    # ---- zcode ----
    def push_zcode(self) -> None:
        path = HOME / ".zcode" / "v2" / "provider_config.json"
        if not path.exists():
            raise FileNotFoundError(path)
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

        # zcode 映射: firework→火山, 其余同名
        name_map = {"firework": "火山", "openrouter": "OpenRouter", "deepseek": "DeepSeek", "stepfun": "StepFun"}
        id_map = {"firework": "a4d4d39c-5404-48db-b49a-e46d34df4525"}

        for pid, p in self.providers_for("zcode").items():
            if not p.get("enabled", True):
                continue
            zid = id_map.get(pid, pid)
            zname = name_map.get(pid, p.get("displayName") or p.get("name"))
            model_ids = [m["id"] for m in p.get("models", [])]

            existing = by_id.get(zid) or by_name.get(zname)
            api_type = "openai-responses" if pid == "firework" else "openai-chat-completions"
            # 保留原有 api type 偏好
            if existing:
                old_api = ((existing.get("config") or {}).get("api") or {}).get("type")
                if old_api:
                    api_type = old_api
                cfg_inner = existing.setdefault("config", {})
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

            cfg_inner.setdefault("group", "standard-personal")
            cfg_inner["access"] = {"type": "api-key", "apiKey": p["apiKey"]}
            cfg_inner["api"] = {"type": api_type, "baseUrl": p["baseUrl"]}
            cfg_inner["personalModelIds"] = model_ids
            cfg_inner["modelOrder"] = model_ids
            if zid not in order:
                order.append(zid)

            # 模型规则
            for m in p.get("models", []):
                mr = next(
                    (x for x in model_rules if x.get("modelId") == m["id"] and x.get("providerId") == zid),
                    None,
                )
                if not mr:
                    mr = {"modelId": m["id"], "providerId": zid, "config": {}}
                    model_rules.append(mr)
                mr["config"] = {
                    "properties": {
                        "contextWindow": m.get("contextWindow", 128000),
                        **(
                            {"inputFormat": {"supportsImage": "image" in m.get("input", [])}}
                            if "image" in m.get("input", [])
                            else {}
                        ),
                    }
                }

        save_json(path, data)

    # ---- dsh ----
    def push_dsh(self) -> None:
        plan = DshModelPlan(self)

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
                if "qwen-token" in rid:
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
                            if "qwen-token" in rid:
                                providers.pop(rid)
                        providers.update(copy.deepcopy(blocks))
                        cfg["providers"] = providers

                # 主 agent 默认模型：只有配置了 modelSync 才接管，否则保持用户当前选择
                selection = plan.selection()
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
                    if "qwen-token" in rid:
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
        """把具备 Anthropic 兼容端点的 provider 写入 Claude Code 与 cc-switch。"""
        settings_path = HOME / ".claude" / "settings.json"
        anthropic_providers = {
            pid: p
            for pid, p in self.providers_for("claude").items()
            if p.get("enabled", True) and p.get("anthropicBaseUrl")
        }
        if not anthropic_providers:
            return

        claude_cfg = (self.cfg.get("sync", {}).get("targets", {}).get("claude") or {})
        active = claude_cfg.get("activeProvider") or "deepseek"
        # 平台级特性开关：不属于任何 provider，切换 provider 不收回
        feature_env = claude_cfg.get("featureEnv") or {}
        if active not in anthropic_providers:
            active = next(iter(anthropic_providers))
        # modelSync 指向的供应商若具备 Anthropic 端点，就以它为准：跨客户端同步
        # 一次选定同一个模型，Claude Code 不应该还停在平台配置里的旧供应商。
        # 这里也把 activeProvider 写回 providers.json（state 记录由调用方负责），
        # 否则下次 push 又会退回旧供应商，反复漂移。
        synced = resolve_agent_model(self.cfg, "claude")
        if synced and synced["provider"] in anthropic_providers and synced["provider"] != active:
            active = synced["provider"]
        # 把最终选定的供应商写回 providers.json：否则下次 push 又按旧的
        # activeProvider 走一遍，出现「每次同步都在两家之间来回切」的漂移。
        if claude_cfg.get("activeProvider") != active:
            self.cfg.setdefault("sync", {}).setdefault("targets", {}).setdefault("claude", {})[
                "activeProvider"] = active
            save_json(HUB_CONFIG, self.cfg)
        # 1) ~/.claude/settings.json — 只改 env 中的 ANTHROPIC_*，保留 hooks/permissions 等
        if settings_path.exists():
            backup_file(settings_path)
            data = load_json(settings_path, default={}) or {}
        else:
            data = {"$schema": "https://json.schemastore.org/claude-code-settings.json"}
        env = data.setdefault("env", {})
        p = anthropic_providers[active]
        cm = p.get("claudeModels") or {}
        tiers = agent_model_tiers(self.cfg, "claude")

        def tier_model(tier: str):
            """档位模型：活跃供应商的 claudeModels 优先，其次 modelSync 解析结果。"""
            resolved = tiers.get(tier)
            return resolved["model"] if resolved else None

        main_model = tiers.get("model", {}).get("model") or cm.get("model")
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
        # 自定义供应商：声明式透传任意 env 变量 (CLAUDE_CODE_*、ENABLE_TOOL_SEARCH 等)
        extra_env = {
            k: v for k, v in (p.get("claudeEnv") or {}).items()
            if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
        }
        written_env = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}
        env["ANTHROPIC_AUTH_TOKEN"] = p["apiKey"]
        env["ANTHROPIC_BASE_URL"] = p["anthropicBaseUrl"]
        for k, v in model_map.items():
            if v:
                env[k] = v
                written_env.add(k)
        for k, v in extra_env.items():
            env[k] = v
            written_env.add(k)
        # hub 自己管理的 env 键命名空间（闭集）：固定别名 + 各 provider 声明的 claudeEnv。
        # 用它来收回残留：hub 声明过的键就归 hub 管，用户手工加的键不在其中，永不受影响。
        owned_env = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", *model_map}
        for q in anthropic_providers.values():
            owned_env.update(k for k in (q.get("claudeEnv") or {})
                             if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"))
        # 切换 provider 时收回上一个 provider 写入、当前 provider 未声明的 env 键：
        # 否则切到不声明 fable 的供应商后，ANTHROPIC_DEFAULT_FABLE_MODEL 还留着上一家的模型名。
        for k in owned_env:
            if k not in written_env:
                env.pop(k, None)
        data["env"] = env
        # 平台级特性开关最后写入：即使某个 provider 的 claudeEnv 声明过同名键、
        # 刚才被 owned_env 收回，这里也按平台配置补齐，切换 provider 不丢特性。
        for k, v in self._feature_env_values(feature_env).items():
            env[k] = v

        # 自定义供应商：声明式透传任意顶层 settings 字段
        # (model / effortLevel / permissions / includeCoAuthoredBy / skipDangerousModePermissionPrompt ...)
        settings_map = {
            k: v for k, v in (p.get("claudeSettings") or {}).items()
            if k not in ("env", "$schema")
        }
        # model 有两个来源：claudeModels.model（→ env.ANTHROPIC_MODEL）和
        # claudeSettings.model（→ 顶层 model）。同时声明时以 claudeModels 为准，
        # 否则出现「切换了模型但顶层 model 还指着旧型号」的漂移。
        if main_model and "model" in settings_map:
            settings_map["model"] = main_model
        # 顶层键同理：任一 provider 声明过的键都归 hub 管，切换时收回未声明的，
        # 避免残留上一家的 model / effortLevel。用户从未被任何 provider 声明过的
        # 键（hooks / statusLine / modelSettings …）不在命名空间里，永不受影响。
        # 注意 env / $schema 不能进这个命名空间——它们由 settings_map 的过滤条件始终排除，
        # 一旦进来就会被下面这段循环误删。
        owned_keys = set()
        for q in anthropic_providers.values():
            owned_keys.update((q.get("claudeSettings") or {}).keys())
        for k in owned_keys:
            if k not in settings_map:
                data.pop(k, None)
        for k, v in settings_map.items():
            data[k] = v
        save_json(settings_path, data)

        # 2) cc-switch 注册全部 anthropic 可用 provider，便于 GUI 切换
        db_path = HOME / ".cc-switch" / "cc-switch.db"
        if db_path.exists():
            backup_file(db_path)
            con = sqlite3.connect(str(db_path))
            try:
                cur = con.cursor()
                for pid, p in anthropic_providers.items():
                    cm = p.get("claudeModels") or {}

                    def registered(tier: str, fallback=None):
                        """cc-switch 条目里该档位的模型：供应商声明优先，其次 modelSync 解析结果。"""
                        value = (p.get("claudeModels") or {}).get(tier)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
                        resolved = tiers.get(tier)
                        if resolved and resolved["provider"] == pid:
                            return resolved["model"]
                        return fallback

                    display = p.get("displayName") or p.get("name")
                    settings_config = {
                        "$schema": "https://json.schemastore.org/claude-code-settings.json",
                        "env": {
                            "ANTHROPIC_AUTH_TOKEN": p["apiKey"],
                            "ANTHROPIC_BASE_URL": p["anthropicBaseUrl"],
                            **{k: v for k, v in {
                                "ANTHROPIC_MODEL": registered("model", cm.get("model")),
                                "ANTHROPIC_DEFAULT_OPUS_MODEL": registered("opus"),
                                "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": registered("opus"),
                                "ANTHROPIC_DEFAULT_SONNET_MODEL": registered("sonnet"),
                                "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": registered("sonnet"),
                                "ANTHROPIC_DEFAULT_HAIKU_MODEL": registered("haiku"),
                                "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": registered("haiku"),
                                "ANTHROPIC_DEFAULT_FABLE_MODEL": registered("fable"),
                                "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": registered("fable"),
                                "CLAUDE_CODE_SUBAGENT_MODEL": registered("subagent"),
                                **{k: v for k, v in (p.get("claudeEnv") or {}).items()
                                   if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")},
                            }.items() if v},
                        },
                        # 自定义供应商的顶层 settings 字段，随 cc-switch 条目一起切换
                        **{k: v for k, v in (p.get("claudeSettings") or {}).items()
                           if k not in ("env", "$schema")},
                    }
                    row_name = f"Hub {display}"
                    cur.execute(
                        "SELECT id FROM providers WHERE app_type=? AND name=?",
                        ("claude", row_name),
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
                            (f"hub-claude-{pid}", "claude", row_name, payload, "custom", 200, now_epoch_ms()),
                        )
                # 清理已不属于当前 anthropic 集合的 hub 条目
                expected_names = [
                    f"Hub {(p.get('displayName') or p.get('name'))}" for p in anthropic_providers.values()
                ]
                placeholders = ",".join("?" for _ in expected_names) or "''"
                cur.execute(
                    f"DELETE FROM providers WHERE app_type='claude' AND id LIKE 'hub-claude-%' AND name NOT IN ({placeholders})",
                    expected_names,
                )
                # 平台级特性开关注入全部 claude 预设：cc-switch 切换 provider 时
                # 用预设的 settings_config 覆盖 settings.json，预设里没有这些键，
                # Agent Teams 这类特性切过去就没了。
                self._apply_cc_switch_feature_env(cur, feature_env)
                con.commit()
                self._verify_cc_switch_created_at(con)
                self._verify_cc_switch_feature_env(cur, feature_env)
            finally:
                con.close()

    # ---- codex (Codex 桌面版 / CLI) ----
    def push_codex(self) -> None:
        """把同步引擎选定的模型写进 Codex 的 ~/.codex/config.toml。

        只动三样东西：顶层 `model_provider` / `model`，以及 `[model_providers.custom]`
        段。ChatGPT 登录态（auth.json 的 tokens）与其它 [model_providers.*] 段一律不碰，
        所以界面上的账号显示不受影响，请求线路由 model_provider 决定。

        没有为 codex 配置任何模型时不写文件，保持用户当前选择。
        """
        config_path = HOME / ".codex" / "config.toml"
        providers = self.providers_for("codex")
        tier = agent_model_tiers(self.cfg, "codex").get("agent")
        if tier is None:
            return
        pid, model = tier["provider"], tier["model"]
        p = providers.get(pid)
        if not isinstance(p, dict) or p.get("enabled", True) is False:
            raise ValueError(f"modelSync 指向的供应商 {pid} 未启用或未同步到 codex "
                             f"（当前 codex 可用: {', '.join(sorted(providers)) or '无'}）")
        base_url = p.get("baseUrl")
        if not base_url:
            raise ValueError(f"codex 需要 {pid} 的 baseUrl（Responses/Chat 兼容端点）")
        # 同一个网关可能给 Claude 与 Codex 发不同的 key（模型集合不同）：
        # openaiApiKey 是 Codex 专用 key，优先；其次才是通用 apiKey 或 env_key
        # （env_key 需要重启客户端才能读到新设的环境变量）。
        openai_key = p.get("openaiApiKey")
        api_field = p.get("apiKeyEnv")
        if isinstance(openai_key, str) and openai_key.strip():
            # 两个都写会让 Codex 优先解析 env_key，未重启的客户端拿不到环境变量就报
            # "Missing environment variable"，所以字面量优先时不再下发 env_key。
            auth_keys: dict[str, Any] = {"experimental_bearer_token": openai_key.strip()}
        elif isinstance(api_field, str) and api_field.strip():
            auth_keys = {"env_key": api_field.strip()}
        else:
            api_key = p.get("apiKey")
            if not isinstance(api_key, str) or not api_key.strip():
                raise ValueError(f"codex 需要 {pid} 的 openaiApiKey / apiKeyEnv / apiKey")
            auth_keys = {"experimental_bearer_token": api_key.strip()}

        if not config_path.exists():
            raise FileNotFoundError(f"没有找到 Codex 配置: {config_path}（先跑一次 codex 让它生成）")
        backup_file(config_path)
        lines = config_path.read_text(encoding="utf-8").splitlines()

        _toml_set_project_scalar(lines, "model_provider", self._codex_provider_id())
        _toml_set_project_scalar(lines, "model", model)
        if tier.get("reasoningEffort"):
            _toml_set_project_scalar(lines, "model_reasoning_effort", tier["reasoningEffort"])
        provider_keys = {
            "name": p.get("displayName") or p.get("name") or pid,
            "base_url": base_url,
            "wire_api": self._codex_wire_api(p),
            # 第三方网关不能用 ChatGPT 登录态当授权，否则 base_url 会被忽略。
            "requires_openai_auth": False,
            **auth_keys,
        }
        _toml_set_table_keys(lines, f"model_providers.{self._codex_provider_id()}", provider_keys)
        self._codex_drop_stale_keys(lines, f"model_providers.{self._codex_provider_id()}",
                                    set(provider_keys))
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _codex_drop_stale_keys(lines: list[str], header: str, keep: set[str]) -> None:
        """删掉上一次写入、这次不该再有的授权键（例如从 env_key 换成字面量 token）。"""
        span = _toml_find_table(lines, header)
        if span is None:
            return
        start, end = span
        drop = {"env_key", "experimental_bearer_token", "requires_openai_auth"} - keep
        kept: list[str] = []
        for line in lines[start + 1:end]:
            match = re.match(r"^\s*([A-Za-z0-9_\-\.]+)\s*=", line)
            if match and match.group(1) in drop:
                continue
            kept.append(line)
        lines[start + 1:end] = kept

    def _codex_provider_id(self) -> str:
        return (self.cfg.get("sync", {}).get("targets", {}).get("codex") or {}
                ).get("providerId") or "custom"

    def _codex_wire_api(self, provider: dict) -> str:
        """Codex 只有 chat / responses 两种线协议。

        默认 responses：Codex 的工具调用与推理项都按 Responses 语义发，
        降级成 chat 会丢这些能力。确实只支持 chat 的网关用 codexWireApi 显式声明。
        """
        declared = provider.get("codexWireApi")
        if isinstance(declared, str) and declared.strip() in ("chat", "responses"):
            return declared.strip()
        api = str(provider.get("api") or "")
        return "chat" if api == "openai-chat-completions" else "responses"

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
                    display = p.get("displayName") or p.get("name")
                    if app_type == "pi":
                        row_name = pid if pid != "firework" else "volcano"
                        settings_config = {
                            "baseUrl": p["baseUrl"],
                            "api": p.get("api", "openai-completions"),
                            "apiKey": p["apiKey"],
                            "models": [
                                {
                                    "id": m["id"],
                                    "name": m.get("name", m["id"]),
                                    "reasoning": m.get("reasoning", True),
                                    "input": " ".join(m.get("input", ["text"])),
                                    "contextWindow": m.get("contextWindow", 128000),
                                    "maxTokens": m.get("maxTokens", 32768),
                                }
                                for m in p.get("models", [])
                            ],
                        }
                    else:
                        row_name = "Volcano Gateway" if pid == "firework" else display
                        settings_config = {
                            "name": row_name,
                            "kind": "custom",
                            "enabled": True,
                            "api": p.get("api", "openai-completions"),
                            "options": {
                                "baseURL": p["baseUrl"],
                                "authMode": "api-key",
                                "apiKey": p["apiKey"],
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
                                for m in p.get("models", [])
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
                    if entry.get("apiKey"):
                        providers[pid]["apiKey"] = entry["apiKey"]
                    if entry.get("baseUrl"):
                        providers[pid]["baseUrl"] = entry["baseUrl"]
        elif agent == "dsh":
            imported = HOME / ".dsh" / "settings.yaml.imported"
            if imported.exists():
                data = parse_yaml(imported.read_text(encoding="utf-8")) or {}
                for pid, entry in ((data.get("llm-pi-ai") or {}).get("providers") or {}).items():
                    if pid in providers and entry.get("baseURL"):
                        providers[pid]["baseUrl"] = entry["baseURL"]
        elif agent == "claude":
            settings_path = HOME / ".claude" / "settings.json"
            if settings_path.exists():
                data = load_json(settings_path, default={}) or {}
                env = data.get("env") or {}
                token = env.get("ANTHROPIC_AUTH_TOKEN")
                base = env.get("ANTHROPIC_BASE_URL")
                if token and base:
                    # 反查哪个 provider 的 anthropicBaseUrl 匹配
                    for pid, p in providers.items():
                        if p.get("anthropicBaseUrl") == base and token == p.get("apiKey"):
                            # 已匹配，无需改
                            break
                    else:
                        # base 能对上但 key 变了 → 更新 key
                        for pid, p in providers.items():
                            if p.get("anthropicBaseUrl") == base:
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
        for target, label in (("pi", "pi"), ("dsh", "dsh / dsh 桌面版"), ("claude", "Claude Code")):
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
        codex_tier = agent_model_tiers(self.cfg, "codex").get("agent")
        if codex_tier:
            suffix = "" if codex_tier.get("known") else "  ⚠ 不在模型库中"
            lines.append(f"  {'Codex 官方':18} {codex_tier['provider']} / {codex_tier['model']}{suffix}")
        else:
            lines.append(f"  {'Codex 官方':18} 未配置（保留客户端当前模型与登录态）")
        lines.append("")
        lines.append("目标 agent 落地情况:")
        targets = self.cfg.get("sync", {}).get("targets", {})
        for tname, tcfg in targets.items():
            en = tcfg.get("enabled", True)
            files = tcfg.get("files", [])
            status_parts = []
            for f in files:
                p = expand(f)
                status_parts.append(f"{'✓' if p.exists() else '✗'} {p.name}")
            extra = ""
            if tname == "claude":
                active = tcfg.get("activeProvider", "?")
                extra = f"  active={active}"
                # 仅 anthropic 可用
                caps = [pid for pid, p in self.providers.items() if p.get("anthropicBaseUrl")]
                extra += f"  anthropic-capable={','.join(caps) or 'none'}"
            if tcfg.get("aliasOf"):
                extra += f"  (alias of {tcfg['aliasOf']})"
            lines.append(f"  {tname:12} enabled={en}  {', '.join(status_parts)}{extra}")
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
    print_lines(hub.push_all(only))
    hub.record_hashes()
    hub.save_state()
    print("完成。")
    return 0


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
        print_lines(hub.push_all())
    else:
        print("无外部变更，执行常规推送 ...")
        print_lines(hub.push_all())
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
    canonical = {("dsh" if t == "dsh-desktop" else t) for t in targets}
    unknown = sorted(canonical - {"pi", "dsh", "claude"})
    if unknown:
        print(f"未知目标: {', '.join(unknown)}（可用: pi / dsh / claude，缺省表示全局）", file=sys.stderr)
        return 1
    pid = args.provider
    if pid not in hub.providers:
        print(f"供应商不存在: {pid}", file=sys.stderr)
        return 1
    if hub.providers[pid].get("enabled") is False:
        print(f"供应商已停用: {pid}", file=sys.stderr)
        return 1
    known = {m.get("id") for m in hub.providers[pid].get("models") or []}
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
        for f in tcfg.get("files", []):
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


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(prog="hub", description="Agent Hub 统一 provider 同步")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看状态")
    p_push = sub.add_parser("push", help="推送统一配置到各 agent")
    p_push.add_argument("targets", nargs="*", help="仅推送指定 agent")
    p_pull = sub.add_parser("pull", help="从 agent 拉取变更")
    p_pull.add_argument("agent", nargs="?", default="pi")
    sub.add_parser("sync", help="检测变更并对齐")
    sub.add_parser("watch", help="持续监听自动同步")
    p_rm = sub.add_parser("remove-provider", help="删除 provider")
    p_rm.add_argument("provider_id")
    sub.add_parser("backup", help="备份目标配置")
    sub.add_parser("save-gold", help="将当前模型列表存为黄金模板")
    sub.add_parser("restore-gold", help="从黄金模板恢复模型列表")
    p_model = sub.add_parser("model", help="设置多 agent 使用的模型（写入 modelSync）")
    p_model.add_argument("provider", help="供应商 ID")
    p_model.add_argument("model", help="模型 ID")
    p_model.add_argument("--target", action="append", choices=["pi", "dsh", "dsh-desktop", "claude"],
                         help="只对指定客户端生效；不传表示全局主模型")
    p_model.add_argument("--reasoning-effort", choices=list(REASONING_EFFORTS),
                         help="推理强度（可选）")

    args = parser.parse_args(argv)
    hub = Hub()
    dispatch = {
        "status": cmd_status,
        "push": cmd_push,
        "pull": cmd_pull,
        "sync": cmd_sync,
        "watch": cmd_watch,
        "remove-provider": cmd_remove,
        "backup": cmd_backup,
        "save-gold": cmd_save_gold,
        "restore-gold": cmd_restore_gold,
        "model": cmd_model,
    }
    return dispatch[args.cmd](hub, args)


if __name__ == "__main__":
    sys.exit(main())
