"""Tray behaviour without a real notification area or user configuration."""
import argparse
import unittest
from types import SimpleNamespace

from desktop_app import DesktopApp, TRAY_READY, TRAY_STARTING


class FakeMenuItem:
    def __init__(self, label, callback):
        self.label, self.callback = label, callback


class WorkspaceTrayTests(unittest.TestCase):
    def setUp(self):
        self.app = DesktopApp(argparse.Namespace())
        quota = SimpleNamespace(status=lambda: {"accounts": {
            "codex-chatgpt": {"connected": True, "quota": {"windows": [{"usedPercent": 75}]}, "stale": True},
            "zcode-subscription": {"connected": False}}})
        service = SimpleNamespace(workspace=SimpleNamespace(quota=quota),
            server=SimpleNamespace(api_switch_plans=lambda: {"store": {"plans": [{"id": "work", "name": "工作"}]}}))
        self.app.services = service

    def test_quota_summary_marks_stale_and_never_guesses_disconnected(self):
        self.assertEqual(self.app._tray_quota_summary(), "额度 · Codex 25%（旧）")
        self.app.services.workspace.quota.status = lambda: {"accounts": {}}
        self.assertEqual(self.app._tray_quota_summary(), "额度 · 未知")

    def test_saved_plan_menu_is_a_selection_not_an_apply(self):
        rows = self.app._tray_plan_items(SimpleNamespace(MenuItem=FakeMenuItem))
        self.assertEqual([r.label for r in rows], ["工作"])
        self.assertTrue(callable(rows[0].callback))

    def test_notifications_wait_for_tray_and_remain_public(self):
        sent = []
        self.app.icon = SimpleNamespace(notify=lambda msg, title: sent.append((title, msg)))
        self.app._tray_state = TRAY_STARTING
        self.app._notify_quota({"title": "提醒", "message": "额度不足"})
        self.assertEqual(sent, [])
        self.app._tray_state = TRAY_READY
        self.app._flush_quota_notifications()
        self.assertEqual(sent, [("提醒", "额度不足")])


if __name__ == "__main__":
    unittest.main()
