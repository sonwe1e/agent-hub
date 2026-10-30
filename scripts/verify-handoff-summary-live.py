"""Explicit live Codex handoff-summary probe; consumes subscription quota.

Only synthetic material is sent. An ephemeral loopback gateway reads the
existing Codex auth file but never changes it or the running Agent Hub.
No private conversation or credential is printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_subscription as codex
from gateway_runtime import export_connections
from gateway_server import Handler, Server, State, packed
from agent_hub.sessions.summary import generate_summary, preview_summary
from agent_hub.observability.store import redact


MATERIAL = """以下是完全虚构、用于测试的交接材料，不含真实用户任务：
用户目标：给假想项目 Cedar 添加 CSV 导出。
用户约束：不得修改既有公开 API。
已完成：修改 demo/exporter.py 文件中的导出字段映射。
明确的模拟验证证据：命令 python -m unittest tests.test_exporter 输出 3 passed。
尚未完成：浏览器端验证尚未运行。
未知：没有提供用户数量或性能改善数据。
"""


def identity(auth):
    return auth.get("account") or auth.get("access") or None


def main():
    parser = argparse.ArgumentParser(description="用合成交接文本验证真实 Codex 订阅模型；会消耗额度")
    parser.add_argument("--model", required=True, help="当前 Codex 模型目录中的模型 ID")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        auth = codex.read_auth()
        auth_file = codex.auth_path()
        original = hashlib.sha256(auth_file.read_bytes()).digest()
        cache = json.loads((codex.codex_home() / "models_cache.json").read_text(encoding="utf-8"))
        available = {row.get("slug") or row.get("id") for row in cache.get("models", [])
                     if isinstance(row, dict) and row.get("visibility", "list") == "list"}
        if args.model not in available:
            raise ValueError("模型不在本机 Codex 可见模型目录")
    except Exception as exc:
        print(json.dumps({"provider": "codex-chatgpt", "model": args.model,
                          "result": "not-started", "reason": type(exc).__name__}, ensure_ascii=False))
        return 1
    with tempfile.TemporaryDirectory(prefix="hub-handoff-summary-live-") as directory:
        data = Path(directory).resolve() / "gateway"
        data.mkdir()
        srv = Server(("127.0.0.1", 0), Handler)
        cfg = {"gateway": {"port": srv.server_port}, "providers": {}}
        cfg["providers"]["codex-chatgpt"] = codex.provider_config(cfg, [{"id": args.model}])
        providers, mapping = export_connections(cfg)
        (data / "providers.json").write_bytes(packed(providers))
        (data / "hub-connections.json").write_bytes(packed(mapping))
        srv.state = State(data, "temporary-probe-admin")
        srv.state.consent(True)  # Confirms no session body is written even if recording is on.
        worker = threading.Thread(target=srv.serve_forever, daemon=True)
        worker.start()
        selection = {"provider": "codex-chatgpt", "agent": "pi", "model": args.model}
        result = {"provider": "codex-chatgpt", "model": args.model, "agentConnection": "pi",
                  "syntheticMaterial": True}
        try:
            preview = preview_summary(cfg, selection, MATERIAL, account_identity=identity(auth))
            current = codex.read_auth()
            response = generate_summary(cfg, selection, MATERIAL, preview["digest"],
                                        account_identity=identity(current), timeout=args.timeout)
            afterward = codex.read_auth()
            draft = response["draft"]
            protected = [auth.get("access", ""), auth.get("account", ""),
                         current.get("access", ""), current.get("account", ""),
                         afterward.get("access", ""), afterward.get("account", ""),
                         cfg["providers"]["codex-chatgpt"]["apiKey"]]
            # The only prompt text is synthetic. Still redact known local
            # values defensively in case an upstream response echoes them.
            result.update({"result": "received", "generatedBy": response["generatedBy"],
                           "completionStatus": response["completionStatus"],
                           "accountIdentityStable": identity(auth) == identity(afterward),
                           "authFileUnchanged": hashlib.sha256(auth_file.read_bytes()).digest() == original,
                           "modelDraftFromSyntheticMaterial": redact(draft, protected),
                           "modelDraftNeedsHumanFactReview": True})
            ledger = "".join(p.read_text(encoding="utf-8") for p in data.rglob("*.jsonl"))
            result["noSyntheticBodyInLedger"] = "demo/exporter.py" not in ledger
            result["noConversationBodyFile"] = not list((data / "gateway-conversations").rglob("*.jsonl"))
            result["credentialsAbsentFromLedger"] = not any(s and s in ledger for s in protected)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if (response["completionStatus"] == "complete" and result["accountIdentityStable"]
                         and result["noConversationBodyFile"] and result["noSyntheticBodyInLedger"]
                         and result["credentialsAbsentFromLedger"]) else 1
        except Exception as exc:
            # Type and trusted module's sanitized error only: no raw upstream
            # bodies, credentials, identity, request headers or exception repr.
            from agent_hub.sessions.summary import SummaryError
            result.update({"result": "failed", "reason": str(exc) if isinstance(exc, SummaryError) else type(exc).__name__})
            print(json.dumps(result, ensure_ascii=False))
            return 1
        finally:
            srv.shutdown()
            srv.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
