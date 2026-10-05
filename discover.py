#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从供应商端点发现可用模型列表。"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

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


def discover_models(provider: dict) -> list[dict]:
    """根据 provider 配置探测模型列表。

    支持:
      - OpenAI 兼容: GET {baseUrl}/models  (Bearer token)
      - OpenRouter:  GET https://openrouter.ai/api/v1/models
      - Anthropic:   GET {anthropicBaseUrl 或 baseUrl}/v1/models (x-api-key)
    """
    base = (provider.get("baseUrl") or "").rstrip("/")
    key = provider.get("apiKey") or ""
    if not base:
        raise ValueError("baseUrl 为空，无法检测")
    if not key:
        raise ValueError("apiKey 为空，无法检测")

    candidates: list[tuple[str, dict[str, str]]] = []

    # Anthropic 风格
    a_base = (provider.get("anthropicBaseUrl") or "").rstrip("/")
    if a_base:
        candidates.append((f"{a_base}/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"}))
    # OpenAI / OpenRouter 风格
    candidates.append((f"{base}/models", {"Authorization": f"Bearer {key}"}))
    # 有的网关挂在 /v1/models 且 base 已含 /v1 → 退化成 /models；再试裸 /v1/models
    if base.endswith("/v1"):
        candidates.append((f"{base[:-3].rstrip('/')}/v1/models", {"Authorization": f"Bearer {key}"}))

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
