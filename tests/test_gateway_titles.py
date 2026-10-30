"""Gateway title capture uses synthetic requests and an isolated observation root."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gateway_runtime import Runtime, route_id
from gateway_server import Handler, Server, State, packed
from gateway_titles import begin, excerpt, save
from observation import Store


class GatewayTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-gateway-title-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Store(self.base / 'data', self.base, config=lambda: {})
        self.addCleanup(self.store.close)
        self.root = self.store.root

    def rows(self):
        with self.store.db() as db:
            return db.execute('SELECT agent,sid,title,source,at FROM session_title_metadata ORDER BY agent,sid').fetchall()

    def test_user_messages_only_and_common_sensitive_content_is_redacted(self):
        bodies = [
            {'messages': [{'role': 'system', 'content': 'must not capture'},
                          {'role': 'user', 'content': 'Please fix test with api_key=fixture-super-secret'}]},
            {'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Please fix test with api_key=fixture-super-secret'}]}]},
            {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'Please fix test with api_key=fixture-super-secret'}]}]},
        ]
        for body in bodies:
            with self.subTest(body=body):
                title = excerpt(body, ['fixture-super-secret'])
                self.assertTrue(title.startswith('Please fix test'))
                self.assertNotIn('fixture-super-secret', title)
                self.assertLessEqual(len(title), 61)
        self.assertIsNone(excerpt({'messages': [{'role': 'assistant', 'content': 'not a user request'},
                                              {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'not user text'}]}]}))
        self.assertIsNone(excerpt({'messages': [{'role': 'user', 'content': '# AGENTS.md instructions\nfixture instructions'}]}))
        self.assertIsNone(excerpt({'messages': [{'role': 'user', 'content': 'x' * 20000}]}))

    def test_exact_identity_preserves_manual_override_without_storing_body(self):
        start = time.time()
        epoch = begin(self.root)
        body = {'messages': [{'role': 'user', 'content': '测试短标题 ' + 'x' * 400}]}
        self.assertTrue(save(self.root, 'pi', 'session-one', body, start, epoch))
        row = self.rows()[0]
        self.assertEqual(row[:2], ('pi', 'session-one'))
        self.assertEqual(row[3], 'gateway-user-message')
        self.assertLessEqual(len(row[2]), 61)
        with self.store.db() as db:
            db.execute("INSERT INTO session_manual_titles VALUES('pi','session-one','manual fixture',?)", (time.time(),))
        self.assertTrue(save(self.root, 'pi', 'session-one', body, time.time(), begin(self.root)))
        self.assertTrue(save(self.root, 'claude', 'session-one', body, time.time(), begin(self.root)))
        self.assertTrue(save(self.root, 'codex-desktop', 'session-one', body, time.time(), begin(self.root)))
        by_agent = {row[0]: row for row in self.rows()}
        self.assertEqual(by_agent['pi'][2:4], (row[2], 'gateway-user-message'))
        self.assertEqual(by_agent['claude'][3], 'gateway-user-message')
        self.assertEqual(by_agent['codex'][3], 'gateway-user-message')
        with self.store.db() as db:
            manual = db.execute('SELECT title FROM session_manual_titles WHERE agent=? AND sid=?', ('pi', 'session-one')).fetchone()
            stored = repr(db.execute('SELECT title FROM session_title_metadata').fetchall())
        self.assertEqual(manual[0], 'manual fixture')
        self.assertNotIn('x' * 200, stored)

    def test_clear_and_disable_invalidate_inflight_epochs_even_after_reenable(self):
        body = {'messages': [{'role': 'user', 'content': 'temporary task'}]}
        old_at, old_epoch = time.time(), begin(self.root)
        self.assertTrue(save(self.root, 'pi', 'existing', body, old_at, old_epoch))
        with self.store.db() as db:
            db.execute("INSERT INTO session_manual_titles VALUES('pi','existing','manual fixture',?)", (old_at,))
        self.store.cleanup(clear=True)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM session_manual_titles').fetchone()[0], 0)
        self.store.set_settings({'enabled': True})
        self.assertFalse(save(self.root, 'pi', 'old', body, old_at, old_epoch))
        new_at, new_epoch = time.time(), begin(self.root)
        self.store.set_settings({'enabled': False})
        self.assertIsNone(begin(self.root))
        self.store.set_settings({'enabled': True})
        self.assertFalse(save(self.root, 'pi', 'paused', body, new_at, new_epoch))
        self.assertEqual(self.rows(), [])
        self.assertTrue(save(self.root, 'pi', 'new', body, time.time(), begin(self.root)))
        self.assertEqual(len(self.rows()), 1)

    def test_gateway_title_limit_does_not_evict_manual_titles(self):
        body = {'input': 'test bounded titles'}
        epoch, start = begin(self.root), time.time()
        with self.store.db() as db:
            db.execute("INSERT INTO session_manual_titles VALUES('pi','manual','keep',?)", (start,))
        with patch('gateway_titles.MAX_GATEWAY_TITLES', 2):
            for index in range(3):
                self.assertTrue(save(self.root, 'pi', f'test-{index}', body, start+index, epoch))
        self.assertEqual({row[1] for row in self.rows()}, {'test-1', 'test-2'})
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT title FROM session_manual_titles').fetchone()[0], 'keep')

    def test_separate_gateway_process_cannot_commit_preclear_request(self):
        code = '''
import sys, time
from gateway_titles import begin, save
root=sys.argv[1]
at=time.time()
epoch=begin(root)
print("READY", epoch is not None, flush=True)
sys.stdin.readline()
body={"messages":[{"role":"user","content":"cross process fixture"}]}
print("SAVED", save(root,"pi","process-fixture",body,at,epoch), flush=True)
'''
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = subprocess.Popen([sys.executable, '-c', code, str(self.root)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   creationflags=creationflags)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'READY True')
            self.store.cleanup(clear=True)
            self.store.set_settings({'enabled': True})
            output, errors = process.communicate('\n', timeout=10)
            self.assertEqual(process.returncode, 0, errors)
            self.assertEqual(output.strip(), 'SAVED False')
            self.assertEqual(self.rows(), [])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)

    def test_retention_clear_and_corrupt_settings_fail_closed(self):
        with self.store.db() as db:
            db.execute("INSERT INTO session_title_metadata VALUES('pi','expired','older fixture','gateway-user-message',?)", (time.time()-2*86400,))
        self.store.set_settings({'enabled': True, 'retentionDays': 1})
        self.assertEqual(self.rows(), [])
        epoch = begin(self.root)
        self.store.settings_path.write_text('{invalid', encoding='utf-8')
        self.assertIsNone(begin(self.root))
        self.assertFalse(save(self.root, 'pi', 'new', {'input': 'should not be saved'}, time.time(), epoch))
        self.assertEqual(self.rows(), [])

    def test_unspecified_root_does_not_create_an_observation_store(self):
        state = State(self.base / 'gateway', 'fake-key')
        self.assertIsNone(state.observation_root)
        self.assertIsNone(begin(None))
        self.assertFalse(save(None, 'pi', 'unused', {'input': 'unused'}, time.time(), 1))
        self.assertFalse((self.base / 'gateway').exists())


class GatewayTitleSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-gateway-title-socket-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Store(self.base / 'data', self.base, config=lambda: {})
        self.addCleanup(self.store.close)
        self.started, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)
        started, release = self.started, self.release

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
                self.wfile.flush()
                started.set()
                release.wait(5)
                self.wfile.write(b'data: {"choices":[{"finish_reason":"stop","delta":{}}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()

        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.directory = self.store.root / 'gateway/config/agent-hub'
        self.directory.mkdir(parents=True)
        self.rid = route_id('pi', 'fixture')
        (self.directory / 'providers.json').write_bytes(packed({'providers': [{
            'id': self.rid, 'key': 'fixture-secret-key', 'chat': f'http://127.0.0.1:{self.upstream.server_port}/v1', 'models': ['test']}]}))
        (self.directory / 'hub-connections.json').write_bytes(packed({self.rid: {'agent': 'pi', 'provider': 'fixture'}}))
        self.state = State(self.directory, 'admin-fixture', observation_root=self.store.root)
        self.gateway = Server(('127.0.0.1', 0), Handler)
        self.gateway.state = self.state
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.gateway.shutdown)

    def request(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=8)
        body = {'model': self.rid + '/test', 'stream': True, 'messages': [
            {'role': 'user', 'content': 'Fixture title; password=fixture-secret-key'},
            {'role': 'assistant', 'content': 'fixture private assistant response'}]}
        conn.request('POST', '/v1/chat/completions', packed(body),
                     {'Authorization': 'Bearer agent-hub-pi', 'X-Session-Id': 'pi-fixture', 'Content-Type': 'application/json'})
        return conn, conn.getresponse()

    def wait_save(self):
        for _ in range(200):
            with self.state.lock:
                if not self.state.active:
                    return
            time.sleep(.01)
        self.fail('gateway did not finish')

    def test_actual_inflight_request_cannot_reappear_after_clear_and_reenable(self):
        conn, response = self.request()
        self.assertEqual(response.status, 200)
        self.assertTrue(self.started.wait(2))
        self.store.cleanup(clear=True)
        self.store.set_settings({'enabled': True})
        self.release.set()
        response.read(); conn.close(); self.wait_save()
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM session_title_metadata').fetchone()[0], 0)
        ledger = (self.directory / 'usage.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('Fixture title', ledger)
        self.assertNotIn('fixture private assistant response', ledger)
        self.assertNotIn('fixture-secret-key', ledger)

    def test_actual_request_stores_only_redacted_short_title_in_sqlite(self):
        self.release.set()
        conn, response = self.request()
        self.assertEqual(response.status, 200)
        response.read(); conn.close(); self.wait_save()
        with self.store.db() as db:
            row = db.execute('SELECT agent,sid,title,source FROM session_title_metadata').fetchone()
        self.assertEqual((row[0], row[1], row[3]), ('pi', 'pi-fixture', 'gateway-user-message'))
        self.assertIn('Fixture title', row[2])
        self.assertNotIn('fixture-secret-key', row[2])
        self.assertLessEqual(len(row[2]), 61)
        ledger = (self.directory / 'usage.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('Fixture title', ledger)
        self.assertFalse((self.directory / 'gateway-conversations').exists())
        self.store.ingest_gateway(self.directory / 'usage.jsonl')
        from session_handoff import recent_sessions
        sessions = recent_sessions(self.store)['sessions']
        row = next(x for x in sessions if x['agent'] == 'pi' and x['sessionId'] == 'pi-fixture')
        self.assertEqual(row['titleSource'], 'gateway-user-message')
        self.assertEqual(row['sourceKind'], 'gateway-only')
        self.assertIn('Fixture title', row['title'])
        self.assertNotIn('fixture-secret-key', row['title'])

    def test_session_with_same_id_in_another_agent_does_not_get_the_pi_title(self):
        self.release.set()
        conn, response = self.request()
        self.assertEqual(response.status, 200)
        response.read(); conn.close(); self.wait_save()
        path = self.directory / 'usage.jsonl'
        original = json.loads(path.read_text(encoding='utf-8').splitlines()[0])
        other = {**original, 'agent': 'claude', 'route_id': 'other-agent-route', 'response_id': 'other-agent-response'}
        with path.open('ab') as out:
            out.write(packed(other) + b'\n')
        self.store.ingest_gateway(path)
        from session_handoff import recent_sessions
        sessions = recent_sessions(self.store)['sessions']
        pi = next(x for x in sessions if x['agent'] == 'pi' and x['sessionId'] == 'pi-fixture')
        claude = next(x for x in sessions if x['agent'] == 'claude' and x['sessionId'] == 'pi-fixture')
        self.assertEqual(pi['titleSource'], 'gateway-user-message')
        self.assertIsNone(claude['title'])
        self.assertEqual(claude['titleSource'], 'unavailable')

    def test_title_storage_error_does_not_break_response_stream(self):
        self.release.set()
        with patch('gateway_titles.save', side_effect=RuntimeError('synthetic store error')):
            conn, response = self.request()
            self.assertEqual(response.status, 200)
            self.assertIn(b'[DONE]', response.read())
            conn.close(); self.wait_save()

    def test_owned_gateway_child_uses_explicit_isolated_observation_root(self):
        from test_proxy_transport import PROXY_ENV
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        store = Store(self.base / 'child-data', self.base, config=lambda: {})
        self.addCleanup(store.close)
        config = {'gateway': {'enabled': True, 'port': port}, 'providers': {'fixture': {
            'name': 'fixture', 'baseUrl': f'http://127.0.0.1:{self.upstream.server_port}/v1',
            'apiKey': 'fixture-secret-key', 'api': 'openai-completions',
            'models': [{'id': 'test'}], 'targets': ['pi'], 'enabled': True}}}
        runtime = Runtime(store.root / 'gateway', lambda: config)
        self.release.set()
        try:
            with patch.dict(os.environ, {**PROXY_ENV, 'NO_PROXY': '127.0.0.1,localhost'}):
                runtime.start(False)
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=8)
                body = {'model': route_id('pi', 'fixture') + '/test', 'stream': True,
                        'messages': [{'role': 'user', 'content': 'separate gateway fixture title'}]}
                conn.request('POST', '/v1/chat/completions', packed(body),
                             {'Authorization': 'Bearer agent-hub-pi', 'X-Session-Id': 'child-session',
                              'Content-Type': 'application/json'})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(b'[DONE]', response.read())
                conn.close()
                row = None
                for _ in range(200):
                    with store.db() as db:
                        row = db.execute('SELECT agent,sid,title,source FROM session_title_metadata').fetchone()
                    if row:
                        break
                    time.sleep(.01)
                self.assertEqual(row, ('pi', 'child-session', 'separate gateway fixture title', 'gateway-user-message'))
                self.assertNotIn('separate gateway fixture title', (runtime.directory / 'usage.jsonl').read_text(encoding='utf-8'))
        finally:
            runtime.close()


class GatewayTitleSubscriptionTests(unittest.TestCase):
    def test_codex_and_zcode_special_routes_capture_user_excerpt(self):
        from test_codex_subscription import CodexSubscriptionTests
        from test_zcode_subscription import ZCodeSubscriptionTests
        for case_class, provider, prefix in (
            (CodexSubscriptionTests, 'codex-chatgpt', 'codex-subscription'),
            (ZCodeSubscriptionTests, 'zcode-subscription', 'zcode-subscription'),
        ):
            with self.subTest(provider=provider):
                fixture = case_class(methodName='runTest')
                fixture.setUp()
                try:
                    store = Store(fixture.home / 'title-data', fixture.home, config=lambda: {})
                    try:
                        fixture.http.state.observation_root = store.root
                        body = {'model': 'fixture-model', 'stream': False,
                                'messages': [{'role': 'user', 'content': 'isolated subscription title'}]}
                        conn = http.client.HTTPConnection('127.0.0.1', fixture.http.server_port, timeout=8)
                        conn.request('POST', f'/{prefix}/pi/v1/chat/completions', packed(body),
                                     {'Content-Type': 'application/json', 'X-Session-Id': 'fixture-subscription',
                                      'Authorization': 'Bearer ' + fixture.cfg['providers'][provider]['apiKey']})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 200)
                        response.read(); conn.close()
                        for _ in range(200):
                            with fixture.http.state.lock:
                                if not fixture.http.state.active:
                                    break
                            time.sleep(.01)
                        with store.db() as db:
                            row = db.execute('SELECT agent,sid,title,source FROM session_title_metadata').fetchone()
                        self.assertEqual(row, ('pi', 'fixture-subscription', 'isolated subscription title', 'gateway-user-message'))
                    finally:
                        store.close()
                finally:
                    fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
