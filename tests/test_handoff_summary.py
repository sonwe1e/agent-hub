"""Real loopback sockets and synthetic material; never contacts a vendor."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import codex_subscription
import workbuddy
import zcode_subscription
from gateway_runtime import export_connections
from gateway_server import Handler, Server, State, packed
from handoff_summary import (MAX_MATERIAL_BYTES, SummaryError, generate_summary,
                             list_summary_routes, preview_summary)
from test_proxy_transport import PROXY_ENV


class HandoffSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        owner = self
        class Vendor(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if self.path.startswith("/backend-api/codex/"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    item = {"type": "message", "id": "fixture-message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "模拟订阅草稿"}]}
                    events = [{"type": "response.created", "response": {"id": "fixture-response", "status": "in_progress"}},
                              {"type": "response.output_text.delta", "output_index": 0, "delta": "模拟订阅草稿"},
                              {"type": "response.completed", "response": {"id": "fixture-response", "status": "completed",
                               "output": [item], "usage": {"input_tokens": 5, "output_tokens": 3}}}]
                    for event in events:
                        self.wfile.write(b"event: " + event["type"].encode() + b"\ndata: " + packed(event) + b"\n\n")
                    self.wfile.flush()
                    return
                if self.path.startswith("/zcode-fixture/"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    events = [{"type": "message_start", "message": {"id": "zmsg", "type": "message", "role": "assistant",
                               "model": "model", "content": [], "usage": {"input_tokens": 5, "output_tokens": 0}}},
                              {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                              {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "模拟订阅草稿"}},
                              {"type": "content_block_stop", "index": 0},
                              {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
                              {"type": "message_stop"}]
                    for event in events:
                        self.wfile.write(b"event: " + event["type"].encode() + b"\ndata: " + packed(event) + b"\n\n")
                    self.wfile.flush()
                    return
                if self.path == "/v2/chat/completions":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    events = [{"id": "wmsg", "model": "model", "choices": [{"delta": {"content": "模拟订阅草稿"}}]},
                              {"choices": [{"delta": {}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}]
                    for event in events:
                        self.wfile.write(b"data: " + packed(event) + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                if self.path.endswith("/chat/completions"):
                    response = {"choices": [{"message": {"content": "模拟交接草稿"}, "finish_reason": "stop"}]}
                elif self.path.endswith("/responses"):
                    response = {"status": "completed", "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": "模拟交接草稿"}]}]}
                else:
                    response = {"content": [{"type": "text", "text": "模拟交接草稿"}], "stop_reason": "end_turn"}
                wire = packed(response)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

        self.vendor = ThreadingHTTPServer(("127.0.0.1", 0), Vendor)
        threading.Thread(target=self.vendor.serve_forever, daemon=True).start()
        self.addCleanup(self.vendor.server_close)
        self.addCleanup(self.vendor.shutdown)
        self.directory = self.root / "gateway"
        self.directory.mkdir()
        self.gateway = Server(("127.0.0.1", 0), Handler)
        self.gateway.state = State(self.directory, "fixture-control")
        self.gateway.state.consent(True)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.gateway.shutdown)
        upstream = f"http://127.0.0.1:{self.vendor.server_port}/v1"
        self.cfg = {"gateway": {"port": self.gateway.server_port}, "providers": {
            "fixture": {"name": "Fixture", "baseUrl": upstream, "api": "openai-completions",
                        "apiKey": "do-not-expose-fixture-key", "models": [{"id": "some/model"}],
                        "agents": {"codex": {"api": "openai-responses"},
                                   "claude": {"api": "anthropic-messages", "baseUrl": upstream}}}}}
        exported, mapping = export_connections(self.cfg)
        (self.directory / "providers.json").write_bytes(packed(exported))
        (self.directory / "hub-connections.json").write_bytes(packed(mapping))
        env = {**PROXY_ENV, "NO_PROXY": "127.0.0.1,localhost"}
        patched = patch.dict(os.environ, env)
        patched.start()
        self.addCleanup(patched.stop)
        self.material = "任务：修复测试。已完成：修改模块。验证：尚未执行。private-fixture-material"

    def selection(self, agent="pi"):
        return {"provider": "fixture", "agent": agent, "model": "some/model"}

    def test_preview_and_catalog_are_local_and_do_not_expose_key(self):
        routes = list_summary_routes(self.cfg)
        preview = preview_summary(self.cfg, self.selection(), self.material)
        self.assertIn(self.material, preview["material"])
        self.assertIn("模型草稿仍需核对", preview["notice"])
        self.assertNotIn("do-not-expose-fixture-key", json.dumps([routes, preview]))
        self.assertEqual(self.calls, [])

    def test_all_three_protocols_through_actual_hub_gateway(self):
        for agent, path in (("pi", "/v1/chat/completions"), ("codex", "/v1/responses"),
                            ("claude", "/v1/messages")):
            with self.subTest(agent=agent):
                selection = self.selection(agent)
                preview = preview_summary(self.cfg, selection, self.material)
                result = generate_summary(self.cfg, selection, self.material, preview["digest"])
                self.assertEqual(result["generatedBy"], "model")
                self.assertEqual(result["draft"], "模拟交接草稿")
                self.assertEqual(result["completionStatus"], "complete")
                self.assertEqual(self.calls[-1]["path"], path)
                self.assertEqual(self.calls[-1]["body"]["model"], "some/model")
                self.assertIn(self.material, json.dumps(self.calls[-1]["body"], ensure_ascii=False))
                self.assertNotIn("session_id", self.calls[-1]["body"])
                self.assertNotIn("session-id", {x.lower() for x in self.calls[-1]["headers"]})
        # Recording is enabled, but the request has no session id; neither the
        # prompt nor the generated draft may be written into a conversation.
        for _ in range(40):
            if (self.directory / "usage.jsonl").exists(): break
            time.sleep(.01)
        self.assertFalse(list((self.directory / "gateway-conversations").rglob("*.jsonl")))
        stored = "".join(p.read_text(encoding="utf-8") for p in self.directory.rglob("*.jsonl"))
        self.assertNotIn("private-fixture-material", stored)
        self.assertNotIn("do-not-expose-fixture-key", stored)

    def test_material_selection_and_endpoint_change_invalidate_preview(self):
        selection = self.selection()
        preview = preview_summary(self.cfg, selection, self.material)
        with self.assertRaisesRegex(SummaryError, "重新预览"):
            generate_summary(self.cfg, selection, self.material + " changed", preview["digest"])
        with self.assertRaisesRegex(SummaryError, "重新预览"):
            generate_summary(self.cfg, selection, self.material, "无效摘要")
        changed = copy.deepcopy(self.cfg)
        changed["providers"]["fixture"]["baseUrl"] += "/other"
        with self.assertRaisesRegex(SummaryError, "重新预览"):
            generate_summary(changed, selection, self.material, preview["digest"])
        changed = copy.deepcopy(self.cfg)
        changed["gateway"]["port"] += 1
        with self.assertRaisesRegex(SummaryError, "重新预览"):
            generate_summary(changed, selection, self.material, preview["digest"])
        self.assertEqual(self.calls, [])

    def test_missing_selection_and_model_are_rejected_without_network(self):
        for selection in ({}, {"provider": "fixture", "agent": "pi", "model": "unknown"},
                          {"provider": "other", "agent": "pi", "model": "some/model"},
                          {"provider": "fixture", "agent": "pi", "model": "some/model", "url": "https://evil.invalid"}):
            with self.assertRaises(SummaryError):
                preview_summary(self.cfg, selection, self.material)
        with self.assertRaises(SummaryError):
            preview_summary(self.cfg, self.selection(), " " )
        with self.assertRaises(SummaryError):
            preview_summary(self.cfg, self.selection(), "a" * (MAX_MATERIAL_BYTES + 1))
        self.assertEqual(self.calls, [])

    def test_provider_error_and_malformed_output_never_echo_secret(self):
        selection = self.selection()
        preview = preview_summary(self.cfg, selection, self.material)
        for status, response in ((429, b'{"error":"fixture-secret"}'),
                                 (401, b'{"error":"fixture-secret"}'),
                                 (200, b'not-json fixture-secret')):
            with self.assertRaises(SummaryError) as raised:
                generate_summary(self.cfg, selection, self.material, preview["digest"],
                                 transport=lambda *_: (status, response))
            self.assertNotIn("fixture-secret", str(raised.exception))

    def test_incomplete_and_unknown_completion_are_explicit(self):
        selection = self.selection()
        preview = preview_summary(self.cfg, selection, self.material)
        for finish, expected in (("length", "truncated"), (None, "unknown")):
            response = packed({"choices": [{"message": {"content": "partial"}, "finish_reason": finish}]})
            result = generate_summary(self.cfg, selection, self.material, preview["digest"],
                                      transport=lambda *_: (200, response))
            self.assertEqual(result["completionStatus"], expected)
            self.assertIn("请", result["notice"])

    def test_tool_request_or_refusal_is_not_used_as_a_summary(self):
        selection = self.selection()
        preview = preview_summary(self.cfg, selection, self.material)
        for message in ({"content": "text", "tool_calls": [{}]}, {"content": "text", "refusal": "no"}):
            response = packed({"choices": [{"message": message, "finish_reason": "stop"}]})
            with self.assertRaises(SummaryError):
                generate_summary(self.cfg, selection, self.material, preview["digest"],
                                 transport=lambda *_: (200, response))

    def test_subscription_lines_use_local_token_not_public_catalog(self):
        cfg = {"gateway": {"port": self.gateway.server_port}, "providers": {}}
        cfg["providers"]["codex-chatgpt"] = codex_subscription.provider_config(cfg, [{"id": "model"}])
        selection = {"provider": "codex-chatgpt", "agent": "pi", "model": "model"}
        account = ("fixture-private-identity", "fixture-plan")
        preview = preview_summary(cfg, selection, self.material, account_identity=account)
        seen = []
        def fake(url, headers, body, timeout):
            seen.append((url, headers, json.loads(body), timeout))
            return 200, packed({"choices": [{"message": {"content": "draft"}, "finish_reason": "stop"}]})
        result = generate_summary(cfg, selection, self.material, preview["digest"], transport=fake, account_identity=account)
        key = cfg["providers"]["codex-chatgpt"]["apiKey"]
        self.assertEqual(seen[0][1]["Authorization"], "Bearer " + key)
        self.assertNotIn(key, json.dumps(preview) + json.dumps(result) + json.dumps(list_summary_routes(cfg)))
        self.assertNotIn("fixture-private-identity", json.dumps(preview) + json.dumps(result))
        self.assertIn("chatgpt.com", preview["destination"])

    def test_subscription_account_change_requires_new_preview_without_network(self):
        cfg = {"gateway": {"port": self.gateway.server_port}, "providers": {}}
        cfg["providers"]["codex-chatgpt"] = codex_subscription.provider_config(cfg, [{"id": "model"}])
        selection = {"provider": "codex-chatgpt", "agent": "pi", "model": "model"}
        with self.assertRaisesRegex(SummaryError, "无法确认当前订阅账号"):
            preview_summary(cfg, selection, self.material)
        preview = preview_summary(cfg, selection, self.material, account_identity=("account-a", "private-token-a"))
        never = []
        def request(*args):
            never.append(args)
            return 200, b"{}"
        with self.assertRaisesRegex(SummaryError, "重新预览"):
            generate_summary(cfg, selection, self.material, preview["digest"],
                             transport=request, account_identity=("account-b", "private-token-b"))
        self.assertEqual(never, [])
        self.assertNotIn("private-token-a", json.dumps(preview))

    def test_real_gateway_and_mocked_codex_zcode_subscriptions(self):
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "fixture-private-access", "account_id": "fixture-account"}}), encoding="utf-8")
        (codex_home / "models_cache.json").write_text('{"client_version":"0.164.0"}', encoding="utf-8")
        auth = {"family": "bigmodel", "kind": "individual-coding-plan", "account": "fixture-account",
                "key": "fixture-zcode-key", "base": f"http://127.0.0.1:{self.vendor.server_port}/zcode-fixture"}
        self.gateway.state.codex_base = f"http://127.0.0.1:{self.vendor.server_port}/backend-api/codex"
        cfg = {"gateway": {"port": self.gateway.server_port}, "providers": {}}
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}), \
             patch.object(zcode_subscription, "read_auth", return_value=auth), \
             patch.object(zcode_subscription, "resolve_auth", return_value=auth):
            cfg["providers"]["codex-chatgpt"] = codex_subscription.provider_config(cfg, [{"id": "model"}])
            cfg["providers"]["zcode-subscription"] = zcode_subscription.provider_config(cfg, [{"id": "model"}], auth=auth)
            exported, mapping = export_connections(cfg)
            (self.directory / "providers.json").write_bytes(packed(exported))
            (self.directory / "hub-connections.json").write_bytes(packed(mapping))
            for provider in ("codex-chatgpt", "zcode-subscription"):
                for agent in ("pi", "codex", "claude"):
                    selection = {"provider": provider, "agent": agent, "model": "model"}
                    account = (provider, "fixture-account")
                    preview = preview_summary(cfg, selection, self.material, account_identity=account)
                    result = generate_summary(cfg, selection, self.material, preview["digest"], account_identity=account)
                    self.assertEqual(result["draft"], "模拟订阅草稿", (provider, agent))
                    self.assertEqual(result["completionStatus"], "complete", (provider, agent))
        self.assertFalse(list((self.directory / "gateway-conversations").rglob("*.jsonl")))

    def test_retired_workbuddy_subscription_never_appears_as_a_summary_route(self):
        cfg = {"gateway": {"port": self.gateway.server_port}, "providers": {}}
        cfg["providers"]["workbuddy-ai"] = workbuddy.provider_config(cfg, [{"id": "model"}])
        exported, mapping = export_connections(cfg)
        self.assertEqual(exported["providers"], [])
        self.assertEqual(mapping, {})
        self.assertEqual(list_summary_routes(cfg), [])
        with self.assertRaisesRegex(SummaryError, "不可用"):
            preview_summary(cfg, {"provider": "workbuddy-ai", "agent": "pi", "model": "model"}, self.material)


if __name__ == "__main__":
    unittest.main()
