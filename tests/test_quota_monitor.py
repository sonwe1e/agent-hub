"""Quota notifications use isolated identities and simulated upstream data."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from quota_monitor import QuotaMonitor, classify_error


PROVIDER = "codex-chatgpt"


class Clock:
    def __init__(self):
        self.now = 10000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def quota(used=80, reset=20000, clock=None):
    return {"quota": {"source": "fixture upstream", "plan": "fixture plan",
                       "checkedAt": clock() if clock else 10000,
                       "windows": [{"name": "5h", "usedPercent": used,
                                    "windowSeconds": 18000, "resetsAt": reset}]}}


class QuotaMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.identity = {"value": "fixture-private-account"}
        self.current = {"value": quota(clock=self.clock)}
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, query=None, connected=None):
        return QuotaMonitor(self.root, {PROVIDER: query or (lambda: self.current["value"])},
                            {PROVIDER: lambda: self.identity["value"]},
                            connected={PROVIDER: connected} if connected else None,
                            notify=self.events.append, clock=self.clock)

    def test_defaults_settings_persist_and_are_validated(self):
        monitor = self.make()
        self.assertEqual(monitor.status()["settings"], {"enabled": True, "intervalSeconds": 900,
                                                       "thresholdRemainingPercent": 20})
        monitor.update_settings({"enabled": False, "intervalSeconds": 120, "thresholdRemainingPercent": 7.5})
        self.assertEqual(self.make().status()["settings"], {"enabled": False, "intervalSeconds": 120,
                                                           "thresholdRemainingPercent": 7.5})
        for change in ({"enabled": 1}, {"intervalSeconds": 0}, {"intervalSeconds": 60.5},
                       {"thresholdRemainingPercent": 101}, {"unknown": True}):
            with self.assertRaises(ValueError):
                monitor.update_settings(change)

    def test_unknown_is_not_zero_or_an_alert(self):
        self.current["value"] = quota(None, clock=self.clock)
        result = self.make().refresh()
        self.assertIsNone(result["accounts"][PROVIDER]["quota"]["windows"][0]["usedPercent"])
        self.assertEqual(self.events, [])

    def test_dedup_across_polls_and_restart_and_alert_new_level_or_window(self):
        monitor = self.make()
        monitor.refresh()
        monitor.refresh()
        self.assertEqual([x["kind"] for x in self.events], ["low"])
        self.make().refresh()
        self.assertEqual(len(self.events), 1)
        self.current["value"] = quota(100, clock=self.clock)
        monitor.refresh()
        self.assertEqual([x["kind"] for x in self.events], ["low", "exhausted"])
        self.current["value"] = quota(80, reset=25000, clock=self.clock)
        monitor.refresh()
        self.assertEqual([x["kind"] for x in self.events], ["low", "exhausted", "low"])

    def test_no_reset_window_can_realert_after_recovery(self):
        monitor = self.make()
        self.current["value"] = quota(80, reset=None, clock=self.clock)
        monitor.refresh()
        self.current["value"] = quota(30, reset=None, clock=self.clock)
        monitor.refresh()
        self.current["value"] = quota(90, reset=None, clock=self.clock)
        monitor.refresh()
        self.assertEqual([x["kind"] for x in self.events], ["low", "low"])

    def test_identity_change_invalidates_old_snapshot_and_never_exposes_identity(self):
        monitor = self.make()
        monitor.refresh()
        self.identity["value"] = "another-private-account"
        status = monitor.status()
        self.assertIsNone(status["accounts"][PROVIDER]["quota"])
        monitor.refresh()
        encoded = json.dumps(monitor.status(), ensure_ascii=False)
        files = "".join(p.read_text(encoding="utf-8") for p in self.root.glob("*.json"))
        self.assertNotIn("private-account", encoded + files)
        self.assertEqual([x["kind"] for x in self.events], ["low", "low"])

    def test_identity_changes_during_request_discard_snapshot(self):
        def switch():
            self.identity["value"] = "new-account"
            return quota(90, clock=self.clock)
        monitor = self.make(query=switch)
        self.assertIsNone(monitor.refresh()["accounts"][PROVIDER]["quota"])
        self.assertEqual(self.events, [])

    def test_failed_query_keeps_old_snapshot_stale_and_redacts_error(self):
        should_fail = {"value": False}
        def request():
            if should_fail["value"]:
                raise ValueError("HTTP 401 fixture-secret")
            return quota(10, clock=self.clock)
        monitor = self.make(query=request)
        monitor.refresh()
        should_fail["value"] = True
        state = monitor.refresh()["accounts"][PROVIDER]
        self.assertIsNotNone(state["quota"])
        self.assertTrue(state["stale"])
        self.assertEqual(state["error"]["kind"], "login")
        self.assertNotIn("fixture-secret", json.dumps(state))

    def test_stale_and_unknown_identity(self):
        monitor = self.make()
        monitor.refresh()
        self.clock.advance(1801)
        self.assertTrue(monitor.status()["accounts"][PROVIDER]["stale"])
        self.identity["value"] = None
        status = monitor.status()["accounts"][PROVIDER]
        self.assertIsNone(status["quota"])
        self.assertEqual(status["error"]["kind"], "login")

    def test_disconnected_never_polls(self):
        called = []
        monitor = self.make(query=lambda: called.append(True), connected=lambda: False)
        self.assertFalse(monitor.refresh()["accounts"][PROVIDER]["connected"])
        self.assertEqual(called, [])

    def test_ambiguous_429_never_claims_exhaustion(self):
        kind, message = classify_error(status=429)
        self.assertEqual(kind, "limited")
        self.assertIn("额度或请求速率", message)
        self.assertEqual(classify_error(status=429, code="insufficient_quota")[0], "quota")
        self.assertEqual(classify_error(status=429, code="rate_limit_exceeded")[0], "rate_limit")
        self.assertEqual(classify_error(status=401)[0], "login")
        self.assertEqual(classify_error(status=403)[0], "permission")
        self.assertEqual(classify_error(error="network")[0], "network")

    def test_only_new_observed_failures_notify_once_without_raw_vendor_text(self):
        monitor = self.make()
        self.assertFalse(monitor.report_upstream(PROVIDER, status=429, event_id="fixture-old-id", at=self.clock()-1))
        self.clock.advance(1)
        self.assertTrue(monitor.report_upstream(PROVIDER, status=429, event_id="new", at=self.clock()))
        self.assertFalse(monitor.report_upstream(PROVIDER, status=429, event_id="new", at=self.clock()))
        self.assertEqual(monitor.status()["events"][0]["kind"], "limited")
        self.assertNotIn("fixture-old-id", json.dumps(monitor.status()))

    def test_recent_failures_are_filtered_deduplicated_and_errors_are_contained(self):
        seen_since = []
        rows = [{"id": "old", "provider": PROVIDER, "time": self.clock()-1, "httpStatus": 429},
                {"id": "now", "provider": PROVIDER, "time": self.clock()+1, "httpStatus": 429,
                 "error": "do-not-store-this-vendor-body"}]
        def failures(since):
            seen_since.append(since)
            return rows
        monitor = QuotaMonitor(self.root, {PROVIDER: lambda: quota(0, clock=self.clock)},
                               {PROVIDER: lambda: self.identity["value"]}, notify=self.events.append,
                               clock=self.clock, recent_failures=failures)
        monitor._check_recent_failures()
        monitor._check_recent_failures()
        self.assertEqual(seen_since, [self.clock(), self.clock()])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["kind"], "limited")
        self.assertNotIn("do-not-store", json.dumps(monitor.status()))
        monitor.recent_failures = lambda since: (_ for _ in ()).throw(OSError("offline"))
        monitor._check_recent_failures()
        self.assertEqual(len(self.events), 1)
        self.assertTrue(monitor.status()["failureReadError"])
        monitor.recent_failures = lambda since: []
        monitor._check_recent_failures()
        self.assertFalse(monitor.status()["failureReadError"])

    def test_failure_watch_is_independent_of_slow_quota_request(self):
        entered = threading.Event()
        release = threading.Event()
        notified = threading.Event()
        rows = []
        def slow():
            entered.set()
            release.wait(2)
            return quota(5, clock=time.time)
        def event_callback(event):
            self.events.append(event)
            notified.set()
        monitor = QuotaMonitor(self.root, {PROVIDER: slow}, {PROVIDER: lambda: self.identity["value"]},
                               notify=event_callback, recent_failures=lambda since: list(rows),
                               tick_seconds=0.05, failure_interval=0.05)
        monitor.start()
        try:
            self.assertTrue(entered.wait(1))
            rows.append({"id": "recent", "provider": PROVIDER, "time": time.time(), "httpStatus": 429})
            self.assertTrue(notified.wait(1), "slow quota request must not block failure watch")
            self.assertEqual(self.events[0]["kind"], "limited")
            self.assertFalse(monitor.close(timeout=0.01))
        finally:
            release.set()
            self.assertTrue(monitor.close(timeout=2))

    def test_query_network_adapter_failure_is_categorized_and_backed_off(self):
        def fail():
            raise ValueError("额度查询不可用，请检查网络并重试")
        monitor = self.make(query=fail)
        status = monitor.refresh()["accounts"][PROVIDER]
        self.assertEqual(status["error"]["kind"], "network")
        self.assertEqual(monitor._next[PROVIDER], self.clock() + 1800)
        monitor.refresh()
        self.assertEqual(monitor._next[PROVIDER], self.clock() + 3600)

    def test_disabled_monitor_does_not_notify(self):
        monitor = self.make()
        monitor.update_settings({"enabled": False})
        monitor.refresh()
        self.assertEqual(self.events, [])
        self.assertFalse(monitor.report_upstream(PROVIDER, status=429, event_id="a"))

    def test_background_start_idempotent_and_stop(self):
        first = threading.Event()
        calls = []
        def fetch():
            calls.append(True)
            first.set()
            return quota(5, clock=self.clock)
        monitor = self.make(query=fetch)
        monitor.start()
        monitor.start()
        self.assertTrue(first.wait(2))
        self.assertTrue(monitor.close(timeout=2))
        self.assertFalse(monitor.status()["running"])
        self.assertEqual(len(calls), 1)

    def test_notify_failure_does_not_lose_event(self):
        def fail(_event):
            raise RuntimeError("notification unavailable")
        monitor = self.make()
        monitor.notify = fail
        status = monitor.refresh()
        self.assertEqual(len(status["events"]), 1)
        self.assertFalse(status["alertPersistenceError"])


if __name__ == "__main__":
    unittest.main()
