#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Agent Hub 桌面版打包成单个 exe。

与 server.py 共用同一份 ROUTES 分发逻辑，差别只在传输层：
桌面版不监听任何端口，页面里的 fetch 经 WebView2 WebMessage
直达进程内的 server.dispatch_api。

用法（在含 pywebview / pyinstaller 的解释器下）：
    python build_desktop.py                 # 产出 dist/AgentHub.exe
    python build_desktop.py --onedir        # 产出 dist/AgentHub/ 目录
    python build_desktop.py --skip-build    # 只打印将执行的命令
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"
ICON = UI_DIR / "agent-hub.ico"
NAME = "AgentHub"


def check_prereqs() -> list[str]:
    missing = []
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        missing.append("pyinstaller")
    try:
        import webview  # noqa: F401
    except ImportError:
        missing.append("pywebview")
    try:
        import pystray  # noqa: F401
    except ImportError:
        missing.append("pystray")
    if not ICON.is_file():
        missing.append(f"图标 {ICON}")
    return missing


def build(onefile: bool, dry: bool, name: str = NAME) -> int:
    sep = ";" if sys.platform.startswith("win") else ":"
    datas = [
        f"{UI_DIR}{sep}ui",
        f"{ROOT / 'zcode_start_prompt.json'}{sep}.",
    ]
    hidden = [
        # 本项目模块（PyInstaller 从 desktop_app 的 import 图通常能自动找到，
        # 这里显式声明，避免动态入口被漏掉）
        "server", "hub", "observation", "agent_inventory", "switch_plans",
        "sync_transaction", "codex_config", "codex_subscription", "zcode_subscription", "subscription_usage", "workbuddy_protocol",
        "gateway_runtime", "gateway_server", "gateway_observation", "upstream_transport", "discover",
        "capability_library", "quota_monitor", "workspace_services", "session_handoff", "session_titles", "gateway_titles", "handoff_summary",
        # 功能模块的真实实现；旧名称仍同时打包以兼容历史导入路径。
        "agent_hub.agents.inventory", "agent_hub.capabilities.library",
        "agent_hub.observability.store", "agent_hub.sessions.handoff",
        "agent_hub.sessions.titles", "agent_hub.sessions.gateway_titles",
        "agent_hub.sessions.summary", "agent_hub.quota.monitor", "agent_hub.quota.usage",
        "agent_hub.gateway.observation", "agent_hub.gateway.transport",
        "agent_hub.gateway.protocol", "agent_hub.workspace.services",
        # codex/dsh 的 zstd 压缩请求体靠它解码；gateway_server 只在函数内
        # 延迟 import，显式声明避免某些分析路径下漏打包。
        "zstandard",
        # 桌面运行时
        "webview", "pystray", "PIL", "PIL.Image", "clr_loader",
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        # 不用 --clean：它会让 PyInstaller 删除 build/ 与 dist/ 下的旧产物，
        # 在启用文件安全策略的环境里会被拦截而导致打包失败。--noconfirm
        # 已足以覆盖输出；需要彻底重建时用 --clean-build 手动删 build/。
        "--windowed",                       # 无控制台窗口
        "--name", name,
        "--icon", str(ICON),
        "--paths", str(ROOT),
        "--collect-all", "webview",
        "--collect-all", "pystray",
        "--collect-submodules", "webview",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "clr_loader",
    ]
    for h in hidden:
        cmd += ["--hidden-import", h]
    for d in datas:
        cmd += ["--add-data", d]
    if onefile:
        cmd += ["--onefile"]
    else:
        cmd += ["--onedir"]
    cmd += [str(ROOT / "desktop_app.py")]

    print(" ".join(cmd))
    if dry:
        return 0

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    if onefile:
        exe = ROOT / "dist" / f"{name}.exe"
    else:
        exe = ROOT / "dist" / name / f"{name}.exe"
    if exe.is_file():
        print(f"\n打包完成: {exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print(f"\n打包结束，但未找到 {exe}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 Agent Hub 桌面版")
    ap.add_argument("--onedir", action="store_true", help="输出目录而不是单文件")
    ap.add_argument("--skip-build", action="store_true", help="只打印命令")
    ap.add_argument("--name", default=NAME, help=f"输出文件名（默认 {NAME}.exe）；旧 exe 正在运行时用它避开占用")
    ap.add_argument("--clean-build", action="store_true", help="打包前删除 build/ 缓存（不动 dist/，那里可能有用户数据）")
    args = ap.parse_args()

    missing = check_prereqs()
    if missing:
        print("缺少依赖: " + ", ".join(missing), file=sys.stderr)
        print("请先: pip install pywebview pystray Pillow pyinstaller", file=sys.stderr)
        return 1

    if args.clean_build:
        shutil.rmtree(ROOT / "build", ignore_errors=True)

    return build(onefile=not args.onedir, dry=args.skip_build, name=args.name)


if __name__ == "__main__":
    sys.exit(main())
