#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从供应商端点发现可用模型列表。"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hub import agent_connection  # noqa: E402

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _http_get_json(url: str, headers: dict[str, str], timeout: float = 15.0) -> Any:
    """GET 一个 JSON 端点。

    必须带浏览器样式的 User-Agent：部分网关（Cloudflare 等）对默认的
    "Python-urllib/3.x" 直接返回 403（Cloudflare 错误码 1010），导致
    「检测模型」在网页里对这类供应商永远失败。
    """
    hdrs = {"User-Agent": _BROWSER_UA, "Accept": "application/json, text/plain, */*"}
    hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"连接失败: {e.reason}") from e


def _norm_model(item: Any) -> dict | None:
    if isinstance(item, str):
        return {"id": item, "name": item}
    if not isinstance(item, dict):
        return None
    mid = item.get("id") or item.get("model") or item.get("name")
    if not mid:
        return None
    name = item.get("name") or item.get("display_name") or mid
    out: dict[str, Any] = {"id": str(mid), "name": str(name)}
    # 尽量带上上下文等线索
    for src_key, dst in (
        ("context_length", "contextWindow"),
        ("context_window", "contextWindow"),
        ("max_context_length", "contextWindow"),
        ("max_tokens", "maxTokens"),
        ("top_provider", None),
    ):
        if dst and src_key in item and item[src_key]:
            try:
                out[dst] = int(item[src_key])
            except (TypeError, ValueError):
                pass
    # openrouter 风格
    if "context_length" in item:
        try:
            out.setdefault("contextWindow", int(item["context_length"]))
        except (TypeError, ValueError):
            pass
    arch = item.get("architecture") or {}
    if isinstance(arch, dict):
        modality = arch.get("input_modalities") or arch.get("modality") or []
        if isinstance(modality, str):
            modality = [modality]
        if modality:
            out["input"] = [str(m) for m in modality if m]
    return out


def discover_models(provider: dict, agent: str | None = None) -> list[dict]:
    """根据 provider 配置探测模型列表。

    ``agent`` 为空时探测默认连接；指定 Agent（claude / codex / pi / dsh…）时
    探测该 Agent 的连接——同一供应商给不同 Agent 配不同 key 时，远端返回的
    模型集合本来就可以不同。

    支持:
      - Anthropic 风格 (claude 连接): GET {baseUrl}/v1/models  (x-api-key)
      - OpenAI 兼容: GET {baseUrl}/models  (Bearer token)
    """
    if agent:
        conn = agent_connection(provider, agent)
        base = (conn.get("baseUrl") or "").rstrip("/")
        key = conn.get("apiKey") or ""
        anthropic = conn.get("api") == "anthropic-messages"
    else:
        base = (provider.get("baseUrl") or "").rstrip("/")
        key = provider.get("apiKey") or ""
        anthropic = provider.get("api") == "anthropic-messages"
        if not base:
            # 兼容旧配置：只声明了 Anthropic 端点（anthropicBaseUrl）的供应商
            # 没有默认连接时，按 Claude 连接探测。
            conn = agent_connection(provider, "claude")
            if conn["baseUrl"]:
                base, key = conn["baseUrl"].rstrip("/"), conn["apiKey"]
                anthropic = True
    if not base:
        raise ValueError("该连接没有配置 baseUrl，无法检测")
    if not key:
        raise ValueError("该连接没有配置 apiKey，无法检测（apiKeyEnv 只保存变量名，不能用于探测）")

    candidates: list[tuple[str, dict[str, str]]] = []

    # Anthropic 风格
    if anthropic:
        anthropic_models = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        candidates.append((anthropic_models, {"x-api-key": key, "anthropic-version": "2023-06-01"}))
    # OpenAI / OpenRouter 风格
    candidates.append((f"{base}/models", {"Authorization": f"Bearer {key}"}))
    # 有的网关挂在 /v1/models 且 base 已含 /v1 → 退化成 /models；再试裸 /v1/models
    if base.endswith("/v1"):
        candidates.append((f"{base[:-3].rstrip('/')}/v1/models", {"Authorization": f"Bearer {key}"}))
    elif not base.endswith("/v1/models"):
        candidates.append((f"{base}/v1/models", {"Authorization": f"Bearer {key}"}))

    errors: list[str] = []
    for url, headers in candidates:
        try:
            data = _http_get_json(url, headers)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
            continue

        items: list[Any] = []
        if isinstance(data, dict):
            items = data.get("data") or data.get("models") or []
        elif isinstance(data, list):
            items = data

        models: list[dict] = []
        seen: set[str] = set()
        for it in items:
            m = _norm_model(it)
            if m and m["id"] not in seen:
                seen.add(m["id"])
                models.append(m)
        if models:
            return models
        errors.append(f"{url}: 返回 0 个模型")

    raise RuntimeError("未能检测到模型:\n" + "\n".join(errors[:4]))


def preview_merge(existing: list[dict], discovered: list[dict]) -> dict:
    """对比现有模型与检测结果: new / keep / missing。"""
    exist_ids = {m.get("id") for m in existing}
    disc_ids = {m.get("id") for m in discovered}
    return {
        "new": [m for m in discovered if m["id"] not in exist_ids],
        "keep": [m for m in discovered if m["id"] in exist_ids],
        "missing": [m for m in existing if m.get("id") not in disc_ids],
    }
