"""Conservative Codex base-config compiler. Never accesses native auth storage.

Requires Python 3.11+ (stdlib tomllib). Named *.config.toml profiles, CLI and
project overrides are outside this adapter; they can override the base settings.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import tomllib
from urllib.parse import urlsplit

PROVIDER_ID = "agent_hub"
FIELDS = ("model_provider", "model", "model_reasoning_effort")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _set_root(lines, key, value, present=True):
    """Handle ordinary and quoted scalar keys; reject multiline/compound surprises."""
    end = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    pattern = re.compile(r'^\s*(?:' + re.escape(key) + r'|"' + re.escape(key) + r'"|\'' + re.escape(key) + r"')\s*=")
    matches = [i for i in range(end) if pattern.match(lines[i])]
    if len(matches) > 1:
        raise ValueError("Codex 配置包含重复的托管字段")
    if matches:
        i = matches[0]
        if present:
            lines[i] = f"{key} = {json.dumps(value, ensure_ascii=False)}"
        else:
            del lines[i]
    elif present:
        lines.insert(end, f"{key} = {json.dumps(value, ensure_ascii=False)}")


def _remove_owned_table(lines):
    pattern = re.compile(r'^\s*\[model_providers\.agent_hub\]\s*(?:#.*)?$')
    starts = [i for i, line in enumerate(lines) if pattern.match(line)]
    if len(starts) != 1:
        raise ValueError("Codex 的 agent_hub 配置结构已更改，请先手动检查")
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    del lines[start:end]


def compile_config(text, saved, mode, provider=None, selection=None, *,
                   reconcile=False, warnings=None):
    """Compile without writes; only an explicit preview may reconcile owned edits.

    Reconciliation is not permission to apply: the transaction must still bind
    the output to the exact live source snapshot and require explicit apply.
    Automatic push/watch and direct adapter calls retain fail-closed defaults.
    """
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("Codex config.toml 无效；未写入任何配置") from exc
    if mode not in ("official", "third-party"):
        raise ValueError("Codex mode 必须是 official 或 third-party")
    if saved is not None and (not isinstance(saved, dict) or saved.get("version") != 1):
        raise ValueError("Codex 恢复记录无效，请先检查备份")
    active = bool(saved and saved.get("active"))
    if mode == "official" and ((before.get("model_providers") or {}).get("openai") or before.get("forced_login_method") == "api"):
        raise ValueError("官方配置存在外部 OpenAI 路由或 API 登录覆盖；请先在 Codex 中检查，未覆盖外部编辑")
    if mode == "official" and not active:
        # No login inspection and no guessing an official model on first use.
        if before.get("model_provider", "openai") != "openai":
            raise ValueError("尚无官方配置快照。请先在 Codex 中恢复官方 provider、model 和 effort，再启用 Hub 切换")
        return text, saved
    if "profile" in before or "profiles" in before:
        raise ValueError("检测到旧 Codex profile 配置。请先按 Codex 0.134+ 文档迁移；Hub 仅管理基础 config.toml")
    providers = before.get("model_providers", {})
    if not isinstance(providers, dict):
        raise ValueError("Codex model_providers 必须是表")
    existing = providers.get(PROVIDER_ID)
    if active:
        if not isinstance(saved.get("providerHash"), str) or not re.fullmatch(r"[0-9a-f]{64}", saved["providerHash"]):
            raise ValueError("Codex 托管 provider 记录无效，请先检查备份")
        changed = []
        provider_changed = fingerprint(existing) != saved["providerHash"]
        if before.get("model_provider") != PROVIDER_ID:
            changed.append("model_provider")
        if "managed" in saved:
            managed = saved["managed"]
            if (not isinstance(managed, dict) or set(managed) != set(FIELDS) or
                    any(not isinstance(entry, dict) or not isinstance(entry.get("present"), bool) or
                        (entry["present"] and not isinstance(entry.get("value"), str))
                        for entry in managed.values())):
                raise ValueError("Codex 托管字段记录无效，请先检查备份")
            changed.extend(key for key, entry in managed.items()
                           if key not in changed and ((key in before) != entry["present"] or
                           (entry["present"] and before[key] != entry["value"])))
        original = saved.get("official")
        if not isinstance(original, dict) or set(original) != set(FIELDS):
            raise ValueError("Codex 官方恢复快照不完整")
        for key, entry in original.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("present"), bool) or (entry["present"] and not isinstance(entry.get("value"), str)):
                raise ValueError("Codex 官方恢复快照无效")
        if changed or provider_changed:
            if not reconcile:
                raise ValueError("Codex 托管字段已在外部更改；请在同步页生成新预览并确认应用，自动同步不会覆盖外部编辑")
            # A native switch back to OpenAI is a newer official baseline. Keep
            # its current model/effort instead of reviving the previous cycle.
            if before.get("model_provider", "openai") == "openai":
                if providers.get("openai") or before.get("forced_login_method") == "api":
                    raise ValueError("官方基线包含外部 OpenAI 路由或 API 登录覆盖；请先在 Codex 中检查")
                original = {key: {"present": key in before, "value": before.get(key)} for key in FIELDS}
                if any(v["present"] and not isinstance(v["value"], str) for v in original.values()):
                    raise ValueError("Codex model/provider/effort 必须是字符串")
            if warnings is not None:
                fields = changed + (["model_providers.agent_hub"] if provider_changed else [])
                action = ("恢复官方配置；若已在 Codex 中切回官方则保留当前官方模型和推理强度"
                          if mode == "official" else "按当前 Hub 选择重新设置供应商、模型和推理强度")
                warnings.append("Codex: 检测到外部更改（" + "、".join(fields) + "）。本次预览将" + action +
                                "，并更新 Hub 托管记录。确认应用前不会写入；之后的外部编辑会使预览失效。")
    else:
        if existing is not None:
            raise ValueError("model_providers.agent_hub 已存在但不属于本工具；为避免覆盖，请先重命名该表")
        if before.get("model_provider", "openai") != "openai":
            raise ValueError("首次切换前请先在 Codex 中选好官方 provider、model 和 effort，以保存可靠的恢复快照")
        if providers.get("openai") or before.get("forced_login_method") == "api":
            raise ValueError("官方基线包含 OpenAI 路由或 API 登录覆盖，请先在 Codex 中确认官方订阅配置")
        original = {key: {"present": key in before, "value": before.get(key)} for key in FIELDS}
        if any(v["present"] and not isinstance(v["value"], str) for v in original.values()):
            raise ValueError("Codex model/provider/effort 必须是字符串")
    expected = copy.deepcopy(before)
    lines = text.splitlines()
    if active and existing is not None:
        _remove_owned_table(lines)
        del expected["model_providers"][PROVIDER_ID]
    # Preserve the current untouched parent header, not its historical presence.
    # Adding/removing an empty parent is harmless even while agent_hub is active.
    official_provider_table_present = "model_providers" in tomllib.loads("\n".join(lines))
    if active and existing is not None:
        if not expected["model_providers"] and not official_provider_table_present:
            expected.pop("model_providers")
    if mode == "official":
        for key, entry in original.items():
            _set_root(lines, key, entry["value"], entry["present"])
            if entry["present"]:
                expected[key] = entry["value"]
            else:
                expected.pop(key, None)
        next_state = {"version": 1, "active": False}
    else:
        if not isinstance(provider, dict) or not selection:
            raise ValueError("Codex 第三方模式需要明确的供应商和模型")
        wire = provider.get("codexWireApi")
        if wire != "responses" and not (wire is None and provider.get("api") == "openai-responses"):
            raise ValueError("当前 Codex 仅支持 Responses；请确认网关后显式设置 codexWireApi=responses")
        base = provider.get("baseUrl")
        if not isinstance(base, str):
            raise ValueError("Codex 需要供应商 baseUrl")
        url = urlsplit(base)
        if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Codex baseUrl 必须是无凭据、查询参数和片段的 HTTP(S) 地址")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Codex 远程供应商必须使用 HTTPS")
        env_key = provider.get("apiKeyEnv")
        literal = provider.get("openaiApiKey") or provider.get("apiKey")
        if isinstance(env_key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
            auth = {"env_key": env_key}
        elif env_key:
            raise ValueError("Codex apiKeyEnv 必须是有效环境变量名")
        elif isinstance(literal, str) and literal.strip():
            auth = {"experimental_bearer_token": literal.strip()}
        else:
            raise ValueError("Codex 需要 apiKeyEnv / openaiApiKey / apiKey")
        model = selection.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Codex 模型不能为空")
        effort = selection.get("reasoningEffort")
        if effort == "off":
            effort = "none"  # Global legacy vocabulary; Codex uses none.
        if effort is not None and effort not in ("none", "minimal", "low", "medium", "high",
                                                 "xhigh", "max", "ultra"):
            raise ValueError("Codex reasoningEffort 仅支持 none/minimal/low/medium/high/xhigh/max/ultra")
        block = {"name": provider.get("displayName") or provider.get("name") or PROVIDER_ID,
                 "base_url": base, "wire_api": "responses", "requires_openai_auth": False, **auth}
        for key, value in zip(FIELDS, (PROVIDER_ID, model, effort)):
            _set_root(lines, key, value, value is not None)
            if value is None:
                expected.pop(key, None)
            else:
                expected[key] = value
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"[model_providers.{PROVIDER_ID}]"]
        lines += [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in block.items()]
        expected.setdefault("model_providers", {})[PROVIDER_ID] = block
        next_state = {"version": 1, "active": True, "official": original, "providerHash": fingerprint(block),
                      "managed": {key: {"present": key in expected, "value": expected.get(key)} for key in FIELDS},
                      "officialProviderTablePresent": official_provider_table_present}
    result = "\n".join(lines) + "\n"
    try:
        after = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("Codex 配置结构无法安全保留；请检查复杂 TOML 字段，未写入") from exc
    if after != expected:
        raise ValueError("Codex 配置结构无法安全改写且保留其他字段；未写入")
    return result, next_state
