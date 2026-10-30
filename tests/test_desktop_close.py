"""desktop_app 关闭策略的无 GUI 契约测试。

为什么这个测试存在
==================

「点关闭 = 收进托盘，进程与采集继续」这件事，只有把几个分支同时钉住才成立。
它失效的方式很特别：**不报错，只把进程变成不可达的孤儿**。

- 若 `_on_closing` 被改回「裸 `hide()`」，代码照样能跑、pywebview 也不报错，
  但托盘不可用时窗口一藏就再也叫不回来：没有图标可点，再双击 exe 只会被告知
  「已经在运行」，任务管理器里那个进程没人认得。用户看到的现象是
  「点了关闭就没了」或者「关了还占着」——正是这次要修的那个 bug。
- 若 `decide_close` 的 `confirm` 分支被删掉，托盘一坏就静默藏窗口，同样是孤儿。
- 若 `quit()` 不幂等，重复点托盘「退出」会二次 destroy；若 join 不设上限，
  退出会被托盘线程卡住。

这三类问题都不会让任何「界面看起来对不对」的测试变红，所以必须在这里用纯逻辑
钉死。本文件因此**只依赖 stdlib 与假的 window / icon 对象**：不 import webview、
不开窗、不起真托盘、不读配置——在没有桌面会话的机器（CI、远程终端）上也必须全绿。

运行：
    python -m unittest tests.test_desktop_close -v
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import desktop_app as da  # noqa: E402

# 测试专用互斥体名：避免和真在跑的 Agent Hub 抢同一个名字（否则跑测试会误判成
# 「已有实例」，或者反过来把用户的实例判成占用）。
TEST_MUTEX = "Local\\AgentHub.Desktop.Test.SingleInstance"


class FakeWindow:
    """只记录被调用了什么，不接触任何真实句柄。"""

    def __init__(self, hide_raises: BaseException | None = None) -> None:
        self.hide_calls = 0
        self.show_calls = 0
        self.destroy_calls = 0
        self.hide_raises = hide_raises

    def hide(self) -> None:
        self.hide_calls += 1
        if self.hide_raises is not None:
            raise self.hide_raises

    def show(self) -> None:
        self.show_calls += 1

    def restore(self) -> None:
        pass

    def destroy(self) -> None:
        self.destroy_calls += 1


class FakeIcon:
    """假托盘图标：记录 notify / stop，不碰真实通知区。"""

    def __init__(self, notify_raises: BaseException | None = None) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.stop_calls = 0
        self.notify_raises = notify_raises

    def notify(self, message, title=None) -> None:
        if self.notify_raises is not None:
            raise self.notify_raises
        self.notifications.append((str(title), str(message)))

    def stop(self) -> None:
        self.stop_calls += 1

    def update_menu(self) -> None:
        pass


class FakeServices:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def make_app(tray_available: bool = True, icon: FakeIcon | None = None) -> da.DesktopApp:
    """构造一个不依赖 GUI 的 DesktopApp。

    Args:
        tray_available: True 时伪造「图标已就绪且消息循环线程活着」的状态。
        icon: 想检查 notify 时传入的假图标；默认新建一个。

    Returns:
        只装配了 window / icon / 托盘状态的 DesktopApp 实例。
    """
    app = da.DesktopApp(argparse.Namespace(), config_path=None, guided=False)
    app.window = FakeWindow()
    if tray_available:
        # _tray_available() 的三个条件：icon 非空、状态 ready、线程 alive
        app.icon = icon if icon is not None else FakeIcon()
        app._tray_state = da.TRAY_READY
        app._tray_thread = threading.current_thread()
    else:
        app.icon = None
        app._tray_state = da.TRAY_UNAVAILABLE
        app._tray_error = ImportError("No module named 'pystray'")
    return app


class DecideCloseTests(unittest.TestCase):
    """关闭决策的纯函数契约：返回值就是 _on_closing 的分支依据。"""

    def test_quitting_allows_close_even_without_tray(self) -> None:
        # 已经在退出流程里，不能再拦；托盘在不在都无关
        self.assertEqual(da.decide_close(True, True), "allow")
        self.assertEqual(da.decide_close(True, False), "allow")

    def test_tray_available_hides_to_tray(self) -> None:
        self.assertEqual(da.decide_close(False, True), "hide")

    def test_tray_unavailable_asks_user(self) -> None:
        # 关键回归点：这里绝不能是 "hide"
        self.assertEqual(da.decide_close(False, False), "confirm")

    def test_return_values_stay_within_contract(self) -> None:
        seen = {
            da.decide_close(q, t)
            for q in (True, False)
            for t in (True, False)
        }
        self.assertEqual(seen, {"allow", "hide", "confirm"})


class OnClosingTests(unittest.TestCase):
    """_on_closing 的三条分支：True = 放行关闭，False = 取消关闭。"""

    def test_quitting_allows_and_never_hides(self) -> None:
        app = make_app(tray_available=True)
        app.quitting = True
        self.assertIs(app._on_closing(), True)
        self.assertEqual(app.window.hide_calls, 0)

    def test_tray_available_hides_and_cancels_close(self) -> None:
        app = make_app(tray_available=True)
        self.assertIs(app._on_closing(), False)  # False = 取消关闭
        self.assertEqual(app.window.hide_calls, 1)
        self.assertFalse(app.quitting)

    def test_confirm_accept_exits(self) -> None:
        app = make_app(tray_available=False)
        with patch.object(da, "confirm_quit_without_tray", return_value=True) as ask:
            self.assertIs(app._on_closing(), True)
        self.assertTrue(app.quitting)
        # 从没 hide 过：窗口要一直看得见，直到用户亲口确认退出
        self.assertEqual(app.window.hide_calls, 0)
        self.assertEqual(ask.call_count, 1)

    def test_confirm_reject_keeps_window_visible(self) -> None:
        app = make_app(tray_available=False)
        with patch.object(da, "confirm_quit_without_tray", return_value=False):
            self.assertIs(app._on_closing(), False)
        self.assertFalse(app.quitting)
        self.assertEqual(app.window.hide_calls, 0)

    def test_confirm_carries_a_readable_reason(self) -> None:
        # 确认框必须说得清「为什么」，否则等于耍流氓
        app = make_app(tray_available=False)
        with patch.object(da, "confirm_quit_without_tray", return_value=False) as ask:
            app._on_closing()
        detail = ask.call_args.args[0]
        self.assertIn("ImportError", detail)
        self.assertIn("pystray", detail)

    def test_dialog_reentry_does_not_stack(self) -> None:
        # closing 是同步派发的，MessageBox 会开嵌套消息循环；用户此刻再点一次 X
        # 不能叠出第二个框
        app = make_app(tray_available=False)
        app._close_dialog_open = True
        with patch.object(da, "confirm_quit_without_tray", return_value=True) as ask:
            self.assertIs(app._on_closing(), False)
        self.assertEqual(ask.call_count, 0)

    def test_hide_failure_allows_close(self) -> None:
        # 藏不住就别拦：留下一个关不掉的窗口比退出更糟
        app = make_app(tray_available=True)
        app.window = FakeWindow(hide_raises=RuntimeError("boom"))
        self.assertIs(app._on_closing(), True)


class TrayNotifyTests(unittest.TestCase):
    """首次收进托盘只提示一次气泡：窗口消失必须有人说句话。"""

    def test_notifies_exactly_once_across_repeated_closes(self) -> None:
        icon = FakeIcon()
        app = make_app(tray_available=True, icon=icon)
        for _ in range(3):
            self.assertIs(app._on_closing(), False)
        self.assertEqual(app.window.hide_calls, 3)
        self.assertEqual(len(icon.notifications), 1, "气泡只该弹一次")

    def test_notification_texts_are_actionable(self) -> None:
        icon = FakeIcon()
        app = make_app(tray_available=True, icon=icon)
        app._on_closing()
        title, message = icon.notifications[0]
        self.assertIn("后台", title)
        self.assertIn("托盘", message)
        self.assertIn("退出", message)

    def test_notify_failure_never_breaks_hiding(self) -> None:
        # 部分 Windows 上通知会被组策略/专注助手掐掉，绝不能因此不隐藏
        icon = FakeIcon(notify_raises=OSError("notification blocked"))
        app = make_app(tray_available=True, icon=icon)
        self.assertIs(app._on_closing(), False)
        self.assertEqual(app.window.hide_calls, 1)
        self.assertFalse(app.quitting)
        self.assertTrue(app._hide_notified, "失败的提示也不该每轮重试")

    def test_no_notification_on_confirm_branch(self) -> None:
        # 那条路径已经弹了确认框，再弹气泡就是二次打扰
        icon = FakeIcon()
        app = make_app(tray_available=False, icon=icon)
        with patch.object(da, "confirm_quit_without_tray", return_value=True):
            app._on_closing()
        self.assertEqual(app.icon, None)
        self.assertEqual(icon.notifications, [])


class QuitIdempotencyTests(unittest.TestCase):
    """退出路径幂等，且不能让托盘线程拖住。"""

    def test_repeated_quit_destroys_window_once(self) -> None:
        app = make_app(tray_available=True)
        app.quit()
        app.quit()
        app.quit()
        self.assertEqual(app.window.destroy_calls, 1)

    def test_quit_stops_tray_and_clears_it(self) -> None:
        icon = FakeIcon()
        app = make_app(tray_available=True, icon=icon)
        app.quit()
        self.assertEqual(icon.stop_calls, 1)
        self.assertIsNone(app.icon)
        self.assertEqual(app._tray_state, da.TRAY_STOPPED)
        self.assertFalse(app._tray_available())
        self.assertTrue(app.quitting)

    def test_quit_after_tray_already_stopped_is_safe(self) -> None:
        app = make_app(tray_available=False)
        app.quit()  # icon 为 None，不应抛
        self.assertEqual(app.window.destroy_calls, 1)

    def test_shutdown_closes_services_once(self) -> None:
        app = make_app(tray_available=True)
        app.services = FakeServices()
        app._shutdown()
        app._shutdown()
        self.assertEqual(app.services.close_calls, 1)

    def test_shutdown_without_services_is_safe(self) -> None:
        app = make_app(tray_available=True)
        app._shutdown()  # guided 模式下 services 一直是 None
        self.assertIsNone(app.services)

    def test_restart_sets_flag_before_destroy(self) -> None:
        # destroy 会让主线程立刻返回 webview.start()，标志必须先置位，
        # 否则「重启」会静默变成「退出」
        app = make_app(tray_available=True)
        with patch.object(da.os, "execv") as execv:
            app.restart()
            self.assertTrue(app._restart_requested)
            app._shutdown()
        self.assertEqual(app.window.destroy_calls, 1)
        self.assertEqual(execv.call_count, 1)


@unittest.skipUnless(os.name == "nt", "单实例互斥体是 Win32 语义")
class SingleInstanceTests(unittest.TestCase):
    """互斥体的占/放：restart 依赖「先放再 execv」。"""

    def setUp(self) -> None:
        self._patch = patch.object(da, "MUTEX_NAME", TEST_MUTEX)
        self._patch.start()
        da.release_single_instance()

    def tearDown(self) -> None:
        da.release_single_instance()
        self._patch.stop()

    def test_first_acquire_succeeds_second_reports_existing(self) -> None:
        self.assertTrue(da.single_instance_ok())
        self.assertFalse(da.single_instance_ok())

    def test_release_allows_reacquire(self) -> None:
        self.assertTrue(da.single_instance_ok())
        self.assertFalse(da.single_instance_ok())
        da.release_single_instance()
        self.assertTrue(da.single_instance_ok())

    def test_release_is_idempotent(self) -> None:
        self.assertTrue(da.single_instance_ok())
        da.release_single_instance()
        da.release_single_instance()
        da.release_single_instance()
        self.assertIsNone(da._MUTEX_HANDLE)

    def test_unknown_title_is_not_found(self) -> None:
        # 第二次启动靠标题找窗口；找不到时必须走「提示」而不是假装成功
        self.assertIsNone(da.find_main_window("AgentHub-No-Such-Window-4F2A"))

    def test_wake_reports_failure_without_a_window(self) -> None:
        self.assertFalse(
            da.wake_existing_window("AgentHub-No-Such-Window-4F2A", attempts=1, delay=0.0)
        )


class TrayAvailabilityTests(unittest.TestCase):
    """托盘可用性的三道确认（都不需要真托盘）。"""

    def test_default_state_is_unavailable(self) -> None:
        app = da.DesktopApp(argparse.Namespace(), config_path=None, guided=False)
        self.assertEqual(app._tray_state, da.TRAY_UNAVAILABLE)
        self.assertFalse(app._tray_available())
        self.assertEqual(app._tray_error_text(), "")

    def test_ready_requires_live_thread(self) -> None:
        app = make_app(tray_available=True)
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        app._tray_thread = dead
        self.assertFalse(app._tray_available(), "消息循环线程死了就不能算可用")

    def test_stopped_state_is_not_available(self) -> None:
        app = make_app(tray_available=True)
        app._stop_tray()
        self.assertFalse(app._tray_available())


if __name__ == "__main__":
    unittest.main(verbosity=2)
