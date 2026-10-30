"""Manual names for indexed conversations; all state is a temporary HOME."""
import json
import time
import unittest

from support import isolated, server
from observation import key, parse_native


class ManualSessionTitleTests(unittest.TestCase):
    def _gateway_session(self, home):
        store = server.observations()
        now = time.time()
        source = store.root / 'gateway/config/agent-hub/usage.jsonl'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.touch()
        session = {'agent': 'pi', 'sessionId': 'synthetic-gateway', 'cwd': None,
                   'source': str(source), 'title': None}
        with store.db() as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                       (key('pi', 'synthetic-gateway'), 'pi', 'synthetic-gateway', now, json.dumps(session)))
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',
                       ('fixture-request', 'pi', 'synthetic-gateway', now, '{}'))
        return store

    def test_manually_label_gateway_session_and_clear_without_saving_secret(self):
        with isolated() as home:
            store = self._gateway_session(home)
            status, value = server.dispatch_api('PUT', '/api/sessions/title', body={
                'agent': 'pi', 'sessionId': 'synthetic-gateway',
                'title': '整理网关错误 api_key=test-not-a-real-key'})
            self.assertEqual(status, 200, value)
            status, result = server.dispatch_api('GET', '/api/sessions/recent')
            self.assertEqual(status, 200, result)
            row = result['sessions'][0]
            self.assertEqual(row['titleSource'], 'manual')
            self.assertEqual(row['sourceKind'], 'gateway-only')
            self.assertIn('整理网关错误', row['title'])
            self.assertNotIn('test-not-a-real-key', row['title'])
            with store.db() as db:
                raw = db.execute('SELECT title FROM session_manual_titles').fetchone()[0]
                self.assertNotIn('test-not-a-real-key', raw)
            status, result = server.dispatch_api('PUT', '/api/sessions/title', body={
                'agent': 'pi', 'sessionId': 'synthetic-gateway', 'title': ''})
            self.assertEqual(status, 200, result)
            self.assertIsNone(server.dispatch_api('GET', '/api/sessions/recent')[1]['sessions'][0]['title'])

    def test_manual_title_wins_over_native_and_clear_removes_it(self):
        with isolated() as home:
            store = server.observations()
            source = home / '.pi/agent/sessions/project/session.jsonl'
            source.parent.mkdir(parents=True)
            now = time.time()
            from datetime import datetime, timezone
            stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
            rows = [
                {'type': 'session', 'id': 'synthetic-native', 'timestamp': stamp, 'cwd': str(home)},
                {'type': 'message', 'timestamp': stamp, 'message': {'role': 'user', 'content': '自动名称'}},
                {'type': 'message', 'id': 'a', 'timestamp': stamp, 'message': {
                    'role': 'assistant', 'responseId': 'response', 'stopReason': 'stop',
                    'usage': {'input': 1, 'output': 1}}},
            ]
            source.write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows), encoding='utf-8')
            store.ingest(parse_native('pi', source), {'content': False, 'retentionDays': 0, 'clearedBefore': 0})
            status, value = server.dispatch_api('PUT', '/api/sessions/title', body={
                'agent': 'pi', 'sessionId': 'synthetic-native', 'title': '手动名称'})
            self.assertEqual(status, 200, value)
            row = server.dispatch_api('GET', '/api/sessions/recent')[1]['sessions'][0]
            self.assertEqual((row['title'], row['titleSource']), ('手动名称', 'manual'))
            store.cleanup(clear=True)
            with store.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM session_manual_titles').fetchone()[0], 0)

    def test_removing_manual_title_reveals_preserved_gateway_title(self):
        with isolated() as home:
            store = self._gateway_session(home)
            with store.db() as db:
                db.execute('INSERT INTO session_title_metadata VALUES(?,?,?,?,?)',
                           ('pi', 'synthetic-gateway', '自动网关标题', 'gateway-user-message', time.time()))
            body = {'agent': 'pi', 'sessionId': 'synthetic-gateway', 'title': '我起的名称'}
            self.assertEqual(server.dispatch_api('PUT', '/api/sessions/title', body=body)[0], 200)
            row = server.dispatch_api('GET', '/api/sessions/recent')[1]['sessions'][0]
            self.assertEqual((row['title'], row['titleSource']), ('我起的名称', 'manual'))
            body['title'] = ''
            self.assertEqual(server.dispatch_api('PUT', '/api/sessions/title', body=body)[0], 200)
            row = server.dispatch_api('GET', '/api/sessions/recent')[1]['sessions'][0]
            self.assertEqual((row['title'], row['titleSource']), ('自动网关标题', 'gateway-user-message'))

    def test_unknown_session_and_invalid_title_are_rejected(self):
        with isolated() as home:
            self._gateway_session(home)
            for sid, title in (('missing', '标题'), ('synthetic-gateway', 'x' * 61),
                               ('synthetic-gateway', 'bad\x00title')):
                status, _ = server.dispatch_api('PUT', '/api/sessions/title', body={
                    'agent': 'pi', 'sessionId': sid, 'title': title})
                self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
