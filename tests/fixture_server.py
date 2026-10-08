"""浏览器回归用服务器；只操作临时配置，平台适配器和模型检测均为桩。"""
import threading
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from support import isolated, engine, server
from test_api import fake_push_all


plans = {}
sequence = 0


def preview(h, only=None, *, reconcile=False):
    global sequence
    sequence += 1
    plan_id = f"fixture-{sequence}"
    plans[plan_id] = list(only or [])
    return {"planId": plan_id, "targets": plans[plan_id], "warnings": [],
            "files": [{"path": str(engine.HOME / "test-clients" / f"{name}.json"), "action": "update"} for name in plans[plan_id]]}


def apply(h, plan_id):
    if plan_id not in plans:
        raise ValueError("预览已失效，请重新预览")
    return {"transactionId": plan_id, "results": fake_push_all(h, plans.pop(plan_id))}



def discover(_provider, agent=None):
    """按连接返回不同的模型列表：不同 Key 拉到的模型集合可以不同。"""
    if agent == "claude":
        return [{"id": "claude-plan/model-1", "name": "Claude plan model"}]
    return [{"id": "deepseek/model-1", "name": "Existing model"},
            {"id": "vendor/new-model", "name": "New vision model", "input": ["text", "image"]},
            {"id": "vendor/another-new", "name": "Another new model"}]


with isolated(), ExitStack() as stack:
    for name in ("pi", "mcode", "zcode", "dsh", "claude", "cc_switch", "omp", "codex"):
        stack.enter_context(patch.object(engine.Hub, f"push_{name}"))
    stack.enter_context(patch.object(engine.Hub, "preview_push", preview, create=True))
    stack.enter_context(patch.object(engine.Hub, "apply_preview", apply, create=True))
    # 验证逐平台失败反馈，绝不写入真实客户端配置。
    engine.Hub.push_omp.side_effect = RuntimeError("测试平台暂不可用")
    stack.enter_context(patch.object(server, "discover_models", discover))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    print(f"READY {httpd.server_port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
