"""隔离测试配置：所有密钥与客户端路径均为临时数据。"""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hub as engine
import server


def fixture() -> dict:
    providers = {}
    names = [("deepseek", "DeepSeek", "https://api.deepseek.com"),
             ("stepfun", "StepFun Step Plan (China)", "https://api.stepfun.com/step_plan/v1"),
             ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1"),
             ("firework", "Firework (Volcano Gateway)", "https://example.com/v1")]
    for pid, name, url in names:
        providers[pid] = {"name": name, "displayName": name, "baseUrl": url,
                          "apiKey": "test-not-a-real-key", "api": "openai-completions", "enabled": True,
                          "models": [{"id": f"{pid}/model-{i + 1}", "name": f"{name.split()[0]} Model {i + 1}",
                                      "contextWindow": 128000 if i % 2 else 1000000, "maxTokens": 32768,
                                      "input": ["text", "image"] if i % 2 else ["text"],
                                      "reasoning": i % 3 != 0, "cost": {"input": 0.5}, "compat": {"custom": True}}
                                     for i in range(7)]}
    providers["deepseek"]["agents"] = {
        # Anthropic 端点单独放在 claude 连接上；Codex 只走 Responses
        "claude": {"baseUrl": "https://api.deepseek.com/anthropic", "api": "anthropic-messages"},
        "codex": {"api": "openai-responses"},
    }
    providers["openrouter"]["agents"] = {"codex": {"api": "openai-responses"}}
    targets = {n: {"enabled": True, "files": [f"~/test-clients/{n}.json"]}
               for n in ("pi", "mcode", "zcode", "dsh", "claude", "cc-switch", "omp")}
    targets["dsh-desktop"] = {"enabled": True, "aliasOf": "dsh", "files": ["~/test-clients/dsh.json"]}
    targets["codex"] = {"enabled": False, "notes": "自行维护官方授权"}
    return {"providers": providers, "removedProviders": {}, "sync": {"targets": targets},
            "updatedAt": "2026-01-01T00:00:00Z",
            # 多客户端模型对齐：默认主模型指向 deepseek，缺省时不写入任何客户端覆盖。
            "modelSync": {"default": {"provider": "deepseek", "model": "deepseek/model-1",
                                      "reasoningEffort": "medium"}}}


@contextmanager
def isolated():
    with tempfile.TemporaryDirectory(prefix="agent-hub-test-") as directory:
        home = Path(directory)
        root = home / ".agent-hub"
        root.mkdir()
        config = root / "providers.json"
        config.write_text(json.dumps(fixture()), encoding="utf-8")
        def expand(path):
            text = str(path)
            return home / text[2:] if text.startswith("~/") else Path(text)

        with patch.multiple(engine, HOME=home, HUB_DIR=root, HUB_CONFIG=config,
                            HUB_STATE=root / "state.json", HUB_BACKUP=root / "backups", expand=expand), \
             patch.object(server, "HUB_CONFIG", config), patch.object(server, "expand", expand):
            yield home
