"""Workspace routes use the real services with a temporary HOME and config."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import isolated, server
from workspace_services import WorkspaceServices


class WorkspaceIntegrationTests(unittest.TestCase):
    def test_recent_sessions_marks_only_known_gateway_ledgers_and_respects_clear(self):
        with isolated() as home:
            store = server.observations()
            now = time.time()
            store.settings_path.write_text(json.dumps({'enabled': False, 'content': False,
                'retentionDays': 30, 'clearedBefore': now - 50}), encoding='utf-8')
            paths = {
                'hub-gateway': store.root / 'gateway/config/agent-hub/usage.jsonl',
                'legacy-gateway': store.root / 'gateway/config/magpie/usage.jsonl',
                'other-same-name': home / 'unrelated' / 'usage.jsonl',
                'missing-source': None,
                'before-clear': store.root / 'gateway/config/agent-hub/usage.jsonl',
            }
            with store.db() as db:
                for sid, source in paths.items():
                    at = now - 100 if sid == 'before-clear' else now
                    session = {'agent': 'pi', 'sessionId': sid,
                               'source': str(source) if source else None, 'cwd': None}
                    request = {'agent':'pi', 'sessionId':sid, 'time':at}
                    db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                               ('session-'+sid, 'pi', sid, at, json.dumps(session)))
                    db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',
                               ('request-'+sid, 'pi', sid, at, json.dumps(request)))
            status, payload = server.dispatch_api('GET', '/api/sessions/recent')
            self.assertEqual(status, 200, payload)
            rows = {row['sessionId']: row for row in payload['sessions']}
            self.assertNotIn('before-clear', rows)
            for sid in ('hub-gateway', 'legacy-gateway'):
                self.assertEqual(rows[sid]['sourceKind'], 'gateway-only')
                self.assertNotIn('source', rows[sid])
            for sid in ('other-same-name', 'missing-source'):
                self.assertNotIn('sourceKind', rows[sid])
                self.assertNotIn('source', rows[sid])

    def test_discarded_library_preview_cannot_be_applied_or_change_files(self):
        with isolated() as home:
            source = home / "personal" / "discard-skill"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: discard-skill\n---\nBody\n", encoding="utf-8")
            status, item = server.dispatch_api("POST", "/api/capabilities/import", body={
                "kind": "skill", "name": "discard-skill", "source": str(source),
                "targets": ["codex"], "mode": "manual"})
            self.assertEqual(status, 200, item)
            status, preview = server.dispatch_api("POST", f"/api/capabilities/{item['id']}/preview")
            self.assertEqual(status, 200, preview)
            target = home / ".codex" / "skills" / "discard-skill"
            status, discarded = server.dispatch_api("POST", "/api/capabilities/discard", body={"planId": preview["planId"]})
            self.assertEqual((status, discarded.get("discarded")), (200, True))
            status, discarded_again = server.dispatch_api("POST", "/api/capabilities/discard", body={"planId": preview["planId"]})
            self.assertEqual((status, discarded_again.get("discarded")), (200, False))
            status, error = server.dispatch_api("POST", "/api/capabilities/apply", body={"planId": preview["planId"]})
            self.assertEqual(status, 400, error)
            self.assertFalse(target.exists())

    def test_library_api_publish_conflict_and_restore(self):
        with isolated() as home:
            source = home / "personal" / "sample-skill"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: sample-skill\n---\nOne\n", encoding="utf-8")
            (source / "references").mkdir()
            (source / "references" / "guide.md").write_text("Reference\n", encoding="utf-8")
            status, item = server.dispatch_api("POST", "/api/capabilities/import", body={
                "kind": "skill", "name": "sample-skill", "source": str(source),
                "targets": ["codex", "pi"], "mode": "manual"})
            self.assertEqual(status, 200, item)
            item_id = item["id"]
            target = home / ".codex" / "skills" / "sample-skill"
            self.assertFalse(target.exists(), "import must not publish")
            status, preview = server.dispatch_api("POST", f"/api/capabilities/{item_id}/preview")
            self.assertEqual(status, 200, preview)
            self.assertFalse(preview["hasConflicts"])
            status, receipt = server.dispatch_api("POST", "/api/capabilities/apply", body={"planId": preview["planId"]})
            self.assertEqual(status, 200, receipt)
            self.assertEqual((target / "references" / "guide.md").read_text(encoding="utf-8"), "Reference\n")
            (target / "SKILL.md").write_text("local edit\n", encoding="utf-8")
            status, preview2 = server.dispatch_api("POST", f"/api/capabilities/{item_id}/preview")
            self.assertEqual(status, 200, preview2)
            self.assertTrue(preview2["hasConflicts"])
            status, blocked = server.dispatch_api("POST", "/api/capabilities/apply", body={"planId": preview2["planId"]})
            self.assertEqual(status, 400, blocked)
            status, blocked = server.dispatch_api("POST", "/api/capabilities/restore", body={"transactionId": receipt["transactionId"]})
            self.assertEqual(status, 400, blocked)
            self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "local edit\n")
            self.assertEqual(server.dispatch_api("DELETE", f"/api/capabilities/{item_id}")[0], 200)
            self.assertTrue(target.exists())

    def test_session_and_quota_read_routes_are_safe_without_live_credentials(self):
        with isolated():
            status, sessions = server.dispatch_api("GET", "/api/sessions/recent")
            self.assertEqual(status, 200, sessions)
            self.assertEqual(sessions["sessions"], [])
            status, quota = server.dispatch_api("GET", "/api/quota-monitor")
            self.assertEqual(status, 200, quota)
            self.assertFalse(quota["accounts"]["codex-chatgpt"]["connected"])
            self.assertIsNone(quota["accounts"]["codex-chatgpt"]["quota"])
            status, error = server.dispatch_api("POST", "/api/sessions/resume", body={"agent": "codex", "sessionId": "missing"})
            self.assertEqual(status, 400, error)

    def test_failure_scan_includes_unattributed_native_codex_and_does_not_truncate(self):
        with isolated():
            store = server.observations()
            now = time.time()
            rows = []
            for i in range(1005):
                native = i == 1004
                row = {"provider": None if native else "codex-chatgpt", "sourceType": "gateway",
                       "status": "failed", "httpStatus": 429, "evidence": "Agent Hub usage.jsonl + routing"}
                rows.append((str(i), "codex", None, now + i / 10000, json.dumps(row)))
            with store.db() as db:
                db.executemany("INSERT INTO requests VALUES(?,?,?,?,?)", rows)
            found = server._recent_subscription_failures(now - 1)
            self.assertEqual(len(found), 1005)
            self.assertEqual(found[-1]["provider"], "codex-native")
            self.assertNotIn("error", found[-1])
            monitor = server.workspace_services().quota
            self.assertTrue(monitor.report_upstream("codex-native", status=429, event_id="new"))
            event = monitor.status()["events"][-1]
            self.assertEqual(event["accountAttribution"], "unknown")
            self.assertIn("额度或请求速率", event["message"])

    def test_summary_dispatch_reconfigures_gateway_and_releases_global_lock(self):
        with isolated():
            cfg = {"providers": {}}
            calls = []
            class Runtime:
                def __init__(self): self.lock = threading.RLock()
                def start(self, content): calls.append(("start", content))
                def configure(self, content, cfg=None): calls.append(("configure", cfg))
            runtime = Runtime()
            preview = {"digest": "same"}
            def generate(*args, **kwargs):
                # Acquiring server.LOCK from a second thread proves this is not
                # held while the model request waits.
                acquired = threading.Event()
                def probe():
                    with server.LOCK: acquired.set()
                thread = threading.Thread(target=probe); thread.start()
                self.assertTrue(acquired.wait(1))
                thread.join(1)
                calls.append(("generate", runtime.lock._is_owned()))
                return {"draft": "test"}
            with patch.object(server, "hub", return_value=SimpleNamespace(cfg=cfg)), \
                 patch.object(server, "gateway", return_value=runtime), \
                 patch.object(server, "_summary_identity", return_value=None), \
                 patch("handoff_summary.preview_summary", return_value=preview), \
                 patch("handoff_summary.generate_summary", side_effect=generate):
                result = server.api_summary_generate({"selection": {}, "material": "test", "digest": "same"})
            self.assertEqual(result["draft"], "test")
            self.assertEqual([entry[0] for entry in calls], ["start", "configure", "generate"])
            self.assertIs(calls[1][1], cfg)
            self.assertTrue(calls[2][1])


class WorkspaceWorkerTests(unittest.TestCase):
    def test_automatic_sync_waits_for_stable_source(self):
        class Quota:
            def start(self): pass
            def close(self, timeout=0): return True
        class Library:
            def __init__(self):
                self.hash = "first"
                self.calls = []
                self.called = threading.Event()
            def list_items(self):
                return {"items": [{"id": "a", "mode": "auto", "published": "v1", "sourceHash": self.hash}]}
            def sync_auto(self, item_ids=None, expected_hashes=None):
                assert expected_hashes == {key: self.hash for key in item_ids}
                self.calls.append((self.hash, item_ids))
                self.called.set()
                return {"results": []}
        library = Library()
        worker = WorkspaceServices(library, Quota(), interval=1)
        worker.start()
        try:
            time.sleep(1.1)
            library.hash = "second"
            time.sleep(1.1)
            self.assertEqual(library.calls, [])
            self.assertTrue(library.called.wait(1.5))
            self.assertEqual(library.calls[0], ("second", ["a"]))
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()
