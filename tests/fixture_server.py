"""浏览器回归用服务器；只操作临时配置，平台适配器和模型检测均为桩。"""
import threading
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from support import isolated, engine, server


def discover(_provider):
    return [{"id": "deepseek/model-1", "name": "Existing model"},
            {"id": "vendor/new-model", "name": "New vision model", "input": ["text", "image"]},
            {"id": "vendor/another-new", "name": "Another new model"}]


with isolated(), ExitStack() as stack:
    for name in ("pi", "mcode", "zcode", "dsh", "claude", "cc_switch", "omp", "codex"):
        stack.enter_context(patch.object(engine.Hub, f"push_{name}"))
    # 验证逐平台失败反馈，绝不写入真实客户端配置。
    engine.Hub.push_omp.side_effect = RuntimeError("测试平台暂不可用")
    stack.enter_context(patch.object(server, "discover_models", discover))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    print(f"READY {httpd.server_port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
