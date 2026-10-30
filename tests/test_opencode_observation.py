"""OpenCode v2 observation uses isolated, read-only SQLite and its live WAL."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from observation import Store, opencode_database, source_agent


class OpenCodeObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hub-opencode-observe-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / '.local/share/opencode/opencode.db'
        self.path.parent.mkdir(parents=True)
        self.env = patch.dict(os.environ, {'OPENCODE_DB': str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.native = sqlite3.connect(self.path)
        self.native.execute('PRAGMA journal_mode=WAL')
        self.native.executescript('''
            CREATE TABLE session_v2(id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
                                    title TEXT, time_created INTEGER);
            CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT, type TEXT,
                                         seq INTEGER, time_created INTEGER, data TEXT);
            CREATE TABLE credential(secret TEXT);
            INSERT INTO credential VALUES('private-credential-never-read');
        ''')
        self.now = int(time.time() * 1000) - 5000
        self.native.executemany('INSERT INTO session_v2 VALUES(?,?,?,?,?)', [
            ('session-a', None, '/project/a', 'Private title A', self.now),
            ('session-b', 'session-a', '/project/b', 'Private title B', self.now),
        ])
        self.add_message('msg-a', 'session-a', 'assistant', 1, {
            'model': {'id': 'test-model', 'providerID': 'test-provider'},
            'finish': 'stop', 'time': {'created': self.now, 'completed': self.now + 1000},
            'tokens': {'input': 100, 'output': 20, 'reasoning': 5, 'cache': {'read': 30, 'write': 2}},
            'providerState': {'responseId': 'exact-response-id'},
            'cost': 0.012, 'content': [{'type': 'text', 'text': 'private assistant body'}],
        })
        self.add_message('msg-b', 'session-b', 'assistant', 1, {
            'model': {'id': 'other-model', 'providerID': 'other-provider'},
            'finish': 'error', 'time': {'created': self.now, 'completed': self.now + 500},
            'tokens': {'input': 0, 'output': 0, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}},
            'cost': 0, 'content': [],
        })
        self.add_message('user-a', 'session-a', 'user', 2, {'text': 'private user body'})
        self.native.commit()
        self.store = Store(self.home / 'hub-data', self.home)
        self.addCleanup(self.store.close)
        self.addCleanup(self.native.close)

    def add_message(self, mid, sid, kind, seq, data):
        self.native.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?)',
                            (mid, sid, kind, seq, self.now + seq, json.dumps(data)))

    def test_collection_reads_v2_steps_and_wal_without_copying_content(self):
        self.store.collect()
        result = self.store.query({'agent': 'opencode', 'days': 'all'})
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['sessionTotal'], 2)
        rows = {row['sessionId']: row for row in result['requests']}
        self.assertEqual(rows['session-a']['usage']['context'], 132)
        self.assertEqual(rows['session-a']['usage']['output'], 25)
        self.assertEqual(rows['session-a']['usage']['reasoning'], 5)
        self.assertEqual(rows['session-a']['status'], 'succeeded')
        self.assertEqual(rows['session-b']['status'], 'failed')
        self.assertIsNone(rows['session-b']['usage']['input'])
        self.assertIsNone(rows['session-b']['cost'])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
            indexed = ''.join(r[0] for r in db.execute('SELECT data FROM requests UNION ALL SELECT data FROM sessions'))
        self.assertNotIn('private user body', indexed)
        self.assertNotIn('private assistant body', indexed)
        self.assertNotIn('private-credential-never-read', indexed)

        # A gateway record with the exact provider response ID is the same
        # request, not another billable step.
        gateway = self.store.root / 'gateway/config/agent-hub'
        gateway.mkdir(parents=True)
        (gateway / 'usage.jsonl').write_text(json.dumps({
            'agent': 'opencode', 't': self.now / 1000, 'route_id': 'route-1',
            'response_id': 'exact-response-id', 'status': 200, 'stop': 'completed',
            'in': 132, 'out': 25, 'cache_read': 30, 'cache_write': 2,
        }) + '\n', encoding='utf-8')
        self.store.ingest_gateway(gateway / 'usage.jsonl')
        merged = self.store.query({'agent': 'opencode', 'days': 'all'})
        self.assertEqual(merged['total'], 2)
        self.assertTrue(any(row['sourceType'] == 'gateway+native' for row in merged['requests']))
        second = Store(self.home / 'other-hub-data', self.home)
        try:
            second.ingest_gateway(gateway / 'usage.jsonl')
            second.collect()
            self.assertEqual(second.query({'agent': 'opencode', 'days': 'all'})['total'], 2)
        finally:
            second.close()

        detail = self.store.session_detail('opencode', 'session-a', {'days': 'all'})
        self.assertEqual(detail['contentSource'], 'native-on-demand')
        self.assertIn('private user body', json.dumps(detail['messages']))
        self.assertNotIn('private-credential-never-read', json.dumps(detail))
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

        # The writer remains open: SQLite can append solely to WAL without
        # changing the main database's size or mtime.
        self.add_message('msg-new', 'session-a', 'assistant', 3, {
            'model': {'id': 'test-model', 'providerID': 'test-provider'},
            'finish': 'stop', 'time': {'created': self.now + 3, 'completed': self.now + 6},
            'tokens': {'input': 1, 'output': 2, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}},
        })
        self.native.commit()
        self.store.collect()
        updated = self.store.query({'agent': 'opencode', 'days': 'all'})
        self.assertEqual(updated['total'], 3)
        self.assertEqual(updated['sessionTotal'], 2)
        self.store.collect()
        self.assertEqual(self.store.query({'agent': 'opencode', 'days': 'all'})['total'], 3)

    def test_source_path_is_exact_and_invalid_rows_are_reported(self):
        self.assertEqual(opencode_database(self.home), self.path)
        self.assertEqual(source_agent(self.home, self.path), 'opencode')
        self.assertIsNone(source_agent(self.home, self.path.with_name('other.db')))
        self.native.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?)',
                            ('bad', 'session-a', 'assistant', 9, self.now, '{broken'))
        self.native.commit()
        self.store.collect()
        self.assertEqual(self.store.query({'agent': 'opencode', 'days': 'all'})['total'], 2)
        self.assertTrue(any(e['agent'] == 'opencode' and e['kind'] == 'parse'
                            for e in self.store.snapshot()['degraded']))


if __name__ == '__main__':
    unittest.main()
