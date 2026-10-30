#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent Hub 桌面版入口：单进程 WebView2 窗口，不监听任何端口。

与 server.py（浏览器版）共用同一份 ROUTES 分发逻辑，差别只在传输层：

  · 浏览器版  ThreadingHTTPServer + fetch 走 HTTP（127.0.0.1:8787）
  · 桌面版    WebView2 WebMessage + window.fetch 被 bridge.js
              改写为进程内直调 server.dispatch_api

因此桌面版没有任何监听端口：没有端口冲突、没有端口漂移，
也不存在「任何本机网页都能碰到配置 API」的问题——server.py:1416
那条 Host/Origin 校验在桌面版天然成立，因为没有 Host 可校验。

窗口行为：关闭主窗口不退出进程，只是隐藏到系统托盘，采集继续跑；托盘
「退出」才真正结束（停采集、停托盘、放互斥体）。**托盘不可用时不能静默
隐藏**——那会让进程活着却没有任何入口，所以改为弹确认框明确告诉用户
「关闭将退出应用」；再点一次 exe 会把已有窗口唤到前台而不是报错了事。
观测采集是 30 秒轮询的常驻任务，不应被一次误关打断。

用法：
    python desktop_app.py                 # 正常启动
    python desktop_app.py --browser       # 退回浏览器模式（复用 server.py）
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HUB_DIR = Path(__file__).resolve().parent
UI_DIR = HUB_DIR / "ui"
INDEX_HTML = UI_DIR / "index.html"
ICON_ICO = UI_DIR / "agent-hub.ico"

WINDOW_TITLE = "Agent Hub"
MUTEX_NAME = "Global\\AgentHub.Desktop.SingleInstance"
WINDOW_SIZE = (1440, 960)
MIN_SIZE = (1024, 660)
# 与骨白纸面一致，避免首帧白闪
BACKGROUND_COLOR = "#F7F3EB"

# ---- 托盘状态 ----
# pystray 在 Win32 下创建窗口/图标/菜单失败，只会在它自己的线程里抛异常，
# 调用方完全看不到（线程把异常吞掉，消息循环照样转）。所以「托盘可用」必须
# 是一个被显式确认过的事实，不能靠「import 到了」来推断——否则就会出现
# 「窗口已 hide、进程还活着、托盘里却没有图标」这种没有任何入口的状态。
TRAY_STARTING = "starting"
TRAY_READY = "ready"
TRAY_UNAVAILABLE = "unavailable"
TRAY_STOPPED = "stopped"

# 等图标真正进托盘的上限。pystray 自己的 SETUP_THREAD_TIMEOUT 也是 5s。
TRAY_READY_TIMEOUT = 5.0
# 停托盘时等消息循环线程的上限：退出路径绝不允许无限等待。
TRAY_STOP_TIMEOUT = 3.0

# ---- Win32 常量（避免散落的魔法数字）----
SW_RESTORE = 9
MB_YESNO = 0x00000004
MB_ICONWARNING = 0x00000030
MB_DEFBUTTON2 = 0x00000100
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000
IDYES = 6
ERROR_ALREADY_EXISTS = 183


def decide_close(quitting: bool, tray_available: bool) -> str:
    """决定「用户点了窗口关闭按钮」时该怎么做。纯函数，无副作用。

    Args:
        quitting: 是否已在退出流程中（托盘「退出」、--smoke 自检、restart）。
        tray_available: 托盘图标此刻是否真的可用（已创建、已显示、线程仍活着）。

    Returns:
        "allow"   放行关闭，进程随之结束；
        "hide"    只隐藏窗口并取消本次关闭，采集继续；
        "confirm" 托盘不可用，先问用户「关闭将退出应用」，确认后才退出。

    为什么不选「托盘不可用也 hide」：hide 之后窗口没有任何入口能被叫回来，
    进程还在但用户已经失去控制权，体感比直接退出更糟。宁可明确地问一句。
    """
    if quitting:
        return "allow"
    return "hide" if tray_available else "confirm"


def confirm_quit_without_tray(detail: str = "") -> bool:
    """托盘不可用时向用户确认退出。返回 True 表示用户确认退出。

    用原生 MessageBox 而不是页面内提示：走到这里往往意味着界面本身也不健康
    （WebView2 挂了、托盘跟着起不来），原生对话框不依赖页面能否渲染。
    pywebview 自己在 on_closing 里也是用 WinForms.MessageBox 问用户的
    （webview/platforms/winforms.py:403-416），所以这条路径是被支持的。
    """
    if os.name != "nt":
        # 本项目的窗口层是 WebView2，实际只在 Windows 上跑；
        # 其它平台没有托盘概念，不拦也不会卡住用户。
        return True
    text = (
        "系统托盘不可用，关闭窗口将直接退出 Agent Hub。\n\n"
        "退出后，后台的观测采集会一并停止。\n"
        "确定要退出吗？"
    )
    if detail:
        text += f"\n\n（原因：{detail}）"
    try:
        result = ctypes.windll.user32.MessageBoxW(
            0,
            text,
            WINDOW_TITLE,
            MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND | MB_TOPMOST,
        )
    except Exception:
        return True
    return result == IDYES


def _warn(message: str) -> None:
    """尽力往 stderr 写一行诊断。windowed 打包下 sys.stderr 可能是 None。"""
    try:
        if sys.stderr is not None:
            print(message, file=sys.stderr, flush=True)
    except Exception:
        pass


# WebView2 用户数据目录。必须显式指定：pywebview 默认 private_mode=True 时
# 用的是 tempfile.TemporaryDirectory().name（webview/platforms/winforms.py:762），
# 该对象随即被 GC 删除，EnsureCoreWebView2Async 会拿到一个不存在的
# UserDataFolder，表现为「窗口出来了但 loaded 事件永不触发」的间歇性挂起。
# 指定持久目录后行为确定，同时让本地存储在重启后保留。
def webview_storage_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "AgentHub" / "WebView2"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path

# --smoke 用的端到端自检脚本：在真实 WebView2 里跑一次 fetch 往返。
# 覆盖 GET 带查询、GET 普通、POST 带 body 三条路径。
SMOKE_SCRIPT = (
    "(async () => {"
    "const o = {bridge: !!(window.pywebview && window.pywebview.api && window.pywebview.api.request),"
    " fetchIsBridge: typeof window.fetch === 'function' && window.fetch.name === 'bridgedFetch'};"
    "const probe = async (label, method, path, body) => {"
    "  try {"
    "    const r = await fetch(path, {method, headers: {'Content-Type': 'application/json'},"
    "                            body: body === undefined ? undefined : JSON.stringify(body)});"
    "    const j = await r.json();"
    "    o[label] = {status: r.status, ok: r.ok, keys: Object.keys(j || {}).slice(0, 5)};"
    "  } catch (e) { o[label] = {error: String((e && e.message) || e)}; }"
    "};"
    "await probe('status', 'GET', '/api/status');"
    "await probe('observation', 'GET', '/api/observation/status');"
    "await probe('detect', 'POST', '/api/agents/detect', {});"
    "await probe('library', 'GET', '/api/capabilities');"
    "await probe('sessions', 'GET', '/api/sessions/recent');"
    "await probe('quota', 'GET', '/api/quota-monitor');"
    "o.workspaceUI = typeof renderLibrary === 'function' && typeof renderSessions === 'function';"
    "o.skillsBulkUI = typeof skillsBulkPreviewAll === 'function' && typeof skillsBulkRepreview === 'function';"
    "return o;"
    "})()"
)


# ------------------------------------------------------------
# 资源定位：源码运行与 PyInstaller 打包后都能找到 ui/
# ------------------------------------------------------------
def resource_dir() -> Path:
    frozen = getattr(sys, "frozen", False)
    if frozen:
        return Path(getattr(sys, "_MEIPASS", HUB_DIR))
    return HUB_DIR


def index_url() -> str:
    root = resource_dir()
    target = root / "ui" / "index.html"
    if not target.is_file():
        raise SystemExit(f"缺少界面文件: {target}")
    # file:// 不会触发 pywebview 的内部静态服务器（util.py:75 is_local_url），
    # 连静态资源都不开端口。
    return target.as_uri()


# ------------------------------------------------------------
# 配置发现：让 exe 自包含，不必手动把 providers.json 塞到 dist/
# ------------------------------------------------------------
def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "AgentHub"


def config_memory_path() -> Path:
    return app_data_dir() / "config-path.txt"


def exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return HUB_DIR


def remembered_config() -> Path | None:
    try:
        text = config_memory_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text) if text else None


def remember_config(path: Path) -> None:
    try:
        config_memory_path().parent.mkdir(parents=True, exist_ok=True)
        config_memory_path().write_text(str(path), encoding="utf-8")
    except OSError:
        pass


def resolve_config() -> Path | None:
    """按优先级找 providers.json。

    1. 上次记住的位置（用户明确选过一次）
    2. exe 同级目录（便携安装）
    3. exe 的父目录（exe 常被放在 <项目>/dist/ 下）
    4. 当前工作目录
    """
    for candidate in _config_candidates():
        if candidate.is_file():
            return candidate
    return None


def _config_candidates() -> list[Path]:
    candidates: list[Path] = []
    remembered = remembered_config()
    if remembered:
        candidates.append(remembered)
    base = exe_dir()
    candidates += [base / "providers.json", base.parent / "providers.json", Path.cwd() / "providers.json"]
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def choose_config_dialog() -> str | None:
    """Windows 原生文件选择对话框，让用户指定 providers.json。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPARAM),
            ("lpTemplateName", wintypes.LPCWSTR),
        ]

    buf = ctypes.create_unicode_buffer(1024)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = "Agent Hub 配置 (providers.json)\0providers.json\0所有文件\0*.*\0\0"
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 1024
    ofn.lpstrTitle = "选择 Agent Hub 的 providers.json（含供应商与模型配置）"
    ofn.Flags = 0x00080000 | 0x00001000 | 0x00000008  # 探索器样式 + 路径必须存在 + 强制存在的文件
    comdlg32 = ctypes.windll.comdlg32
    if not comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return None
    return buf.value or None


def config_folder() -> Path:
    """配置文件所在目录；配置还没找到时退回 exe 同级目录。

    引导页的「打开配置文件所在目录」按钮与托盘菜单的「打开配置目录」
    共用这一份定义，避免两处对「配置在哪」有不同的理解。
    """
    target = resolve_config()
    return target.parent if target else exe_dir()


def open_path(path: Path) -> bool:
    """用系统默认方式打开目录或文件。返回是否成功，不抛异常。"""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False


GUIDE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Agent Hub · 首次配置</title>
<style>
  :root { --paper:#F7F3EB; --surface:#FCFAF6; --ink:#1E1C19; --ink-2:#4A453E;
          --ink-3:#6E675C; --ink-4:#8B8478; --rule:#D8CFBE; --rule-strong:#C0B5A0;
          --accent:#A93F24; --accent-wash:#F6E8E1; --radius:4px; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:48px 32px;
         font:13px/1.75 "Noto Sans SC","Microsoft YaHei",sans-serif; color:var(--ink);
         background:var(--paper);
         background-image:radial-gradient(1200px 800px at 78% -10%, rgba(169,63,36,.045), transparent 60%),
                          radial-gradient(900px 600px at 8% 108%, rgba(30,28,25,.035), transparent 62%); }
  .panel { width:min(560px,100%); background:var(--surface); border:1px solid var(--rule-strong);
           border-radius:var(--radius); padding:36px 38px; }
  .eyebrow { font-size:9px; color:var(--ink-4); letter-spacing:.05em; margin-bottom:10px; }
  .eyebrow::before { content:""; display:inline-block; width:12px; height:1px;
                     background:var(--rule-strong); vertical-align:middle; margin-right:6px; }
  h1 { font-family:"Newsreader","Noto Serif SC",serif; font-size:23px; font-weight:600;
       letter-spacing:-.01em; margin:0 0 14px; }
  p { color:var(--ink-3); margin:0 0 12px; }
  code { font-family:"Cascadia Mono",Consolas,monospace; font-size:12px; background:#F2EDE4;
         padding:1px 5px; border-radius:2px; color:var(--ink-2); }
  .hint { font-size:11px; color:var(--ink-4); border-top:1px solid var(--rule);
          padding-top:14px; margin-top:20px; }
  button { min-height:34px; padding:8px 16px; border:1px solid var(--accent); border-radius:3px;
           background:var(--accent); color:#FCFAF6; font:500 13px inherit; cursor:pointer;
           transition:background .12s ease; }
  button:hover { background:#8A3119; border-color:#8A3119; }
  button.ghost { background:transparent; color:var(--ink-3); border-color:var(--rule-strong); }
  button.ghost:hover { background:#F2EDE4; color:var(--ink); }
  .actions { display:flex; gap:10px; margin-top:22px; flex-wrap:wrap; }
  #status { font-size:12px; margin-top:14px; min-height:20px; }
  #status.error { color:#8C2B22; }
  #status.ok { color:#3D6B54; }
</style></head>
<body><div class="panel">
  <div class="eyebrow">首次配置</div>
  <h1>选择你的 providers.json</h1>
  <p>Agent Hub 需要一个 <code>providers.json</code> 才能读取供应商、模型与切换方案。
     这个文件包含明文密钥，所以<strong>不会被打包进 exe</strong>——由你指定一次，之后自动记住。</p>
  <p>启动时会依次在「上次选择的位置」「exe 同级目录」「exe 上级目录」「当前工作目录」查找；
     都找不到才需要你手动选。</p>
  <div class="actions">
    <button id="pick">选择 providers.json…</button>
    <button id="open-dir" class="ghost">打开配置文件所在目录</button>
  </div>
  <div id="status"></div>
  <div class="hint">选中的路径会记在 <code>%LOCALAPPDATA%\\AgentHub\\config-path.txt</code>。
     观测数据、备份与网关配置都跟在它旁边，不会散落到别处。</div>
</div>
<script>
  const api = window.pywebview && window.pywebview.api;
  const status = document.getElementById('status');
  function say(text, kind) { status.textContent = text; status.className = kind || ''; }
  document.getElementById('pick').onclick = async () => {
    if (!api) { say('桥接未就绪，请重试。', 'error'); return; }
    say('正在打开文件选择对话框…');
    try {
      const r = await api.chooseConfig();
      if (r && r.path) {
        say('已选择 ' + r.path + '，正在重启…', 'ok');
        await api.applyConfig(r.path);
      } else {
        say(r && r.error ? r.error : '已取消选择。');
      }
    } catch (e) { say(String(e && e.message || e), 'error'); }
  };
  document.getElementById('open-dir').onclick = async () => {
    if (api) await api.openConfigDir();
  };
</script></body></html>
"""


# ------------------------------------------------------------
# 单实例：命名互斥体 + 唤醒已有窗口
#
# 互斥体只负责「别起第二个进程」，但用户真正想要的是「再点一次就把窗口叫出来」。
# 所以第二次启动不走「弹个框说已经在运行」，而是把已有窗口显示并置前。
# 隐藏到托盘的窗口依然能被 FindWindowW 找到：SW_HIDE 只改可见性，
# 不改变窗口对象的存在，而 FindWindowW 枚举的是顶层窗口，不看 IsWindowVisible。
# ------------------------------------------------------------
_MUTEX_HANDLE = None


def _prepare_win32():
    """声明 ctypes 的 restype/argtypes，返回 (user32, kernel32)。

    默认 restype 是 c_int，而 Win64 的 HANDLE 是 64 位：不声明会被截断成
    半个句柄，后续 CloseHandle/ShowWindow 就不是作用在原来那个对象上了。
    """
    from ctypes import c_bool, c_int, c_ulong, c_void_p, c_wchar_p

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    kernel32.CreateMutexW.restype = c_void_p
    kernel32.CreateMutexW.argtypes = (c_void_p, c_bool, c_wchar_p)
    kernel32.CloseHandle.restype = c_bool
    kernel32.CloseHandle.argtypes = (c_void_p,)
    kernel32.GetCurrentThreadId.restype = c_ulong
    kernel32.GetCurrentThreadId.argtypes = ()

    user32.FindWindowW.restype = c_void_p
    user32.FindWindowW.argtypes = (c_wchar_p, c_wchar_p)
    user32.ShowWindow.restype = c_bool
    user32.ShowWindow.argtypes = (c_void_p, c_int)
    user32.SetForegroundWindow.restype = c_bool
    user32.SetForegroundWindow.argtypes = (c_void_p,)
    user32.GetWindowThreadProcessId.restype = c_ulong
    user32.GetWindowThreadProcessId.argtypes = (c_void_p, c_void_p)
    user32.AttachThreadInput.restype = c_bool
    user32.AttachThreadInput.argtypes = (c_ulong, c_ulong, c_bool)
    return user32, kernel32


def single_instance_ok() -> bool:
    """尝试占住单实例互斥体。返回 False 表示已有实例在跑。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        _, kernel32 = _prepare_win32()
    except Exception:
        return True  # 拿不到 kernel32 就别拦用户启动
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True  # 创建失败不是「已有实例」，不该拦
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        # 这次调用同样创建出了句柄，必须关掉，否则每启动一次泄漏一个
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = handle
    return True


def release_single_instance() -> None:
    """释放单实例互斥体。幂等；进程退出前调用，restart 前也必须调用。"""
    global _MUTEX_HANDLE
    handle, _MUTEX_HANDLE = _MUTEX_HANDLE, None
    if not handle or os.name != "nt":
        return
    try:
        _, kernel32 = _prepare_win32()
        kernel32.CloseHandle(handle)
    except Exception:
        pass


def find_main_window(title: str = WINDOW_TITLE):
    """按标题找顶层窗口句柄；找不到返回 None。"""
    if os.name != "nt":
        return None
    try:
        user32, _ = _prepare_win32()
    except Exception:
        return None
    try:
        return user32.FindWindowW(None, title) or None
    except Exception:
        return None


def shell_taskbar_present() -> bool:
    """系统任务栏（Explorer 的 Shell_TrayWnd）是否存在。

    托盘图标的宿主就是任务栏的通知区，没有它就不可能有图标。这条前置判断
    补的是 pystray 检查不到的漏洞：它调 Shell_NotifyIcon 时并不看返回值
    （pystray/_win32.py:337），所以「设了 visible」不等于「图标真的在」。
    判断不出来时按「不可用」处理——宁可多问用户一句，也不要静默藏窗口。
    """
    if os.name != "nt":
        return False
    try:
        user32, _ = _prepare_win32()
    except Exception:
        return False
    try:
        return bool(user32.FindWindowW("Shell_TrayWnd", None))
    except Exception:
        return False


def _force_foreground(hwnd) -> None:
    """把窗口显示出来并置前。尽力而为，失败不影响「已有实例」的事实。"""
    try:
        user32, kernel32 = _prepare_win32()
        # SW_RESTORE 同时负责「显示」和「激活」：对 SW_HIDE 过的窗口
        # 等同于把它重新显示出来，对最小化的窗口则是还原。
        user32.ShowWindow(hwnd, SW_RESTORE)
        if user32.SetForegroundWindow(hwnd):
            return
        # Windows 有前台锁：非前台进程调用 SetForegroundWindow 会被静默忽略。
        # 临时把本线程的输入队列挂到目标窗口线程上，绕过限制后立刻解挂。
        target_tid = user32.GetWindowThreadProcessId(hwnd, None)
        current_tid = kernel32.GetCurrentThreadId()
        if target_tid and target_tid != current_tid:
            user32.AttachThreadInput(current_tid, target_tid, True)
            try:
                user32.SetForegroundWindow(hwnd)
            finally:
                user32.AttachThreadInput(current_tid, target_tid, False)
    except Exception:
        pass


def wake_existing_window(title: str = WINDOW_TITLE, attempts: int = 8, delay: float = 0.25) -> bool:
    """把已在运行的 Agent Hub 主窗口唤到前台。成功返回 True。

    带重试：上一个进程可能正在 restart（互斥体还占着，窗口还没重建），
    这时不该立刻弹「已经在运行」。等一会儿窗口通常就出现了。
    """
    if os.name != "nt":
        return False
    total = max(1, attempts)
    for attempt in range(total):
        hwnd = find_main_window(title)
        if hwnd:
            _force_foreground(hwnd)
            return True
        if attempt + 1 < total:
            time.sleep(max(0.0, delay))
    return False


def notify_already_running() -> None:
    """兜底提示：进程在跑，但窗口怎么都找不回来。"""
    message = (
        "Agent Hub 已经在运行，但没能把它的窗口唤到前台。\n\n"
        "请从系统托盘图标打开它，或在任务管理器里结束 AgentHub 后重试。"
    )
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(0, message, WINDOW_TITLE, 0x40)
    else:
        _warn("Agent Hub 已在运行。")


# ------------------------------------------------------------
# 采集与网关：与 server.main() 同一套生命周期
# ------------------------------------------------------------
class Services:
    def __init__(self, app=None) -> None:
        import server  # noqa: PLC0415  延后导入，--help 不需要依赖

        self.server = server
        self.collector = server.observations()
        self.gateway = server.gateway()
        self.workspace = server.workspace_services()
        if app is not None:
            self.workspace.quota.notify = app._notify_quota

    def start(self) -> None:
        self.collector.start()
        self.gateway.watch(self.collector.settings)
        self.workspace.start()

    def close(self) -> None:
        try:
            self.workspace.close()
        except Exception:
            pass
        try:
            self.gateway.close()
        except Exception:
            pass
        try:
            self.collector.close()
        except Exception:
            pass


# ------------------------------------------------------------
# 暴露给 WebView2 的进程内 API
# pywebview 每次 js_api 调用都在独立线程执行（webview/util.py:335），
# 因此检测模型、导入供应商这类慢任务不会卡住界面。
# ------------------------------------------------------------
class DesktopApi:
    def __init__(self, app: "DesktopApp") -> None:
        self._app = app

    def request(self, method, path, body=None):
        method = str(method or "GET").upper()
        path = str(path or "/")
        if not path.startswith("/"):
            path = "/" + path
        if not isinstance(body, dict):
            body = None
        split = urlsplit(path)
        status, payload = self._app.services.server.dispatch_api(
            method, split.path, parse_qs(split.query), body
        )
        return {"status": status, "body": payload}

    def appInfo(self):
        return {
            "app": "agent-hub",
            "desktop": True,
            "title": WINDOW_TITLE,
            "index": str(INDEX_HTML),
            "singleInstance": True,
        }

    def quit(self):
        self._app.quit()
        return {"ok": True}

    def openExternal(self, url):
        """在系统默认浏览器打开外链。

        桌面壳内没有地址栏，OAuth 一类必须落到系统浏览器的流程走这里。
        只允许 http/https，避免把本地路径交给 shell。
        """
        url = str(url or "")
        if not (url.startswith("http://") or url.startswith("https://")):
            return {"ok": False, "error": "仅支持 http/https 链接"}
        try:
            if os.name == "nt":
                os.startfile(url)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", url])
            return {"ok": True}
        except Exception as e:  # pragma: no cover - 平台相关
            return {"ok": False, "error": str(e)}

    # ---- 首次配置引导 ----
    def chooseConfig(self):
        """弹出原生文件对话框，让用户指定 providers.json。"""
        try:
            path = choose_config_dialog()
        except Exception as e:
            return {"ok": False, "error": f"对话框打开失败: {e}"}
        if not path:
            return {"ok": False, "error": "已取消选择"}
        if Path(path).name != "providers.json":
            return {"ok": False, "error": "请选择名为 providers.json 的配置文件"}
        return {"ok": True, "path": path}

    def applyConfig(self, path):
        """记住选中的配置路径并重启进程，让所有模块读同一份配置。"""
        path = str(path or "")
        target = Path(path)
        if not target.is_file():
            return {"ok": False, "error": f"文件不存在: {path}"}
        remember_config(target)
        # 直接重启：hub.HUB_CONFIG / server.HUB_CONFIG / 观测库 root 都在 import 期
        # 就已确定，热替换会留下半初始化状态，重启是最稳的路径。
        self._app.restart()
        return {"ok": True}

    def openConfigDir(self):
        """打开配置文件所在目录（或 exe 同级目录）方便用户放置。"""
        folder = config_folder()
        if open_path(folder):
            return {"ok": True}
        return {"ok": False, "error": f"打不开目录: {folder}"}


# ------------------------------------------------------------
# 应用本体
# ------------------------------------------------------------
class DesktopApp:
    def __init__(self, args: argparse.Namespace, config_path: Path | None = None, guided: bool = False) -> None:
        self.args = args
        self.config_path = config_path
        self.guided = guided
        self.services: Services | None = None
        self.window = None
        self.icon = None
        self.quitting = False
        # 托盘可用性：必须显式确认（见 _start_tray / _run_tray）
        self._tray_state = TRAY_UNAVAILABLE
        self._tray_error: BaseException | None = None
        self._tray_ready = threading.Event()
        self._tray_thread: threading.Thread | None = None
        # 退出/收尾幂等：重复点「退出」、关闭确认与托盘退出并发都走这里
        self._teardown_lock = threading.Lock()
        self._teardown_done = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        # 关闭确认对话框的重入保护：closing 事件是同步派发并在 UI 线程上等
        # 返回值，MessageBox 会开嵌套消息循环，用户此刻再点一次 X 会递归进来。
        self._close_dialog_open = False
        # 「已收在托盘」气泡只提示一次：每个进程生命周期提醒一遍就够，
        # 之后每次点 X 再弹气泡就是打扰。
        self._hide_notified = False
        self._quota_pending = []
        self._quota_pending_lock = threading.Lock()
        self._tray_menu_stop = threading.Event()

    def _services(self) -> Services:
        """懒构造：引导模式下不启动采集与网关，等配置就绪重启后再起。"""
        if self.services is None:
            self.services = Services(self)
        return self.services

    # ---- 托盘 ----
    def _load_icon(self):
        from PIL import Image  # noqa: PLC0415

        path = resource_dir() / "ui" / "agent-hub.ico"
        if path.is_file():
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                pass
        # 兜底：terracotta 圆点，与品牌标记同色
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        from PIL import ImageDraw  # noqa: PLC0415

        ImageDraw.Draw(img).ellipse([6, 6, 58, 58], fill=(217, 119, 87, 255))
        return img

    def _start_tray(self) -> bool:
        """起托盘图标，返回托盘是否**真的**可用。

        旧实现 import 失败就静默 return，窗口随后照样被 hide，于是进程还活着
        但托盘里没有图标、窗口又找不回来——用户体感就是「关了程序还占着」。
        现在把「图标真的显示出来了」当成必须确认的事实：pystray 的自定义
        setup 回调在消息循环就绪后才会被调用，在那里设 visible 并置位事件。
        """
        if self.quitting:
            return False
        try:
            import pystray  # noqa: PLC0415
        except Exception as exc:
            self._tray_error = exc
            self._tray_state = TRAY_UNAVAILABLE
            _warn(f"[tray] 无法导入 pystray，托盘不可用: {type(exc).__name__}: {exc}")
            return False

        if not shell_taskbar_present():
            # 没有任务栏 = 放不下图标 = hide 之后必然找不回来
            self._tray_error = RuntimeError("找不到系统任务栏（Explorer 未运行？）")
            self._tray_state = TRAY_UNAVAILABLE
            _warn(f"[tray] {self._tray_error}，托盘不可用")
            return False

        menu = pystray.Menu(
            pystray.MenuItem("显示 Agent Hub", self._tray_show, default=True),
            pystray.MenuItem("继续最近会话", self._tray_continue_recent),
            pystray.MenuItem(lambda _item: self._tray_quota_summary(), self._tray_quota),
            pystray.MenuItem("切换已有方案", pystray.Menu(lambda: self._tray_plan_items(pystray))),
            pystray.MenuItem(
                lambda _item: "暂停采集" if self._collecting() else "开始采集",
                self._tray_toggle_collecting,
            ),
            pystray.MenuItem("打开配置目录", self._tray_open_config_dir),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_quit),
        )
        try:
            icon = pystray.Icon(WINDOW_TITLE, self._load_icon(), WINDOW_TITLE, menu)
        except Exception as exc:
            self._tray_error = exc
            self._tray_state = TRAY_UNAVAILABLE
            _warn(f"[tray] 创建托盘图标失败: {type(exc).__name__}: {exc}")
            return False

        self.icon = icon
        # 构造期间用户可能已经点了退出：别把图标重新塞回托盘里
        if self.quitting:
            self._stop_tray()
            return False

        self._tray_ready = threading.Event()
        self._tray_state = TRAY_STARTING
        self._tray_thread = threading.Thread(
            target=self._run_tray, name="agenthub-tray", daemon=True
        )
        self._tray_thread.start()

        if not self._tray_ready.wait(TRAY_READY_TIMEOUT):
            self._tray_state = TRAY_UNAVAILABLE
            if self._tray_error is None:
                self._tray_error = TimeoutError(
                    f"托盘图标在 {TRAY_READY_TIMEOUT:.0f}s 内没有就绪"
                )
        if self._tray_state != TRAY_READY:
            # 起不来就收干净，别留一个没有图标的空转消息循环
            self._stop_tray()
            if self.quitting:
                return False
            if self._tray_error is None:
                # 消息循环没就绪就退了（run 正常返回但 setup 没被调用）。
                # 确认框里必须给得出一个原因，不能是空。
                self._tray_error = RuntimeError("托盘消息循环未能就绪就退出了")
            _warn(f"[tray] 托盘不可用，关闭窗口前会先确认: {self._tray_error_text()}")
            self._tray_state = TRAY_UNAVAILABLE
            return False
        return True

    def _run_tray(self) -> None:
        """托盘消息循环。跑在自己的守护线程里，异常一律降级为「不可用」。"""
        icon = self.icon
        if icon is None:
            self._tray_state = TRAY_UNAVAILABLE
            self._tray_ready.set()
            return

        def setup(icon_) -> None:
            # 传了自定义 setup，pystray 就不会替你设 visible（_base.py:207），
            # 不设的话图标根本不会出现在托盘里，而 run() 照样一直转。
            try:
                icon_.visible = True
                self._tray_state = TRAY_READY
                self._flush_quota_notifications()
                self._tray_menu_stop.clear()
                threading.Thread(target=self._refresh_tray_menu, name="agenthub-tray-menu", daemon=True).start()
            except Exception as exc:
                self._tray_error = exc
                self._tray_state = TRAY_UNAVAILABLE
                try:
                    icon_.stop()  # 从 setup 线程调用不会自锁（_base.py:251）
                except Exception:
                    pass
            finally:
                self._tray_ready.set()

        try:
            icon.run(setup=setup)
        except Exception as exc:
            self._tray_error = exc
            self._tray_state = TRAY_UNAVAILABLE
            _warn(f"[tray] 托盘消息循环异常退出: {type(exc).__name__}: {exc}")
        finally:
            if self._tray_state in (TRAY_STARTING, TRAY_READY):
                self._tray_state = TRAY_STOPPED
            self._tray_ready.set()

    def _stop_tray(self, timeout: float = TRAY_STOP_TIMEOUT) -> None:
        """停托盘。幂等，且从不无限等待——退出路径不能被托盘拖住。"""
        self._tray_menu_stop.set()
        icon, self.icon = self.icon, None
        if icon is not None:
            try:
                # pystray 内部对 setup 线程的 join 本身有 5s 上限（_base.py:251）
                icon.stop()
            except Exception:
                pass
        thread, self._tray_thread = self._tray_thread, None
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=timeout)
        self._tray_state = TRAY_STOPPED

    def _tray_available(self) -> bool:
        """托盘此刻是否真的能用：图标已创建、已显示、线程还活着。"""
        if self.icon is None or self._tray_state != TRAY_READY:
            return False
        thread = self._tray_thread
        return thread is not None and thread.is_alive()

    def _tray_error_text(self) -> str:
        error = self._tray_error
        if error is None:
            return ""
        return f"{type(error).__name__}: {error}"

    def _notify_hidden_once(self) -> None:
        """第一次收进托盘时弹一次气泡，告诉用户它还在跑。

        为什么需要：用户的原话是「点关闭就直接退出了，没挂在后台」——窗口一消失，
        他第一反应就是程序没了。托盘图标默认折叠在通知区的「^」里，看不见，
        所以必须在窗口消失的同一时刻给一句人话，否则修了机制也没修掉这个误解。

        每个进程生命周期只提示一次：标志位先置位再发，失败也不重试，
        避免每次点 X 都弹一次。notify 在部分 Windows 上会失败（通知被组策略/
        专注助手关掉、通知区异常），失败只记一行日志，绝不能影响隐藏本身。
        """
        if self._hide_notified:
            return
        self._hide_notified = True
        icon = self.icon
        if icon is None:
            return
        try:
            icon.notify(
                "点击托盘图标可恢复窗口；右键可退出",
                "Agent Hub 仍在后台运行",
            )
        except Exception as exc:
            _warn(f"[tray] 后台运行气泡发送失败（不影响隐藏）: {type(exc).__name__}: {exc}")

    # ---- 托盘菜单回调（运行在托盘线程上）----
    def _tray_show(self, _icon=None, _item=None) -> None:
        self._restore()

    def _notify_quota(self, event) -> None:
        title = str(event.get("title") or "额度提醒")
        message = str(event.get("message") or "")
        with self._quota_pending_lock:
            if self.icon is None or self._tray_state != TRAY_READY:
                self._quota_pending.append((title, message))
                self._quota_pending = self._quota_pending[-20:]
                return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass  # Event remains visible in the monitor status.

    def _flush_quota_notifications(self) -> None:
        with self._quota_pending_lock:
            pending, self._quota_pending = self._quota_pending, []
        for title, message in pending:
            try:
                if self.icon is not None:
                    self.icon.notify(message, title)
            except Exception:
                pass

    def _show_section(self, section) -> None:
        self._restore()
        if self.window:
            try:
                self.window.evaluate_js("location.hash=" + json.dumps(section))
            except Exception:
                pass

    def _tray_quota(self, _icon=None, _item=None) -> None:
        threading.Thread(target=self._show_section, args=("settings",), daemon=True).start()

    def _tray_plans(self, _icon=None, _item=None) -> None:
        threading.Thread(target=self._show_section, args=("models",), daemon=True).start()

    def _tray_quota_summary(self) -> str:
        try:
            accounts = self._services().workspace.quota.status()["accounts"]
            parts = []
            for key, label in (("codex-chatgpt", "Codex"), ("zcode-subscription", "ZCode")):
                account = accounts.get(key) or {}
                if not account.get("connected"):
                    continue
                quota = account.get("quota") or {}
                values = [w["usedPercent"] for w in quota.get("windows", [])
                          if isinstance(w.get("usedPercent"), (float, int))]
                remaining = max(0, 100-max(values)) if values else None
                parts.append(label + (" 未知" if remaining is None else f" {remaining:.0f}%") +
                             ("（旧）" if account.get("stale") else ""))
            return "额度 · " + (" / ".join(parts) if parts else "未知")
        except Exception:
            return "额度 · 未知"

    def _tray_plan_items(self, pystray):
        try:
            rows = self._services().server.api_switch_plans()["store"]["plans"]
        except Exception:
            rows = []
        if not rows:
            return (pystray.MenuItem("打开方案管理", self._tray_plans),)
        def choose(plan_id):
            def callback(_icon=None, _item=None):
                threading.Thread(target=self._show_selected_plan, args=(plan_id,), daemon=True).start()
            return callback
        return tuple(pystray.MenuItem(str(row.get("name") or row["id"]), choose(row["id"])) for row in rows)

    def _show_selected_plan(self, plan_id):
        self._restore()
        if self.window:
            try:
                self.window.evaluate_js("if(window.agentHubOpenPlan){window.agentHubOpenPlan(" + json.dumps(plan_id) + ")}else{location.hash='models'}")
            except Exception:
                self._show_section("models")

    def _refresh_tray_menu(self):
        while not self._tray_menu_stop.wait(30):
            try:
                if self.icon is not None:
                    self.icon.update_menu()
            except Exception:
                pass

    def _tray_continue_recent(self, _icon=None, _item=None) -> None:
        def run():
            try:
                rows = self._services().server.api_recent_sessions()["sessions"]
                if not rows:
                    raise ValueError("没有已索引的会话")
                row = rows[0]
                self._services().server.api_resume({"agent": row["agent"], "sessionId": row["sessionId"]})
            except Exception:
                # Unavailable/moved sessions stay visible for the user to select.
                self._show_section("sessions")
        threading.Thread(target=run, name="agenthub-recent-session", daemon=True).start()

    def _tray_toggle_collecting(self, icon=None, _item=None) -> None:
        try:
            # 走与 PUT /api/observation/settings 相同的设置通道
            self._services().server.api_observation_settings(
                {"enabled": not self._collecting()}
            )
        except Exception:
            pass
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass

    def _tray_open_config_dir(self, _icon=None, _item=None) -> None:
        open_path(config_folder())

    def _tray_quit(self, _icon=None, _item=None) -> None:
        """托盘菜单「退出」。

        不在这里直接 quit()：window.destroy() 是一次阻塞到 UI 线程的 Invoke，
        在托盘消息循环里等它会把循环按停，icon.stop() 之后 run() 迟迟不返回。
        交给独立线程，菜单回调立刻返回，消息循环照常处理 WM_STOP。
        """
        threading.Thread(target=self.quit, name="agenthub-quit", daemon=True).start()

    def _collecting(self) -> bool:
        try:
            return bool(self._services().collector.settings().get("enabled", True))
        except Exception:
            return True

    def _restore(self) -> None:
        if not self.window:
            return
        try:
            self.window.show()
            self.window.restore()
        except Exception:
            pass

    # ---- 窗口 ----
    def _on_closing(self):
        """用户点了窗口关闭按钮。

        返回 True = 放行关闭，返回 False = 取消关闭。pywebview 的取消语义在
        webview/event.py:60-63：收集所有 handler 的返回值，只要有一个是 False
        就返回 True，winforms 侧据此设 args.Cancel（platforms/winforms.py:400-402）。
        closing 事件是带锁同步派发的（webview/window.py:163 → Event(self, True)），
        所以这里的返回值确实会被采用——代价是本函数在 UI 线程上执行，
        不能做慢事。
        """
        action = decide_close(self.quitting, self._tray_available())

        if action == "allow":
            return True

        if action == "hide":
            try:
                self.window.hide()
            except Exception:
                # 藏不起来就别拦着：否则留下一个关不掉的窗口，比退出更糟
                return True
            # 只在「真的藏进托盘了」之后提示一次，见 _notify_hidden_once
            self._notify_hidden_once()
            return False  # 取消关闭，进程与采集继续

        # action == "confirm"：托盘不可用，hide 之后没有入口能把窗口叫回来，
        # 所以必须让用户明确知道「关闭 = 退出」。
        if self._close_dialog_open:
            return False  # 已经在问了，别在嵌套消息循环里再叠一个框
        self._close_dialog_open = True
        try:
            confirmed = confirm_quit_without_tray(self._tray_error_text())
        finally:
            self._close_dialog_open = False
        if not confirmed:
            return False  # 用户改主意：窗口保持可见（本来就没 hide 过）
        self.quitting = True  # 放行这次关闭，收尾统一走 _shutdown()
        return True

    def _on_shown(self):
        pass

    def run(self, smoke: bool = False) -> int:
        import webview  # noqa: PLC0415

        # 配置在 main() 里已解析并经环境变量注入（必须在 import server 之前），
        # 这里只决定是否启动采集与网关。配置缺失时走首次引导页，
        # 避免一打开就报「暂时无法加载工作空间」。
        if self.config_path:
            os.environ["AGENTHUB_CONFIG"] = str(self.config_path)
        if not self.guided:
            self._services().start()

        api = DesktopApi(self)
        url = index_url()
        if self.guided:
            # 引导页内联在窗口里，不走文件资源
            self.window = webview.create_window(
                WINDOW_TITLE,
                html=GUIDE_HTML,
                width=760,
                height=620,
                min_size=(560, 520),
                resizable=True,
                js_api=api,
                text_select=True,
                confirm_close=False,
                background_color=BACKGROUND_COLOR,
            )
        else:
            self.window = webview.create_window(
                WINDOW_TITLE,
                url,
                width=WINDOW_SIZE[0],
                height=WINDOW_SIZE[1],
                min_size=MIN_SIZE,
                resizable=True,
                js_api=api,
                text_select=True,
                confirm_close=False,
                background_color=BACKGROUND_COLOR,
            )
        self.window.events.closing += self._on_closing
        self._loaded = threading.Event()
        self.window.events.loaded += self._on_loaded

        if smoke:
            self._arm_smoke()
        elif not self.guided:
            # 托盘要等「图标真的出现」才敢算可用，最多 5s；放在后台线程里做，
            # 别让窗口显示跟着一起等。若用户在这 1~2 秒内就关窗口，
            # _tray_available() 还是 False，会走确认框——安全的一侧。
            threading.Thread(
                target=self._start_tray, name="agenthub-tray-start", daemon=True
            ).start()
        # WebView2 偶发在控制器就绪前就完成导航，pywebview 的 js_api 注入
        # 随之丢失（表现为窗口出来了但界面全空白、loaded 永不触发）。
        # 这里用重导航兜底，见 _ensure_loaded。
        threading.Thread(target=self._ensure_loaded, kwargs={"smoke": smoke}, daemon=True).start()

        try:
            webview.start(private_mode=False, storage_path=str(webview_storage_path()))
        finally:
            self._shutdown()
        return 0

    def _on_loaded(self) -> None:
        self._loaded.set()

    def _ensure_loaded(self, timeout: float = 6.0, attempts: int = 4, smoke: bool = False) -> None:
        """确保 pywebview 的 js_api 注入完成；失败则重导航，最多 attempts 次。"""
        if getattr(self, "guided", False):
            return  # 引导页是内联 html，重导航没有意义
        url = index_url()
        for _ in range(attempts):
            if self._loaded.wait(timeout) or self.quitting:
                return
            try:
                self.window.load_url(url)
            except Exception:
                return
        if not self.quitting and not smoke:
            self._show_load_failure()

    def _show_load_failure(self) -> None:
        """所有重试都失败：把空白窗口换成一条可操作的错误提示。"""
        try:
            self.window.load_html(
                "<!DOCTYPE html><meta charset='utf-8'>"
                "<body style=\"font:14px/1.8 'Noto Sans SC','Microsoft YaHei',sans-serif;"
                "background:#F7F3EB;color:#1E1C19;margin:0;display:grid;place-items:center;"
                "min-height:100vh;text-align:center;padding:32px\">"
                "<div><h1 style=\"font-family:'Noto Serif SC',serif;font-size:22px;margin:0 0 12px\">"
                "Agent Hub 界面加载失败</h1>"
                "<p style=\"color:#6E675C;margin:0 0 18px\">WebView2 运行时没有在预期时间内就绪。<br>"
                "请确认已安装 Microsoft Edge WebView2 Runtime，然后重新启动。</p>"
                "<p style=\"font-size:12px;color:#A39A8E\">"
                "也可以在项目目录运行 <code>python server.py</code> 使用浏览器模式。</p></div>"
            )
        except Exception:
            pass

    # ---- 端到端自检 ----
    def _arm_smoke(self) -> None:
        """起窗后在真实 WebView2 里跑一次 fetch 往返，拿到结果即退出。"""
        import json  # noqa: PLC0415
        import threading  # noqa: PLC0415

        finished = threading.Event()
        outcome: dict = {}

        def on_loaded():
            def cb(value):
                outcome["value"] = value
                finished.set()

            try:
                self.window.evaluate_js(SMOKE_SCRIPT, cb)
            except Exception as e:  # pragma: no cover
                outcome["error"] = repr(e)
                finished.set()

        def watchdog():
            if not finished.wait(90):
                outcome["error"] = "smoke timeout"
            try:
                print(json.dumps(outcome, ensure_ascii=False, default=str), flush=True)
            except (OSError, AttributeError):
                pass
            finally:
                self.quit()

        self.window.events.loaded += on_loaded
        threading.Thread(target=watchdog, daemon=True).start()

    def _teardown_window(self) -> None:
        """停托盘 + 销毁窗口，让 webview.start() 返回。幂等。

        重复点托盘「退出」、关闭确认与托盘退出并发，都只会真正执行一次。
        """
        with self._teardown_lock:
            if self._teardown_done:
                return
            self._teardown_done = True
        self.quitting = True
        self._stop_tray()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

    def quit(self) -> None:
        """真正退出：停托盘、关窗口，剩余收尾交给 _shutdown()。幂等。"""
        self._teardown_window()

    def restart(self) -> None:
        """记住新配置后重启进程，让所有模块读同一份配置。

        不热替换 hub.HUB_CONFIG / server.HUB_CONFIG：观测库 root、网关 root
        都在 import 期就已确定，热替换会留下半初始化状态。重启是最稳的路径，
        对冻结 exe 同样有效（sys.executable 就是 exe 本身）。
        """
        # 先置标志再销毁窗口：destroy 会让主线程立刻返回 webview.start()，
        # 反过来的话 _shutdown() 可能抢在标志置位之前跑，重启就静默变成退出。
        self._restart_requested = True
        self._teardown_window()

    def _shutdown(self) -> None:
        """webview.start() 返回后的唯一收尾点。重复调用安全。"""
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self.quitting = True
        self._stop_tray()
        if self.services is not None:
            self.services.close()
        # 先放互斥体再 execv：否则新进程会看到「已有实例」而被当成第二次启动。
        release_single_instance()
        if getattr(self, "_restart_requested", False):
            # 在 webview.start() 返回后、进程退出前换上新进程
            os.execv(sys.executable, [sys.executable, *sys.argv])


# ------------------------------------------------------------
def selftest() -> int:
    """不起窗口的打包自检：校验 bundle 资源 + ROUTES 进程内分发。

    不依赖 WebView2，因此结果确定，适合在打包后立刻跑：
        dist\\AgentHub.exe --selftest
    """
    import json

    out: dict = {"frozen": bool(getattr(sys, "frozen", False)), "resourceDir": str(resource_dir())}
    problems: list[str] = []

    # 0. 配置发现：报告解析结果与候选位置，便于判断为何走/不走引导页
    found = resolve_config()
    out["config"] = {
        "resolved": str(found) if found else None,
        "remembered": str(remembered_config()) if remembered_config() else None,
        "candidates": [str(c) for c in _config_candidates()],
        "memory": str(config_memory_path()),
    }

    # 1. bundle 里的界面资源
    root = resource_dir()
    required = ["ui/index.html", "ui/styles.css", "ui/fonts.css", "ui/bridge.js", "ui/app.js",
                "ui/features/workbench/workbench.js", "ui/features/workbench/daily.css",
                "ui/features/models/models.css",
                "ui/features/subscriptions/subscription_monitor.js",
                "ui/features/subscriptions/codex_subscription.js", "ui/features/subscriptions/zcode_subscription.js",
                "ui/features/workspace/workspace-features.js", "ui/features/workspace/workspace-features.css",
                "ui/features/capabilities/skills-bulk.js", "ui/agent-hub.ico", "zcode_start_prompt.json"]
    for rel in required:
        p = root / rel
        if not p.is_file():
            problems.append(f"缺少资源: {rel}")
    woff2 = sorted((root / "ui" / "fonts").glob("*.woff2")) if (root / "ui" / "fonts").is_dir() else []
    out["fonts"] = [f.name for f in woff2]
    if not woff2:
        problems.append("ui/fonts/ 下没有 woff2 字体")

    # 2. index.html 必须用相对路径（file:// 下绝对路径会 404）
    html = (root / "ui" / "index.html").read_text(encoding="utf-8") if (root / "ui" / "index.html").is_file() else ""
    for bad in ('src="/', 'href="/'):
        if bad in html:
            problems.append(f"index.html 仍含绝对路径 {bad}……（file:// 下会加载失败）")
    out["bridgeLoaded"] = 'src="bridge.js"' in html

    # 3. ROUTES 进程内分发（不经过任何 HTTP）
    # 3. ROUTES 进程内分发（不经过任何 HTTP）
    # 与 main() 完全同序：先解析配置并注入环境变量，再 import server。
    # hub.py 在模块级就定下 HUB_CONFIG，顺序错了就会读到空配置。
    if found:
        os.environ["AGENTHUB_CONFIG"] = str(found)
    try:
        import server

        out["routes"] = len(server.ROUTES)
        out["hubConfig"] = str(server.HUB_CONFIG)
        for method, path in (("GET", "/api/status"), ("GET", "/api/agents"), ("GET", "/api/providers"),
                             ("GET", "/api/subscriptions/activity"), ("GET", "/api/capabilities"),
                             ("GET", "/api/sessions/recent"), ("GET", "/api/quota-monitor"),
                             ("GET", "/api/sessions/summary/routes")):
            query = {"provider": ["codex-chatgpt"]} if path == "/api/subscriptions/activity" else None
            status, payload = server.dispatch_api(method, path, query=query)
            out[path] = {"status": status, "keys": list(payload)[:4] if isinstance(payload, dict) else None}
            if status != 200:
                problems.append(f"{method} {path} -> {status}")
    except SystemExit as e:
        # 找不到 providers.json：GUI 模式下会走首次引导页，不算打包失败
        problems.append(f"配置不可用（{e}）。GUI 模式下将进入首次配置引导。")
    except Exception as e:
        problems.append(f"导入或分发失败: {type(e).__name__}: {e}")

    # 4. 桌面运行时依赖 + 网关子系统（用户要求「网关类的放在软件里」）
    # zstandard 单列：codex/dsh 的 zstd 压缩请求体缺它会在运行期崩线程。
    for mod in ("webview", "pystray", "PIL", "zstandard", "cryptography.hazmat.primitives.ciphers.aead", "gateway_runtime", "gateway_server", "agent_hub.gateway.observation", "agent_hub.sessions.gateway_titles", "codex_subscription", "zcode_subscription", "agent_hub.quota.usage", "agent_hub.capabilities.library", "agent_hub.sessions.handoff", "agent_hub.sessions.titles", "agent_hub.workspace.services", "agent_hub.quota.monitor", "agent_hub.sessions.summary", "agent_hub.observability.store"):
        try:
            __import__(mod)
        except Exception as e:
            problems.append(f"缺少运行时依赖 {mod}: {e}")
    try:
        from gateway_runtime import Runtime  # noqa: PLC0415

        out["gateway"] = {"runtime": "gateway_runtime.Runtime", "importable": True}
    except Exception as e:
        out["gateway"] = {"importable": False, "error": str(e)}
        problems.append(f"网关运行时不可用: {e}")

    out["problems"] = problems
    print(json.dumps(out, ensure_ascii=False, default=str))
    if problems:
        print(f"\nselftest 失败（{len(problems)} 项）:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    print("\nselftest 通过：bundle 资源完整、路由进程内分发正常、桌面依赖齐备")
    return 0


def main(argv: list[str] | None = None) -> int:
    global MUTEX_NAME
    raw = list(sys.argv[1:] if argv is None else argv)
    # 隐藏入口：sync_transaction 在打包环境下用 exe 自己起编译子进程。
    # 必须在 argparse 之前拦截，否则会被当成未知参数拒绝，表现为
    # 「Isolated native configuration compilation failed」。
    if raw and raw[0] == "--compile-worker":
        import json

        from sync_transaction import _worker

        if len(raw) != 5:
            print(json.dumps({"ok": False, "error": "compile worker: 参数数量不对"}))
            return 1
        return _worker(Path(raw[1]), Path(raw[2]), json.loads(raw[3]),
                       reconcile=json.loads(raw[4]))

    # 隐藏入口：gateway_runtime 在打包环境下用 exe 自己起网关子进程。
    # 参数与 gateway_server.py 的 argparse 完全一致，仅多一个入口标记。
    if raw and raw[0] == "--gateway-worker":
        from gateway_server import main as gateway_main

        sys.argv = [sys.argv[0], *raw[1:]]
        gateway_main()
        return 0

    parser = argparse.ArgumentParser(description="Agent Hub 桌面工作台（无端口，单进程）")
    parser.add_argument("--browser", action="store_true", help="退回浏览器模式（等价于运行 server.py）")
    parser.add_argument("--smoke", action="store_true", help="起窗后跑一次端到端自检并退出（需 GUI）")
    parser.add_argument("--selftest", action="store_true", help="不起窗口的打包自检：校验资源与路由分发")
    parser.add_argument("--port", type=int, default=8787, help="仅 --browser 模式使用")
    args = parser.parse_args(raw)
    if args.smoke:
        # A test window must not wake, claim or interfere with a real running
        # instance. The caller is responsible for using an isolated config.
        MUTEX_NAME = f"Local\\AgentHub.Desktop.Smoke.{os.getpid()}"

    if args.selftest:
        # windowed 打包下 stdout/stderr 可能没有编码信息；强制 UTF-8 输出，
        # 否则中文在 GBK 代码页下会变成乱码。
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        return selftest()

    if args.browser:
        import server  # noqa: PLC0415

        sys.argv = [sys.argv[0], "--port", str(args.port)]
        return server.main()

    if not single_instance_ok():
        # 第二次启动：唤醒已有窗口并置前，本进程立刻退出。
        # 隐藏到托盘的窗口同样能被找到——SW_HIDE 只改可见性，不改变窗口对象，
        # 而 FindWindowW 枚举顶层窗口时不看 IsWindowVisible。
        if not wake_existing_window():
            notify_already_running()
        return 0

    # 配置解析必须早于 import server：hub.py 在模块级就定下 HUB_CONFIG，
    # 晚一步改就只能读到空配置。
    config = resolve_config()
    if config:
        os.environ["AGENTHUB_CONFIG"] = str(config)
    app = DesktopApp(args, config_path=config, guided=config is None and not args.smoke)
    return app.run(smoke=args.smoke)


if __name__ == "__main__":
    sys.exit(main())
