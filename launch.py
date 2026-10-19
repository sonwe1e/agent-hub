#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复用本地服务，以 Edge 独立应用窗口（或默认浏览器）打开工作台。"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
            data = json.load(response)
            return data.get("app") == "agent-hub" and data.get("uiVersion", 0) >= 6
    except (OSError, ValueError, urllib.error.URLError):
        return False


def free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def ensure_server(preferred: int) -> int:
    # 老版本或其他服务占用端口时不杀进程，使用下一个空闲端口。
    for port in range(preferred, min(preferred + 10, 65536)):
        if ready(port):
            return port
        if not free(port):
            continue
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        if os.name == "nt" and pythonw.exists():
            python = pythonw
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen([str(python), str(ROOT / "server.py"), "--no-open", "--port", str(port)],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=flags)
        for _ in range(60):
            if ready(port):
                return port
            if process.poll() is not None:
                break
            time.sleep(0.15)
        raise RuntimeError("本地服务启动失败，请运行 python server.py --no-open 查看错误。")
    raise RuntimeError("未找到可用端口，请指定其他 --port。")


def open_workspace(port: int, browser: bool = False) -> None:
    url = f"http://127.0.0.1:{port}/"
    if os.name == "nt" and not browser:
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(env)
            edge = Path(base) / "Microsoft/Edge/Application/msedge.exe" if base else None
            if edge and edge.is_file():
                subprocess.Popen([str(edge), f"--app={url}", "--window-size=1440,960"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
    webbrowser.open(url)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Hub 工作台启动器")
    parser.add_argument("mode", nargs="?", choices=("gui", "ui", "serve"), default="gui")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    try:
        port = ensure_server(args.port)
        if Path(sys.executable).stem.lower() != "pythonw":
            print(f"Agent Hub: http://127.0.0.1:{port}/")
        if args.mode != "serve":
            open_workspace(port, browser=args.mode == "ui")
        return 0
    except Exception as e:
        if os.name == "nt" and Path(sys.executable).stem.lower() == "pythonw":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(e), "Agent Hub 启动失败", 0x10)
        else:
            print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
