#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent Hub 本地工作台：HTTP API + 无构建依赖的静态界面。"""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
from urllib.parse import parse_qs, unquote, urlparse

# Hub 自身目录不再绑定用户主目录：按本文件位置定位，
# 整个目录可以任意搬迁（例如从 ~/.agent-hub 移到 G:\workspaces\agent-hub）。
HUB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB_DIR))
from hub import (Hub, HUB_CONFIG, ModelLossError, ZCODE_API_TYPES, assert_expected_model_changes, assert_no_retired_workbuddy_selection, backup_file,
                 agent_connection, agent_connection_ready, agent_connections,
                 canonical_target, provider_targets,
                 configure_stdio, expand, file_hash, load_json, save_json, now_iso, target_files)  # noqa: E402
from discover import discover_models  # noqa: E402
from agent_hub.agents.inventory import BulkBusy, Inventory, AGENTS  # noqa: E402
from agent_hub.observability.store import Store  # noqa: E402
from switch_plans import (DEFAULT_CLEAR, PLAN_TARGETS, PlanError,  # noqa: E402
                          analyze_plan, compile_plan, connection_snapshot,
                          diagnose_fallback, model_sync_patch_for_write,
                          normalize_plan, reference_states)

# hub.py 是多客户端模型对齐的唯一实现，这里只调用、不复制。
# 用带保护的 import：hub.py 尚未落地这些接口时，受影响的两个端点给出明确错误，
# 其余 API 与界面仍可正常启动。
try:  # pragma: no cover - 正常仓库里始终走 try 分支
    from hub import (REASONING_EFFORTS, agent_model_options, agent_model_tiers_public,
                     resolve_agent_model)  # noqa: E402
except ImportError:  # pragma: no cover
    REASONING_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    agent_model_options = agent_model_tiers_public = resolve_agent_model = None

UI_DIR = (Path(getattr(sys, "_MEIPASS", HUB_DIR)) / "ui") if getattr(sys, "frozen", False) else HUB_DIR / "ui"
LOCK = threading.RLock()
SUPPORTED_TARGETS = {"pi", "mcode", "zcode", "dsh", "dsh-desktop", "claude", "cc-switch", "omp", "codex", "opencode"}
PREVIEW_TARGETS = set(AGENTS) | {"dsh-desktop"}
PROTOCOLS = {"openai-completions", "openai-responses", "openai-chat-completions", "anthropic-messages"}
# 每个 Agent 连接允许的协议：Claude Code 只走 Anthropic Messages，Codex 第三方只走 Responses。
AGENT_APIS = {"claude": {"anthropic-messages"}, "codex": {"openai-responses"},
              "zcode": set(ZCODE_API_TYPES),
              # OpenCode 通过 ai-sdk 适配多种协议，四类端点都能接。
              "opencode": set(PROTOCOLS)}
# providers.json 里可以按 Agent 覆盖连接的目标名（dsh-desktop 归一化成 dsh）。
AGENT_CONNECTION_KEYS = ("claude", "codex", "pi", "dsh", "opencode")

# modelSync 可写的目标 -> 落盘时使用的键。dsh 桌面版与 CLI 共用同一份配置，
# 写入时归一化成 dsh，避免出现两份互相漂移的覆盖。
MODELSYNC_TARGETS = {"pi": "pi", "dsh": "dsh", "dsh-desktop": "dsh",
                     "zcode": "zcode", "claude": "claude", "codex": "codex",
                     "opencode": "opencode"}
# status / GET 需要给出解析结果的目标，顺序即界面展示顺序。
MODELSYNC_RESOLVED_TARGETS = ("pi", "dsh", "dsh-desktop", "zcode", "claude", "codex", "opencode")
# 已经没有只读目标：codex 从 v2 起只写 model_provider / model 与 [model_providers.custom]，
# ChatGPT 登录态仍然由 Codex 自己维护。
MODELSYNC_READONLY_TARGETS: set[str] = set()
# Claude Code 可单独指定的档位；fable 是历史档位，保留以兼容既有 claudeModels。
CLAUDE_TIER_KEYS = ("model", "opus", "sonnet", "haiku", "subagent", "fable")
MODELSYNC_ENTRY_KEYS = ("provider", "model", "reasoningEffort")
# 切换方案的顶层键（providers.json 里与 modelSync 平级）。
SWITCH_PLANS_KEY = "switchPlans"


def hub() -> Hub:
    return Hub()


INVENTORY = Inventory()
_OBSERVATIONS = {}
_GATEWAYS = {}
_GATEWAY_PREVIEWS = {}
_CAPABILITY_LIBRARIES = {}
_WORKSPACE_SERVICES = {}


def api_codex_subscription():
    from codex_subscription import status
    p = hub().providers.get('codex-chatgpt') or {}
    return {**status(), 'connected': p.get('kind') == 'codex-chatgpt' and p.get('enabled', True),
            'models': len(p.get('models') or [])}


def api_codex_subscription_connect():
    from codex_subscription import models, provider_config, read_auth
    before = read_auth()
    catalog = models(before)
    with LOCK:
        after = read_auth()
        if before['account'] != after['account'] or (not before['account'] and before['access'] != after['access']):
            raise ValueError('读取模型期间 Codex 账号已切换，请重新接入')
        h = hub()
        old = h.providers.get('codex-chatgpt')
        if old and old.get('kind') != 'codex-chatgpt':
            raise ValueError('codex-chatgpt ID 已被普通供应商占用，请先为它更名后再接入')
        merged = {m['id']: m for m in (old or {}).get('models', [])}
        for model in catalog:
            merged[model['id']] = {**merged.get(model['id'], {}), **model}
        p = provider_config(h.cfg, list(merged.values()), old)
        _validate_provider(p)
        h.providers['codex-chatgpt'] = p
        h.cfg.get('removedProviders', {}).pop('codex-chatgpt', None)
        _save_config(h)
    gateway().start(observations().settings()['content'])
    return {'ok': True, **api_codex_subscription()}


def api_codex_subscription_disconnect():
    with LOCK:
        h = hub()
        p = h.providers.get('codex-chatgpt')
        if p and p.get('kind') == 'codex-chatgpt':
            p['enabled'] = False
            _save_config(h)
        gateway().configure(observations().settings()['content'])
    return {'ok': True, **api_codex_subscription()}


def api_codex_subscription_quota():
    from codex_subscription import read_auth, client_version
    from agent_hub.quota.usage import codex_quota
    before=read_auth()
    value=codex_quota(before,client_version())
    after=read_auth()
    if before['account'] != after['account'] or (not before['account'] and before['access'] != after['access']):
        raise ValueError('查询额度期间 Codex 账号已切换，请重新查询')
    return {'ok':True,'quota':value}


def api_zcode_subscription():
    from zcode_subscription import status, read_auth, binding, ZCodeSubscriptionError
    p = hub().providers.get('zcode-subscription') or {}
    value=status();changed=False
    if value.get('signedIn') and p.get('subscriptionBinding') and p.get('apiKey'):
        try:changed=not secrets.compare_digest(p['subscriptionBinding'],binding(read_auth(),p['apiKey']))
        except ZCodeSubscriptionError:changed=True
    if changed:value['message']='ZCode 账号或套餐已切换，请刷新模型并恢复服务'
    return {**value, 'connected': p.get('kind') == 'zcode-subscription' and p.get('enabled', True),
            'needsRefresh':changed,'models': len(p.get('subscriptionModels') or p.get('models') or [])}


def api_zcode_subscription_connect():
    from zcode_subscription import models, provider_config, read_auth, resolve_auth, _current_identity
    before = read_auth()
    catalog = models(resolve_auth(before))
    with LOCK:
        after = read_auth()
        if _current_identity(before) != _current_identity(after):
            raise ValueError('读取模型期间 ZCode 账号已切换，请重新接入')
        h = hub()
        old = h.providers.get('zcode-subscription')
        if old and old.get('kind') != 'zcode-subscription':
            raise ValueError('zcode-subscription ID 已被普通供应商占用，请先为它更名后再接入')
        merged = {m['id']: m for m in (old or {}).get('models', [])}
        for model in catalog:
            merged[model['id']] = {**merged.get(model['id'], {}), **model}
        p = provider_config(h.cfg, list(merged.values()), old, after)
        p['subscriptionModels']=[m['id'] for m in catalog]
        _validate_provider(p)
        h.providers['zcode-subscription'] = p
        h.cfg.get('removedProviders', {}).pop('zcode-subscription', None)
        _save_config(h)
    gateway().start(observations().settings()['content'])
    return {'ok': True, **api_zcode_subscription()}


def api_zcode_subscription_disconnect():
    with LOCK:
        h = hub()
        p = h.providers.get('zcode-subscription')
        if p and p.get('kind') == 'zcode-subscription':
            p['enabled'] = False
            _save_config(h)
        gateway().configure(observations().settings()['content'])
    return {'ok': True, **api_zcode_subscription()}


def api_zcode_subscription_quota():
    from zcode_subscription import read_auth, resolve_auth, _current_identity
    from agent_hub.quota.usage import zcode_quota
    before=read_auth()
    value=zcode_quota(resolve_auth(before))
    after=read_auth()
    if _current_identity(before) != _current_identity(after):
        raise ValueError('查询额度期间 ZCode 账号已切换，请重新查询')
    return {'ok':True,'quota':value}


def gateway():
    from gateway_runtime import Runtime
    root=HUB_CONFIG.parent/'data/gateway'
    with LOCK:
        if str(root) not in _GATEWAYS:
            import hub as engine
            _GATEWAYS[str(root)]=Runtime(root,lambda:hub().cfg,
                binary_root=Path(__file__).parent/'data/runtime',
                codex_cache=Path(os.environ.get('CODEX_HOME') or engine.HOME/'.codex')/'models_cache.json')
        return _GATEWAYS[str(root)]


def api_gateway_preview(body):
    from agent_hub.agents.inventory import AGENTS
    with LOCK:
        h=hub()
        if type(body.get('enabled')) is not bool:
            raise ValueError('enabled 必须为布尔值')
        previous=h.cfg.get('gateway')
        active=(previous or {}).get('activeEnabled',(previous or {}).get('enabled',False))
        h.cfg['gateway']={**(previous or {}),'enabled':body['enabled'],'activeEnabled':active}
        _save_config(h)
        try:
            if body['enabled']:
                gateway().start(observations().settings()['content'])
            # Use the same preview, conflict snapshots, grouped writes and restore.
            selected=[a for a in AGENTS if (h.cfg.get('sync',{}).get('targets',{}).get(a) or {}).get('enabled')]
            if not body['enabled']:
                from gateway_runtime import gateway_url
                import hub as engine
                from sync_transaction import TARGET_FILES
                url=(gateway_url(h.cfg)+'/').encode()
                blocked=[a for a in AGENTS if a not in selected and any(
                    (engine.HOME/rel).is_file() and url in (engine.HOME/rel).read_bytes()
                    for rel in TARGET_FILES[a] if 'credentials' not in rel and '/auth.json' not in rel)]
                if blocked:raise ValueError('以下已接入 Agent 当前未启用同步，请先重新启用再预览恢复：'+', '.join(blocked)+'；网关仍保持运行')
            plan=h.preview_push(only=selected,reconcile=False)
            _GATEWAY_PREVIEWS[plan['planId']]=body['enabled']
        except Exception:
            if previous is None:h.cfg.pop('gateway',None)
            else:h.cfg['gateway']=previous
            _save_config(h)
            raise
        return {'planId':plan['planId'],'targets':plan['targets'],
            'files':[{'path':f['path'],'action':f['action']} for f in plan.get('files',[])],
            'warnings':[*plan.get('warnings',[]),'应用后请启动新会话或重启客户端；原生登录凭据不写入网关。'],
            'gateway':gateway().status()}


def observations():
    # Resolves from HUB_CONFIG so isolated tests can never read/write live telemetry.
    root = HUB_CONFIG.parent / "data"
    with LOCK:
        if str(root) not in _OBSERVATIONS:
            import hub as engine
            _OBSERVATIONS[str(root)] = Store(root, engine.HOME, config=lambda: hub().cfg)
        return _OBSERVATIONS[str(root)]


def capability_library():
    from agent_hub.capabilities.library import CapabilityLibrary
    import hub as engine
    root = HUB_CONFIG.parent / "data"
    with LOCK:
        key = (str(root), str(engine.HOME))
        if key not in _CAPABILITY_LIBRARIES:
            _CAPABILITY_LIBRARIES[key] = CapabilityLibrary(root, engine.HOME)
        return _CAPABILITY_LIBRARIES[key]


def _quota_identity_codex():
    from codex_subscription import read_auth
    auth = read_auth(allow_expired=True)
    return auth.get("account") or auth.get("access")


def _quota_identity_zcode():
    from zcode_subscription import read_auth
    auth = read_auth()
    # Credentials can rotate without changing the account. Only fall back to
    # a credential when the login store supplies no stable account identity.
    account = auth.get("account") or auth.get("key") or auth.get("token") or auth.get("jwt")
    return (auth.get("family"), auth.get("kind"), account, auth.get("org"), auth.get("project")) if account else None


def _quota_connected(provider):
    entry = hub().providers.get(provider) or {}
    return entry.get("kind") == provider and entry.get("enabled", True) is not False


def _recent_subscription_failures(since):
    # Select only public event metadata. Provider errors and transcript bodies
    # stay in the existing observation store and never enter the monitor.
    with observations().db() as db:
        rows = db.execute(
            "SELECT id, at, json_extract(data,'$.provider'), json_extract(data,'$.httpStatus') "
            "FROM requests WHERE at >= ? "
            "AND json_extract(data,'$.sourceType') IN ('gateway','gateway+native') "
            "AND (json_extract(data,'$.provider') IN ('codex-chatgpt','zcode-subscription') "
            "OR (agent='codex' AND json_extract(data,'$.provider') IS NULL "
            "AND json_extract(data,'$.evidence') LIKE 'Agent Hub usage.jsonl%')) "
            "AND json_extract(data,'$.status')='failed' ORDER BY at ASC, id ASC",
            (since,)).fetchall()
    return [{"id": row[0], "time": row[1], "provider": row[2] or "codex-native", "httpStatus": row[3]} for row in rows]


def workspace_services():
    from agent_hub.quota.monitor import QuotaMonitor
    from agent_hub.workspace.services import WorkspaceServices
    root = HUB_CONFIG.parent / "data"
    with LOCK:
        key = str(root)
        if key not in _WORKSPACE_SERVICES:
            quota = QuotaMonitor(root,
                queries={"codex-chatgpt": api_codex_subscription_quota,
                         "zcode-subscription": api_zcode_subscription_quota},
                identities={"codex-chatgpt": _quota_identity_codex,
                            "zcode-subscription": _quota_identity_zcode},
                connected={"codex-chatgpt": lambda: _quota_connected("codex-chatgpt"),
                           "zcode-subscription": lambda: _quota_connected("zcode-subscription")},
                recent_failures=_recent_subscription_failures)
            _WORKSPACE_SERVICES[key] = WorkspaceServices(capability_library(), quota)
        return _WORKSPACE_SERVICES[key]


def api_recent_sessions():
    from agent_hub.sessions.handoff import recent_sessions
    return recent_sessions(observations())


def api_set_session_title(body):
    from agent_hub.sessions.handoff import set_session_title
    return set_session_title(observations(), body.get('agent'), body.get('sessionId'), body.get('title'))


def api_resume_preview(body):
    from agent_hub.sessions.handoff import preview_resume
    return preview_resume(observations(), body.get("agent"), body.get("sessionId"),
                          INVENTORY, body.get("instanceId"))


def api_resume(body):
    from agent_hub.sessions.handoff import resume_session
    return resume_session(observations(), body.get("agent"), body.get("sessionId"),
                          INVENTORY, body.get("instanceId"))


def api_handoff(body):
    from agent_hub.sessions.handoff import draft_handoff
    return draft_handoff(observations(), body.get("agent"), body.get("sessionId"), body.get("target"))


def api_handoff_target(body):
    from agent_hub.sessions.handoff import handoff_target_capabilities
    return handoff_target_capabilities(INVENTORY, body.get("target"), body.get("instanceId"))


def api_handoff_open(body):
    from agent_hub.sessions.handoff import launch_target
    return launch_target(observations(), body.get("agent"), body.get("sessionId"), body.get("target"),
                         INVENTORY, body.get("instanceId"))


def api_handoff_start(body):
    from agent_hub.sessions.handoff import start_handoff
    return start_handoff(observations(), body.get("agent"), body.get("sessionId"), body.get("target"),
                         body.get("text"), INVENTORY, body.get("instanceId"))


def api_summary_routes():
    from agent_hub.sessions.summary import list_summary_routes
    return {"routes": list_summary_routes(hub().cfg)}


def _summary_identity(cfg, selection):
    if not isinstance(selection, dict):
        return None
    provider = (cfg.get("providers") or {}).get(selection.get("provider")) or {}
    kind = provider.get("kind")
    if kind == "codex-chatgpt":
        return _quota_identity_codex()
    if kind == "zcode-subscription":
        return _quota_identity_zcode()
    return None


def api_summary_preview(body):
    from agent_hub.sessions.summary import preview_summary
    cfg = hub().cfg
    return preview_summary(cfg, body.get("selection"), body.get("material"),
                           account_identity=_summary_identity(cfg, body.get("selection")))


def api_summary_generate(body):
    from agent_hub.sessions.summary import generate_summary, preview_summary
    # Synchronize the confirmed route while the config is stable, then release
    # the application lock before waiting on the remote model. Keep only the
    # gateway route lock so its watcher cannot swap the recipient mid-request.
    with LOCK:
        cfg = hub().cfg
        selection = body.get("selection")
        identity = _summary_identity(cfg, selection)
        preview = preview_summary(cfg, selection, body.get("material"), account_identity=identity)
        if not isinstance(body.get("digest"), str) or not secrets.compare_digest(preview["digest"], body["digest"]):
            raise ValueError("线路或材料已改变，请重新预览后再生成")
        runtime = gateway()
        runtime.lock.acquire()
        try:
            content = observations().settings()["content"]
            runtime.start(content)
            runtime.configure(content, cfg=cfg)
            current = _summary_identity(cfg, selection)
            if current != identity:
                raise ValueError("账号或套餐已改变，请重新预览后再生成")
        except BaseException:
            runtime.lock.release()
            raise
    try:
        return generate_summary(cfg, selection, body.get("material"), body.get("digest"),
                                account_identity=current)
    finally:
        runtime.lock.release()


def query_values(query):
    return {k: v[0] for k, v in (query or {}).items() if v}


def api_observations(query=None):
    store = observations()
    if store.settings()["enabled"] and store.progress.get("finishedAt", 0) < __import__("time").time() - 30:
        store.refresh()
    result=store.query(query_values(query))
    result['status']['gateway']=gateway().status()
    result['status']['coverage']='原生会话 + 已经过本机 Hub 网关的调用；按响应 ID 合并。直连线路只有原生日志视图，没有记录不等于零请求。'
    return result


def api_subscription_activity(query=None):
    provider=query_values(query).get('provider')
    return observations().subscription_activity(provider)


def api_observation_settings(body):
    result=observations().set_settings(body)
    runtime=gateway()
    if runtime.process and runtime.process.poll() is None:
        runtime.configure(result['settings']['enabled'] and result['settings']['content'])
    return result


def api_observation_cleanup(body):
    result=observations().cleanup(clear=body.get('clear') is True)
    runtime=gateway()
    if body.get('clear') is True and runtime.process and runtime.process.poll() is None:
        runtime.configure(False)
    return result


def api_observation_retry(body):
    """Re-read failed sources, bypassing the fingerprint short-circuit.

    Paths are validated inside Store.retry against the native roots, so a body
    cannot turn this into an arbitrary local file read.
    """
    return observations().retry(body.get('paths'))


def api_inventory(detect=False):
    return INVENTORY.detect() if detect or INVENTORY.checked_at is None else INVENTORY.view()


def api_agents_check_all() -> dict:
    """一键检测：后台并发查询所有 Agent 的发布渠道。

    立即返回，进度写在 GET /api/agents 的 bulk 里。已有批量任务时
    start_check_all 抛 BulkBusy，由 dispatch_api 翻成 409。
    """
    INVENTORY.start_check_all()
    return {"ok": True, "bulk": INVENTORY.bulk_state()}


def api_agents_update_all() -> dict:
    """一键更新：后台串行把已确认可更新的 Agent 升到最新。

    安全边界与单实例更新完全一致——只执行 agent_inventory._update 里既有的
    npm / 原生 updater 两条路径，没有任何来自请求体的命令片段。
    """
    INVENTORY.start_update_all()
    return {"ok": True, "bulk": INVENTORY.bulk_state()}


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
    targets = p.get("targets")
    claude_only = (isinstance(targets, list) and bool(targets)
                   and all(canonical_target(target) == "claude" for target in targets))
    has_agent_connection = any(str(block.get("baseUrl") or "").strip()
                               for block in agents.values())
    if not isinstance(p.get("baseUrl"), str):
        raise ValueError("baseUrl 必须是字符串")
    if not p["baseUrl"].strip() and not (has_agent_connection or (claude_only and p.get("anthropicBaseUrl"))):
        raise ValueError("baseUrl 不能为空；可在「按 Agent 覆盖」里为需要的 Agent 配置端点")
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
        if name not in (*AGENT_CONNECTION_KEYS, "zcode"):
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


def _modelsync_retired_selections(cfg: dict) -> dict:
    """Expose existing retired selections without preventing their replacement."""
    blocked = {}
    for target in MODELSYNC_RESOLVED_TARGETS:
        try:
            assert_no_retired_workbuddy_selection(cfg, target)
        except ValueError as exc:
            blocked[target] = str(exc)
    return blocked


def _validate_modelsync_entry(h: Hub, entry: object, where: str, *, allow_tiers: bool = False) -> dict:
    """校验 modelSync.default / overrides.<target> 的一条模型映射。"""
    if not isinstance(entry, dict):
        raise ValueError(f"{where} 必须是对象")
    allowed = set(MODELSYNC_ENTRY_KEYS) | ({"tiers", "preserveTiers"} if allow_tiers else set())
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ValueError(f"{where} 包含未知字段: {', '.join(unknown)}")
    pid = entry.get("provider")
    if not isinstance(pid, str) or not pid.strip():
        # 切换方案要用「清空全局主模型」表达"这次不设定全局主模型"；
        # 该哨兵只在 switch_plans 编译结果里出现，手工请求仍按非法处理。
        if where == "modelSync.default" and entry == DEFAULT_CLEAR:
            return {}
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
    if "preserveTiers" in entry:
        keep = entry["preserveTiers"]
        if not isinstance(keep, list) or any(t not in CLAUDE_TIER_KEYS[1:] for t in keep):
            raise ValueError("preserveTiers 包含未知 Claude 档位")
        checked["preserveTiers"] = list(dict.fromkeys(keep))
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
        checked = _validate_modelsync_entry(h, raw["default"], "modelSync.default")
        if checked:
            out["default"] = checked
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
            "retiredSelections": _modelsync_retired_selections(h.cfg),
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
            "resolved": resolved, "tiers": tiers, "retiredSelections": _modelsync_retired_selections(h.cfg)}


def api_providers() -> dict:
    h = hub()
    return {"providers": h.providers, "removedProviders": h.cfg.get("removedProviders", {})}


# ---------------------------------------------------------------------------
# 切换方案（switchPlans）：一次选择，明确应用到多个 Agent
#
# 方案只是"保存下来的意图"；应用时由 switch_plans.compile_plan 编译成现有的
# modelSync，再走原来的预览 / 事务 / 恢复路径。这里不做第二套同步实现。
# ---------------------------------------------------------------------------


def _switch_plans_block(cfg: dict) -> dict:
    block = cfg.get(SWITCH_PLANS_KEY)
    block = block if isinstance(block, dict) else {}
    plans = block.get("plans")
    return {"version": 1, "default": block.get("default"),
            "plans": plans if isinstance(plans, dict) else {}}


def _plan_slug(name: str) -> str:
    """方案 id 从名字派生：便于命令行与 URL 引用，冲突时由调用方加后缀。"""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-").lower()
    return slug[:48] or "plan"


def _store_switch_plans(h: Hub, block: dict) -> None:
    # Existing out-of-scope plans are archival data, not selectable daily plans.
    legacy = {k: v for k, v in _switch_plans_block(h.cfg)["plans"].items()
              if any(canonical_target(t) not in AGENTS for t in v.get("targets", []))}
    block = {**block, "plans": {**legacy, **block["plans"]}}
    h.cfg[SWITCH_PLANS_KEY] = block
    _save_config(h)


def normalize_switch_plans(h: Hub, raw: object) -> dict:
    """校验整块 switchPlans；与 modelSync 一样是整块替换，写入走 _save_config。"""
    if not isinstance(raw, dict):
        raise ValueError("switchPlans 必须是对象")
    unknown = sorted(set(raw) - {"version", "default", "plans"})
    if unknown:
        raise ValueError(f"switchPlans 包含未知字段: {', '.join(unknown)}")
    version = raw.get("version", 1)
    if version != 1:
        raise ValueError(f"switchPlans.version 只支持 1（当前 {version!r}）")
    plans_raw = raw.get("plans") or {}
    if not isinstance(plans_raw, dict):
        raise ValueError("switchPlans.plans 必须是对象")
    plans: dict[str, dict] = {}
    names: dict[str, str] = {}
    for key, value in plans_raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("switchPlans.plans 的键必须是非空字符串")
        pid = key.strip()
        try:
            plan = normalize_plan({**(value if isinstance(value, dict) else {}), "id": pid})
        except PlanError as exc:
            raise ValueError(f"方案 {pid} 无效: {exc}") from exc
        if not plan["name"]:
            raise ValueError(f"方案 {pid} 缺少 name")
        if plan["name"] in names:
            raise ValueError(f"方案名重复: {plan['name']}（{names[plan['name']]} 与 {pid}）")
        names[plan["name"]] = pid
        plans[pid] = plan
    default = raw.get("default")
    if default is None or default == "":
        default = None
    elif not isinstance(default, str) or default.strip() not in plans:
        raise ValueError(f"switchPlans.default 指向不存在的方案: {default!r}")
    else:
        default = default.strip()
    return {"version": 1, "default": default, "plans": plans}


def _plan_state(h: Hub, block: dict) -> dict:
    """界面需要的方案清单 + 失效标记；引用了被删/改名的供应商或模型必须看得见。"""
    items = []
    for pid, plan in block["plans"].items():
        if any(canonical_target(t) not in AGENTS for t in plan.get("targets", [])):
            continue
        state = reference_states(h.cfg, plan)
        items.append({
            "id": pid, "name": plan["name"], "description": plan["description"],
            "targets": plan["targets"], "base": plan["base"], "agents": plan["agents"],
            "fallback": plan["fallback"], "updatedAt": plan.get("updatedAt"),
            "stale": state["stale"], "refs": state["refs"],
        })
    return {"version": 1, "default": block["default"], "plans": items,
            "targets": list(PLAN_TARGETS)}


def plan_view() -> dict:
    """GET /api/switch-plans：已保存方案 + 每个 Agent 连接上可选的模型（不含密钥）。"""
    h = hub()
    targets = {}
    for name in PLAN_TARGETS:
        targets[name] = {"connections": connection_snapshot(h.cfg, name),
                         "resolved": resolve_agent_model(h.cfg, name) if resolve_agent_model else None}
    return {"store": _plan_state(h, _switch_plans_block(h.cfg)),
            "modelSync": _modelsync_block(h.cfg),
            "targets": targets}


def update_plans(body: dict) -> dict:
    """PUT /api/switch-plans：整块替换已保存方案。"""
    if SWITCH_PLANS_KEY not in body:
        raise ValueError(f"缺少 {SWITCH_PLANS_KEY} 字段")
    if not _modelsync_api_ready():
        raise RuntimeError("hub.py 未提供 modelSync 接口，无法保存切换方案")
    with LOCK:
        h = hub()
        block = normalize_switch_plans(h, body[SWITCH_PLANS_KEY])
        _store_switch_plans(h, block)
    return {"ok": True, "store": _plan_state(h, block)}


def _plan_request(body: dict) -> tuple[dict, list[str], str]:
    """解析 preview / apply 请求：planId、name、内联 plan 三种引用方式。"""
    h = hub()
    plans = _switch_plans_block(h.cfg)["plans"]
    ref = body.get("planId") or body.get("name")
    if isinstance(ref, str) and ref.strip():
        key = ref.strip()
        plan = plans.get(key)
        if plan is None:
            plan = next((p for p in plans.values() if p["name"] == key), None)
        if plan is None:
            raise ValueError(f"方案不存在: {key}（可能已被删除或改名）")
        plan = dict(plan)
    elif isinstance(body.get("plan"), dict):
        try:
            plan = normalize_plan(body["plan"])
        except PlanError as exc:
            raise ValueError(str(exc)) from exc
    elif isinstance(body.get("plan"), (str, int, float, bool)) or body.get("plan") is not None:
        raise ValueError("plan 必须是对象")
    else:
        raise ValueError("请提供 planId（已保存方案）或 plan（内联方案）")
    requested = body.get("targets")
    if requested is not None:
        if not isinstance(requested, list) or not requested \
                or any(not isinstance(name, str) for name in requested):
            raise ValueError("targets 必须是非空字符串数组")
        scope = [canonical_target(name) for name in requested]
        unknown = [name for name in scope if name not in PLAN_TARGETS]
        if unknown:
            raise ValueError(f"targets 包含不支持的目标: {', '.join(unknown)}")
    else:
        scope = list(plan["targets"])
    if any(canonical_target(name) not in AGENTS for name in scope):
        raise ValueError("历史方案含当前支持范围以外的目标，未写入客户端")
    return plan, scope, ""


def _mode_of(h: Hub):
    targets = (h.cfg.get("sync") or {}).get("targets") or {}

    def mode(name: str) -> str:
        entry = targets.get(name)
        entry = entry if isinstance(entry, dict) else {}
        return entry.get("mode", "official")

    return mode


def api_switch_plans() -> dict:
    return plan_view()


def api_update_switch_plans(body: dict) -> dict:
    return update_plans(body)


def api_switch_plan_analyze(body: dict) -> dict:
    """POST /api/switch-plans/analyze：只读分析——写之前就看清楚每个目标的实际变化。"""
    h = hub()
    plan, scope, _ = _plan_request(body)
    report = analyze_plan(h.cfg, plan, targets=scope, mode_of=_mode_of(h))
    report["fallback"] = diagnose_fallback(h.cfg, plan, targets=scope)
    report["configured"] = {"modelSync": _modelsync_block(h.cfg)}
    return report


def api_switch_plan_preview(body: dict) -> dict:
    """POST /api/switch-plans/preview：在隔离事务中预览模型选择与原生配置。

    只在"这次会写入的目标"参与预览：方案没选中的 Agent 一个文件都不碰。
    """
    if not _modelsync_api_ready():
        raise RuntimeError("hub.py 未提供 modelSync 接口，无法应用切换方案")
    with LOCK:
        h = hub()
        plan, scope, _ = _plan_request(body)
        report = analyze_plan(h.cfg, plan, targets=scope, mode_of=_mode_of(h))
        if not report["writable"]:
            return {"ok": False, "writable": False, "analysis": report,
                    "error": "本次方案有未解决的不兼容项，没有写入任何文件"}
        try:
            _patched, patch, _issues, blocked = compile_plan(h.cfg, plan, targets=scope)
        except PlanError as exc:
            return {"ok": False, "writable": False, "analysis": report, "error": str(exc)}
        if blocked or patch is None:
            return {"ok": False, "writable": False, "analysis": report,
                    "error": "本次方案有未解决的不兼容项，没有写入任何文件"}
        block = model_sync_patch_for_write(h.cfg, patch)
        try:
            normalized = _normalize_modelsync(h, block)
        except ValueError as exc:
            return {"ok": False, "writable": False, "analysis": report,
                    "error": f"编译结果未通过 modelSync 校验: {exc}"}
        warnings = _modelsync_warnings(h, normalized)
        write_scope = [item["target"] for item in report["details"] if item["status"] in ("ok", "warning")]
        preview = h.preview_push(only=write_scope, reconcile=True, model_sync=normalized)
        report["proposedModelSync"] = normalized
        report["modelSyncWarnings"] = warnings
    return {"ok": True, "writable": True, "planId": preview["planId"],
            "targets": preview["targets"],
            "files": [{"path": f["path"], "action": f["action"]} for f in preview.get("files", [])],
            "warnings": [*preview.get("warnings", []), *warnings],
            "analysis": report,
            "results": _plan_result_rows(report, preview),
            "disclaimer": report["disclaimer"]}


def _plan_result_rows(report: dict, preview: dict | None = None) -> list[dict]:
    """把分析结果落成"哪些写入、哪些跳过、哪些还要客户端动手"的结果区数据。"""
    files_by_target: dict[str, list[dict]] = {}
    if preview:
        for item in preview.get("files", []):
            for name in preview.get("targets", []):
                files_by_target.setdefault(name, []).append(
                    {"path": item["path"], "action": item["action"]})
    rows = []
    for item in report["details"]:
        target = item["target"]
        status = item["status"]
        row = {"target": target, "label": item["label"], "status": status,
               "provider": item["provider"], "model": item["model"],
               "reason": item.get("reason", ""),
               "needsClientAction": item.get("needsClientAction", ""),
               "effort": item["effort"], "tiers": item.get("tiers", [])}
        if status == "ok":
            row["message"] = f"将把 {item['provider']} / {item['model']} 写入该客户端的原生配置"
            if target in files_by_target:
                row["files"] = files_by_target[target]
        elif status == "warning":
            row["message"] = item.get("reason", "")
        elif status == "skipped":
            row["message"] = item.get("reason", "本次跳过")
        else:
            row["message"] = item.get("reason", "无法写入")
        rows.append(row)
    return rows


def api_switch_plan_apply(body: dict) -> dict:
    """POST /api/switch-plans/apply：一次确认走完「编译 -> 落盘 -> 事务写入 -> 结果」。

    仍然遵守现有的写入门槛：预览与应用的源文件核对、外部修改拒绝、
    官方模式门控、事务备份与回滚都在 sync_transaction 里，这里不改写它们。
    """
    result = api_switch_plan_preview(body)
    if not result.get("ok"):
        return result
    plan_id = result["planId"]
    with LOCK:
        applied = hub().apply_preview(plan_id)
        h = hub()
        recorded = _record_sync(h, applied["results"], transaction_id=applied["transactionId"])
    rows = result["results"]
    detail_by_target = {row["target"]: row for row in rows}
    for detail in recorded["details"]:
        row = detail_by_target.get(detail["target"])
        if row is not None:
            # 结果区以事务的实际回执为准：文件写入失败就是失败，不显示成成功。
            row["writeOk"] = detail["ok"]
            row["message"] = detail["message"]
            if not detail["ok"]:
                row["status"] = "failed"
        elif not detail["ok"]:
            # 事务里失败、但清单里没有对应行（例如只剩退出码）：照样要报出来。
            rows.append({"target": detail["target"], "label": detail["target"],
                         "status": "failed", "provider": "", "model": "",
                         "reason": detail["message"], "message": detail["message"],
                         "needsClientAction": "", "writeOk": False,
                         "effort": {"support": "applied", "requested": None}, "tiers": []})
    written = [row["target"] for row in rows if row.get("writeOk")]
    failed = [row["target"] for row in rows if row["status"] == "failed"]
    skipped = [row["target"] for row in rows if row["status"] == "skipped"]
    return {"ok": recorded["ok"], "planId": result["planId"],
            "targets": result["targets"], "files": result["files"],
            "warnings": result["warnings"], "analysis": result["analysis"],
            "results": rows, "written": written, "failed": failed, "skipped": skipped,
            "transactionId": recorded["transactionId"],
            "restorable": bool(recorded["transactionId"]),
            "disclaimer": result["disclaimer"],
            "clientActions": result["analysis"]["clientActions"]}


def api_switch_plan_delete(body: dict) -> dict:
    """DELETE /api/switch-plans/<id>：删方案；引用它的 default 指针一并清掉，不偷偷替换。"""
    plan_id = unquote(str(body.get("planId") or ""))
    if not plan_id:
        raise ValueError("缺少 planId")
    with LOCK:
        h = hub()
        block = _switch_plans_block(h.cfg)
        if plan_id not in block["plans"]:
            raise ValueError(f"方案不存在: {plan_id}")
        del block["plans"][plan_id]
        cleared_default = block["default"] == plan_id
        if cleared_default:
            block["default"] = None
        _store_switch_plans(h, block)
    return {"ok": True, "deleted": plan_id, "defaultCleared": cleared_default,
            "store": _plan_state(h, block)}


def api_switch_fallback(body: dict) -> dict:
    """POST /api/switch-plans/fallback：备用线路白名单诊断（不自动执行）。"""
    h = hub()
    plan, scope, _ = _plan_request(body)
    report = diagnose_fallback(h.cfg, plan, targets=scope)
    report["analysis"] = analyze_plan(h.cfg, plan, targets=scope, mode_of=_mode_of(h))
    return report


def api_status() -> dict:
    h = hub()
    changed = set(h.changed_files())
    targets = {}
    configured_targets = dict(h.cfg.get("sync", {}).get("targets") or {})
    # Older configurations predate OpenCode. Show it as disabled without writing
    # anything until the user explicitly enables it.
    configured_targets.setdefault("opencode", {"enabled": False})
    for name, t in configured_targets.items():
        if name not in PREVIEW_TARGETS:
            continue
        files = [{"path": str(expand(f)), "exists": expand(f).exists(),
                  "changed": str(expand(f)) in changed} for f in target_files(name, t)]
        targets[name] = {
            "enabled": t.get("enabled", False), "aliasOf": t.get("aliasOf"),
            "notes": t.get("notes"),
            "supported": name in PREVIEW_TARGETS, "files": files,
            "mode": t.get("mode", "official") if name in ("codex", "zcode") else None,
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
        "app": "agent-hub", "uiVersion": 6,
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
        # User-triggered preview is an explicit new intent. Reconcile only here;
        # legacy /sync, CLI push, and watch must not silently take over edits.
        plan = h.preview_push(only=selected, reconcile=True)
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
        if plan_id in _GATEWAY_PREVIEWS:
            enabled=_GATEWAY_PREVIEWS.pop(plan_id)
            h=hub()
            h.cfg['gateway'].pop('activeEnabled',None)
            _save_config(h)
            if enabled or any(p.get('kind') in ('codex-chatgpt','zcode-subscription') and p.get('enabled',True) for p in h.providers.values()):gateway().start(observations().settings()['content'])
            else:gateway().stop()
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
            if (name not in PREVIEW_TARGETS or (name not in targets and name != "opencode")
                    or (targets.get(name) or {}).get("aliasOf")):
                raise ValueError(f"无法单独设置此平台: {name}")
            if not isinstance(update, dict):
                raise ValueError("平台设置必须是对象")
            proposed.setdefault(name, {"enabled": False})
            if "enabled" in update:
                if not isinstance(update["enabled"], bool):
                    raise ValueError("enabled 必须是布尔值")
                proposed[name]["enabled"] = update["enabled"]
            unknown = set(update) - {"enabled", "mode"}
            if unknown:
                raise ValueError("未知的平台设置字段")
            if "mode" in update:
                if name not in ("codex", "zcode") or update["mode"] not in ("official", "third-party"):
                    label = "Codex" if name == "codex" else "ZCode"
                    raise ValueError(f"{label} mode 必须是 official 或 third-party")
                proposed[name]["mode"] = update["mode"]
        for name, t in proposed.items():
            if t.get("aliasOf"):
                t["enabled"] = proposed.get(t["aliasOf"], {}).get("enabled", False)
        h.cfg.setdefault("sync", {})["targets"] = proposed
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
            # Hidden legacy connections remain byte-for-byte data; the daily UI
            # can only replace the four active connections.
            p["agents"] = {**{k: v for k, v in (p.get("agents") or {}).items() if k not in AGENTS}, **body["agents"]}
        if "targets" in body:
            p["targets"] = list(dict.fromkeys([*body["targets"], *[t for t in p.get("targets", []) if canonical_target(t) not in AGENTS]])) if body["targets"] else []
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


def _normalize_model(body: dict, *, fill_defaults: bool = False) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"].strip():
        raise ValueError("模型 ID 不能为空")
    if "name" in body and not isinstance(body["name"], str):
        raise ValueError("模型名称必须是字符串")
    inp = body.get("input", ["text"] if fill_defaults else None)
    if inp is not None:
        if isinstance(inp, str):
            inp = [s.strip() for s in inp.split(",") if s.strip()]
        if not isinstance(inp, list) or not inp or any(not isinstance(v, str) or not v.strip() for v in inp):
            raise ValueError("至少选择一种输入类型")
    elif "input" in body:
        raise ValueError("至少选择一种输入类型")
    numbers = {}
    for field, default in (("contextWindow", 128000), ("maxTokens", 32768)):
        if not fill_defaults and field not in body:
            continue
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
    if "cost" in body:
        cost = body["cost"]
        if not isinstance(cost, dict):
            raise ValueError("模型参考单价必须是对象")
        for field in ("input", "output", "cacheRead", "cacheWrite"):
            value = cost.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError(f"{field} 参考单价必须是非负有限数字，或留空表示未知")
    # 编辑时保留 cost / compat 等非 UI 字段，不悄悄丢弃高级配置。
    # agent 是「这条模型属于哪个连接」的路由参数，不是模型字段，不能落进配置。
    fields = {k: v for k, v in body.items() if k != "agent"}
    result = {**fields, "id": body["id"].strip(), "name": body.get("name") or body["id"].strip(), **numbers}
    if inp is not None:
        result["input"] = list(dict.fromkeys(inp))
    if "reasoning" in body or fill_defaults:
        result["reasoning"] = body.get("reasoning", True)
    return result


def _connection_models(h: Hub, pid: str, agent: str | None) -> list[dict]:
    """某个连接待编辑的模型列表；继承库在首次编辑时复制为独立库。"""
    p = _provider(h, pid)
    if agent:
        name = canonical_target(agent)
        if name not in AGENT_CONNECTION_KEYS:
            raise ValueError(f"未知的连接: {agent}")
        override = p.setdefault("agents", {}).setdefault(name, {})
        if not isinstance(override.get("models"), list):
            override["models"] = copy.deepcopy(agent_connection(p, name)["models"])
        return override["models"]
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
        merged = {**old, **body}
        # 对已有模型，缺失和显式清空都表示未知；只改价格不能补写猜测值。
        for field in ("contextWindow", "maxTokens", "input", "reasoning"):
            if field in body and body[field] is None:
                merged.pop(field, None)
        new = _normalize_model(merged, fill_defaults=False)
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
    p=_provider(h,pid)
    if p.get('kind') == 'workbuddy-ai':
        raise ValueError('WorkBuddy 订阅反代已取消，请选择其他可用线路')
    elif p.get('kind') == 'codex-chatgpt':
        from codex_subscription import models as codex_models, read_auth
        models = codex_models(read_auth())
    elif p.get('kind') == 'zcode-subscription':
        from zcode_subscription import models as zcode_models, read_auth
        models = zcode_models(read_auth())
    else:
        models = discover_models(p,agent)
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


def api_import_scan(target: str) -> dict:
    """列出某个客户端原生配置中可导入的供应商候选。"""
    if target not in AGENTS:
        raise ValueError(f"不支持从 {target} 读取供应商")
    return {"candidates": hub().import_candidates(target)}


def api_import_apply(target: str, body: dict) -> dict:
    """把选中的供应商写入 providers.json；随后即可同步到其它平台。"""
    if target not in AGENTS:
        raise ValueError(f"不支持导入到来自 {target} 的条目")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("请选择要导入的供应商")
    label = {"zcode": "ZCode", "pi": "pi", "dsh": "dsh", "codex": "Codex", "claude": "Claude Code"}[target]
    imported: list[str] = []
    with LOCK:
        h = hub()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("导入条目必须是对象")
            pid = item.get("id")
            if not isinstance(pid, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", pid):
                raise ValueError("供应商 ID 只能包含字母、数字、_、-，且以字母或数字开头")
            name = item.get("name") if isinstance(item.get("name"), str) and item.get("name").strip() else pid
            provider = {"name": name, "displayName": name,
                        "baseUrl": item.get("baseUrl") or "", "apiKey": item.get("apiKey") or "",
                        "api": item.get("api") or "openai-completions", "enabled": True,
                        "notes": f"从 {label} 客户端原生配置导入",
                        "models": [_normalize_model(m) for m in item.get("models") or [] if isinstance(m, dict)]}
            env = item.get("apiKeyEnv")
            if isinstance(env, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
                provider["apiKeyEnv"] = env
            _validate_provider(provider)
            if pid in h.providers:
                raise ValueError(f"供应商已存在: {pid}")
            h.providers[pid] = provider
            h.cfg.get("removedProviders", {}).pop(pid, None)
            imported.append(pid)
        _save_config(h)
    return {"ok": True, "imported": imported}


# URL 编码必须在路由匹配之后解码：openrouter 等模型 ID 自带 /。
ROUTES = [
    ("GET", r"^/api/capabilities$", lambda m,b,q=None: capability_library().list_items()),
    ("GET", r"^/api/capabilities/discover$", lambda m,b,q=None: capability_library().discover()),
    ("POST", r"^/api/capabilities/import$", lambda m,b,q=None: capability_library().import_item(b)),
    ("PUT", r"^/api/capabilities/([a-f0-9]{32})$", lambda m,b,q=None: capability_library().update_item(m[1], b)),
    ("DELETE", r"^/api/capabilities/([a-f0-9]{32})$", lambda m,b,q=None: capability_library().remove_item(m[1])),
    ("POST", r"^/api/capabilities/([a-f0-9]{32})/preview$", lambda m,b,q=None: capability_library().preview(m[1], b)),
    ("GET", r"^/api/capabilities/([a-f0-9]{32})/history$", lambda m,b,q=None: capability_library().history(m[1])),
    ("POST", r"^/api/capabilities/apply$", lambda m,b,q=None: capability_library().apply(b.get("planId"))),
    ("POST", r"^/api/capabilities/discard$", lambda m,b,q=None: capability_library().discard_preview(b.get("planId"))),
    ("POST", r"^/api/capabilities/restore$", lambda m,b,q=None: capability_library().restore(b.get("transactionId"))),
    ("GET", r"^/api/quota-monitor$", lambda m,b,q=None: workspace_services().quota.status()),
    ("GET", r"^/api/workspace/status$", lambda m,b,q=None: workspace_services().status()),
    ("PUT", r"^/api/quota-monitor/settings$", lambda m,b,q=None: workspace_services().quota.update_settings(b)),
    ("POST", r"^/api/quota-monitor/refresh$", lambda m,b,q=None: workspace_services().quota.refresh(b.get("provider"))),
    ("GET", r"^/api/sessions/recent$", lambda m,b,q=None: api_recent_sessions()),
    ("PUT", r"^/api/sessions/title$", lambda m,b,q=None: api_set_session_title(b)),
    ("POST", r"^/api/sessions/resume/preview$", lambda m,b,q=None: api_resume_preview(b)),
    ("POST", r"^/api/sessions/resume$", lambda m,b,q=None: api_resume(b)),
    ("POST", r"^/api/sessions/handoff$", lambda m,b,q=None: api_handoff(b)),
    ("POST", r"^/api/sessions/target$", lambda m,b,q=None: api_handoff_target(b)),
    ("POST", r"^/api/sessions/target/open$", lambda m,b,q=None: api_handoff_open(b)),
    ("POST", r"^/api/sessions/handoff/start$", lambda m,b,q=None: api_handoff_start(b)),
    ("GET", r"^/api/sessions/summary/routes$", lambda m,b,q=None: api_summary_routes()),
    ("POST", r"^/api/sessions/summary/preview$", lambda m,b,q=None: api_summary_preview(b)),
    ("POST", r"^/api/sessions/summary/generate$", lambda m,b,q=None: api_summary_generate(b)),
    ("GET", r"^/api/zcode-subscription$", lambda m,b,q=None: api_zcode_subscription()),
    ("POST", r"^/api/zcode-subscription/connect$", lambda m,b,q=None: api_zcode_subscription_connect()),
    ("POST", r"^/api/zcode-subscription/disconnect$", lambda m,b,q=None: api_zcode_subscription_disconnect()),
    ("POST", r"^/api/zcode-subscription/quota$", lambda m,b,q=None: api_zcode_subscription_quota()),
    ("GET", r"^/api/codex-subscription$", lambda m,b,q=None: api_codex_subscription()),
    ("POST", r"^/api/codex-subscription/connect$", lambda m,b,q=None: api_codex_subscription_connect()),
    ("POST", r"^/api/codex-subscription/disconnect$", lambda m,b,q=None: api_codex_subscription_disconnect()),
    ("POST", r"^/api/codex-subscription/quota$", lambda m,b,q=None: api_codex_subscription_quota()),
    ("GET", r"^/api/gateway$", lambda m,b,q=None: gateway().status()),
    ("POST", r"^/api/gateway/start$", lambda m,b,q=None: gateway().start(observations().settings()['content'])),
    ("POST", r"^/api/gateway/preview$", lambda m,b,q=None: api_gateway_preview(b)),
    ("GET", r"^/api/agents$", lambda m, b, q=None: api_inventory()),
    ("POST", r"^/api/agents/detect$", lambda m, b, q=None: api_inventory(True)),
    ("POST", r"^/api/agents/check$", lambda m, b, q=None: INVENTORY.check(b.get("instanceId"))),
    ("POST", r"^/api/agents/update$", lambda m, b, q=None: INVENTORY.start_update(b.get("instanceId"))),
    ("POST", r"^/api/agents/check-all$", lambda m, b, q=None: api_agents_check_all()),
    ("POST", r"^/api/agents/update-all$", lambda m, b, q=None: api_agents_update_all()),
    ("GET", r"^/api/observation$", lambda m, b, q=None: api_observations(q)),
    ("GET", r"^/api/subscriptions/activity$", lambda m, b, q=None: api_subscription_activity(q)),
    ("GET", r"^/api/observation/status$", lambda m, b, q=None: observations().status()),
    ("POST", r"^/api/observation/retry$", lambda m, b, q=None: api_observation_retry(b)),
    ("PUT", r"^/api/observation/settings$", lambda m, b, q=None: api_observation_settings(b)),
    ("POST", r"^/api/observation/refresh$", lambda m, b, q=None: observations().refresh()),
    ("POST", r"^/api/observation/cleanup$", lambda m, b, q=None: api_observation_cleanup(b)),
    ("GET", r"^/api/observation/requests/([a-f0-9]+)$", lambda m, b, q=None: observations().detail(m[1])),
    ("GET", r"^/api/observation/session$", lambda m, b, q=None: observations().session_detail(query_values(q).get("agent"), query_values(q).get("session"), query_values(q))),
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
    ("GET", r"^/api/switch-plans$", lambda m, b, q=None: api_switch_plans()),
    ("PUT", r"^/api/switch-plans$", lambda m, b, q=None: api_update_switch_plans(b)),
    ("POST", r"^/api/switch-plans/analyze$", lambda m, b, q=None: api_switch_plan_analyze(b)),
    ("POST", r"^/api/switch-plans/preview$", lambda m, b, q=None: api_switch_plan_preview(b)),
    ("POST", r"^/api/switch-plans/apply$", lambda m, b, q=None: api_switch_plan_apply(b)),
    ("POST", r"^/api/switch-plans/fallback$", lambda m, b, q=None: api_switch_fallback(b)),
    ("DELETE", r"^/api/switch-plans/([^/]+)$",
     lambda m, b, q=None: api_switch_plan_delete({**(b or {}), "planId": unquote(m[1])})),
    ("POST", r"^/api/providers$", lambda m, b, q=None: api_add_provider(b)),
    ("PUT", r"^/api/providers/([^/]+)$", lambda m, b, q=None: api_update_provider(unquote(m[1]), b)),
    ("DELETE", r"^/api/providers/([^/]+)$", lambda m, b, q=None: api_delete_provider(unquote(m[1]))),
    ("POST", r"^/api/providers/([^/]+)/models$", lambda m, b, q=None: api_add_model(unquote(m[1]), b)),
    ("PUT", r"^/api/providers/([^/]+)/models/([^/]+)$", lambda m, b, q=None: api_update_model(unquote(m[1]), unquote(m[2]), b)),
    ("DELETE", r"^/api/providers/([^/]+)/models/([^/]+)$", lambda m, b, q=None: api_delete_model(unquote(m[1]), unquote(m[2]), _query_agent(q))),
    ("GET", r"^/api/providers/([^/]+)/discover$", lambda m, b, q=None: api_discover_models(unquote(m[1]), _query_agent(q))),
    ("POST", r"^/api/providers/([^/]+)/models/batch-delete$", lambda m, b, q=None: api_batch_delete_models(unquote(m[1]), b)),
    ("POST", r"^/api/providers/([^/]+)/models/import$", lambda m, b, q=None: api_import_models(unquote(m[1]), b)),
    ("GET", r"^/api/import/([a-z-]+)$", lambda m, b, q=None: api_import_scan(m[1])),
    ("POST", r"^/api/import/([a-z-]+)$", lambda m, b, q=None: api_import_apply(m[1], b)),
]


def dispatch_api(method: str, path: str, query: dict | None = None, body: dict | None = None) -> tuple[int, dict | list]:
    """在进程内直接调用 ROUTES，不经过 HTTP。返回 (状态码, 载荷)。

    HTTP 处理器与桌面壳（desktop_app.py）共用同一份分发逻辑：
    桌面壳借此完全摆脱监听端口，页面里的 fetch 经 WebView2
    WebMessage 直达这里。URL 解码由各路由自行处理（openrouter
    等模型 ID 自带 /），与旧行为一致。
    """
    body = body or {}
    query = query or {}
    try:
        for verb, pattern, fn in ROUTES:
            if verb != method:
                continue
            match = re.match(pattern, path)
            if match:
                return 200, fn(match, body, query)
        return 404, {"error": f"unknown api: {method} {path}"}
    except BulkBusy as e:
        # 批量检测/更新已在跑：409 是「当前状态不允许」，不是参数错误。
        return 409, {"error": str(e), "bulk": e.bulk}
    except ValueError as e:
        return 400, {"error": str(e)}
    except ModelLossError as e:
        # 护栏拒绝写入：说明这次请求会丢掉预期之外的模型
        return 409, {"error": str(e)}
    except Exception as e:
        return 500, {"error": f"{type(e).__name__}: {e}"}


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
        host = urlparse("http://" + (self.headers.get("Host") or "")).hostname
        if host not in ("localhost", "127.0.0.1", "::1"):
            self._send_json(403, {"error": "工作台 API 仅允许本机 Host"})
            return
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            self._send_json(403, {"error": "拒绝跨站请求"})
            return
        split = urlparse(self.path)
        path = split.path
        try:
            body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
            if method == "POST" and path == "/api/shutdown":
                self._send_json(200, {"ok": True, "message": "本地服务正在退出"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            status, payload = dispatch_api(method, path, parse_qs(split.query), body)
            self._send_json(status, payload)
        except BulkBusy as e:
            # 批量检测/更新已在跑：409 是「当前状态不允许」，不是参数错误。
            self._send_json(409, {"error": str(e), "bulk": e.bulk})
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
    collector = observations()
    collector.start()
    managed_gateway=gateway()
    managed_gateway.watch(collector.settings)
    workspace=workspace_services()
    workspace.start()
    url = f"http://{args.host}:{args.port}/"
    print(f"Agent Hub 工作台: {url}\nCtrl+C 退出")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        workspace.close()
        managed_gateway.close()
        collector.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
