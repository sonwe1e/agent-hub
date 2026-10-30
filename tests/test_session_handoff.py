"""Session handoff against isolated native logs; never opens a real client."""
import json
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observation import Store, parse_native, parse_opencode_database, key
from session_handoff import (draft_handoff, handoff_target_capabilities, launch_target,
                             preview_resume, recent_sessions, resume_session, start_handoff)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')


class Process:
    def __init__(self):
        self.done = threading.Event()

    def wait(self):
        self.done.wait(3)
        return 0


class SessionHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = self.home / 'project with spaces'
        self.project.mkdir()
        self.env = patch.dict(os.environ, {'CODEX_HOME': '', 'CLAUDE_CONFIG_DIR': '',
                                           'PI_CODING_AGENT_DIR': '', 'DSH_HOME': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store(self.home / 'data', self.home,
                           config=lambda: {'providers': {'example': {'apiKey': 'fixture-secret-private'}}})
        self.addCleanup(self.store.close)
        self.store.settings_path.write_text(json.dumps({'enabled': False, 'content': False,
                                                        'retentionDays': 0, 'clearedBefore': 0}), encoding='utf-8')
        self.at = datetime.now(timezone.utc).isoformat()
        self.codex = self.home / '.codex/sessions/2026/01/01/example.jsonl'
        self.codex_rows = [
            {'type': 'session_meta', 'timestamp': self.at, 'payload': {'id': 'codex-session-1', 'cwd': str(self.project)}},
            {'type': 'response_item', 'timestamp': self.at, 'payload': {'id': 'u1', 'role': 'user', 'content': '完整任务 fixture-secret-private'}},
            {'type': 'response_item', 'timestamp': self.at, 'payload': {'id': 'a1', 'role': 'assistant', 'content': '结果待用户检查'}},
            {'type': 'token_usage_record', 'timestamp': self.at,
             'payload': {'thread_id': 'codex-session-1', 'response_id': 'response-1', 'usage': {'input_tokens': 10}}},
        ]
        write_jsonl(self.codex, self.codex_rows)
        self.store.ingest(parse_native('codex', self.codex), {'content': False, 'retentionDays': 0, 'clearedBefore': 0})
        self.pi = self.home / '.pi/agent/sessions/project/pi.jsonl'
        self.pi_rows = [
            {'type': 'session', 'id': 'pi-session-1', 'timestamp': self.at, 'cwd': str(self.project)},
            {'type': 'message', 'id': 'm1', 'timestamp': self.at,
             'message': {'role': 'assistant', 'responseId': 'pi-response-1', 'stopReason': 'stop', 'usage': {'input': 12}}},
        ]
        write_jsonl(self.pi, self.pi_rows)
        self.store.ingest(parse_native('pi', self.pi), {'content': False, 'retentionDays': 0, 'clearedBefore': 0})
        entries = {}
        for agent in ('claude', 'codex', 'pi', 'dsh', 'opencode'):
            entries[agent] = {'id': agent, 'agent': agent, 'kind': 'cli', 'path': sys.executable,
                              'launch': [sys.executable], 'activePath': True,
                              'version': '1.4.0' if agent == 'pi' else 'test'}
        self.inventory = SimpleNamespace(instances=entries, checked_at=time.time(),
                                         lock=threading.RLock(), home=self.home)

    def test_recent_returns_real_index_and_draft_reads_on_demand_with_redaction(self):
        data = recent_sessions(self.store)
        self.assertEqual(data['total'], 2)
        self.assertEqual({x['agent'] for x in data['sessions']}, {'pi', 'codex'})
        draft = draft_handoff(self.store, 'codex', 'codex-session-1', 'pi')
        self.assertEqual(draft['source'], 'native-on-demand')
        self.assertEqual(draft['generatedBy'], 'structured-excerpts')
        self.assertIn('完整任务', draft['text'])
        self.assertNotIn('fixture-secret-private', draft['text'])
        self.assertIn('不是智能摘要', draft['text'])
        self.assertIn('请核对并补充', draft['text'])
        self.assertEqual(draft['includedCount'], 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows console launch')
    def test_resume_uses_native_source_and_new_interactive_console(self):
        preview = preview_resume(self.store, 'codex', 'codex-session-1', self.inventory)
        self.assertTrue(preview['available'])
        self.assertEqual(preview['command'][-2:], ['resume', 'codex-session-1'])
        pi = preview_resume(self.store, 'pi', 'pi-session-1', self.inventory)
        self.assertEqual(pi['command'][-2:], ['--session', str(self.pi.resolve())])
        with patch('session_handoff.subprocess.Popen') as popen:
            result = resume_session(self.store, 'codex', 'codex-session-1', self.inventory)
        self.assertTrue(result['launched'])
        self.assertEqual(popen.call_args.kwargs['cwd'], str(self.project.resolve()))
        self.assertEqual(popen.call_args.kwargs['creationflags'], 0x10)
        self.assertFalse(popen.call_args.kwargs['shell'])

    @unittest.skipUnless(os.name == 'nt', 'Windows console launch')
    def test_default_handoff_opens_empty_target_and_never_passes_text(self):
        capabilities = handoff_target_capabilities(self.inventory, 'codex')
        self.assertTrue(capabilities['openSupported'])
        self.assertFalse(capabilities['startSupported'])
        with patch('session_handoff.subprocess.Popen') as popen:
            result = launch_target(self.store, 'codex', 'codex-session-1', 'claude', self.inventory)
        self.assertTrue(result['launched'])
        self.assertEqual(popen.call_args.args[0], [sys.executable])
        self.assertEqual(popen.call_args.kwargs['cwd'], str(self.project.resolve()))
        self.assertIn('尚未发送', result['message'])

    @unittest.skipUnless(os.name == 'nt', 'Windows console launch')
    def test_start_pi_delivers_only_file_path_and_cleans_after_exit(self):
        content = '已确认的交接内容; $(never-execute) & secret'
        process = Process()
        with patch('session_handoff.subprocess.Popen', return_value=process) as popen:
            response = start_handoff(self.store, 'codex', 'codex-session-1', 'pi', content, self.inventory)
        args = popen.call_args.args[0]
        self.assertTrue(args[-1].startswith('@'))
        self.assertNotIn(content, repr(args))
        path = Path(args[-1][1:])
        self.assertEqual(path.read_text(encoding='utf-8'), content)
        self.assertEqual(popen.call_args.kwargs['cwd'], str(self.project.resolve()))
        self.assertIn('可能残留', response['message'])
        process.done.set()
        deadline = time.monotonic() + 3
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(path.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows console launch')
    def test_path_prompt_starts_fake_client_without_sensitive_argv(self):
        # Real, isolated fake client simulates a model using the file-read tool.
        # It checks transport only; it does not assert real model compliance.
        script = self.home / 'fake_client.py'
        script.write_text('''import hashlib, json, os, sys\nfrom pathlib import Path\nresult = Path(sys.argv[1])\nprompt = sys.argv[-1]\npath = Path(json.loads(prompt.split("HANDOFF_FILE_JSON=", 1)[1]))\nbody = path.read_bytes()\nresult.write_text(json.dumps({"hash": hashlib.sha256(body).hexdigest(), "argv": sys.argv[2:], "cwd": os.getcwd()}), encoding="utf-8")\n''', encoding='utf-8')
        content = '已确认的私密交接内容 & $() 多行\n第二行'
        expected_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        real_popen = subprocess.Popen
        for target, version in [('claude', '2.1.312'), ('codex', '0.165.0'), ('opencode', '2.2.1')]:
            with self.subTest(target=target):
                outcome = self.home / (target + '-outcome.json')
                self.inventory.instances[target].update(version=version, launch=[sys.executable, str(script), str(outcome)])
                children = []

                def fake_popen(command, **kwargs):
                    self.assertEqual(kwargs['creationflags'], 0x10)  # real launch is a visible console
                    self.assertFalse(kwargs['shell'])
                    self.assertNotIn(content, repr(command))
                    kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW  # isolated fake needs no console
                    child = real_popen(command, **kwargs)
                    children.append(child)
                    return child

                cap = handoff_target_capabilities(self.inventory, target)
                self.assertTrue(cap['startSupported'])
                self.assertIn('权限', cap['reason'])
                with patch('session_handoff.subprocess.Popen', side_effect=fake_popen):
                    response = start_handoff(self.store, 'codex', 'codex-session-1', target, content, self.inventory)
                self.assertEqual(children[0].wait(timeout=10), 0)
                result = json.loads(outcome.read_text(encoding='utf-8'))
                self.assertEqual(result['hash'], expected_hash)
                self.assertEqual(result['cwd'], str(self.project.resolve()))
                self.assertNotIn(content, repr(result['argv']))
                self.assertIn('确认读取', response['message'])
                if target == 'opencode':
                    self.assertEqual(result['argv'][:2], ['mini', '--prompt'])

    def test_changed_native_session_or_project_is_rejected(self):
        self.codex_rows[0]['payload']['id'] = 'a-different-id'
        write_jsonl(self.codex, self.codex_rows)
        with self.assertRaisesRegex(ValueError, '身份'):
            preview_resume(self.store, 'codex', 'codex-session-1', self.inventory)
        self.codex_rows[0]['payload']['id'] = 'codex-session-1'
        self.codex_rows[0]['payload']['cwd'] = str(self.home)
        write_jsonl(self.codex, self.codex_rows)
        with self.assertRaisesRegex(ValueError, '目录与索引不一致'):
            preview_resume(self.store, 'codex', 'codex-session-1', self.inventory)

    def test_index_cannot_redirect_source_outside_native_roots(self):
        outside = self.home / 'outside.jsonl'
        write_jsonl(outside, self.codex_rows)
        with self.store.db() as db:
            raw = db.execute('SELECT data FROM sessions WHERE id=?', (key('codex', 'codex-session-1'),)).fetchone()[0]
            row = json.loads(raw)
            row['source'] = str(outside)
            db.execute('UPDATE sessions SET data=? WHERE id=?', (json.dumps(row), key('codex', 'codex-session-1')))
        with self.assertRaisesRegex(ValueError, '不在客户端允许'):
            preview_resume(self.store, 'codex', 'codex-session-1', self.inventory)

    def test_rejects_untrusted_and_unsupported_launch(self):
        self.inventory.instances['pi']['version'] = '9.0.0'
        cap = handoff_target_capabilities(self.inventory, 'pi')
        self.assertFalse(cap['startSupported'])
        with self.assertRaises(ValueError):
            start_handoff(self.store, 'codex', 'codex-session-1', 'pi', '正文', self.inventory)
        with self.assertRaises(ValueError):
            draft_handoff(self.store, 'codex', 'codex-session-1', 'unknown')
        with self.assertRaises(ValueError):
            start_handoff(self.store, 'codex', 'codex-session-1', 'pi', '\x00', self.inventory)

    @unittest.skipUnless(os.name == 'nt', 'Windows console launch')
    def test_opencode_v2_resume_and_empty_target_use_documented_mini(self):
        database = self.home / '.local/share/opencode/opencode.db'
        database.parent.mkdir(parents=True, exist_ok=True)
        now = int((time.time() - 5) * 1000)
        db = sqlite3.connect(database)
        try:
            db.executescript('''CREATE TABLE session_v2(id TEXT PRIMARY KEY,parent_id TEXT,directory TEXT,title TEXT,time_created INTEGER);
                CREATE TABLE session_message(id TEXT PRIMARY KEY,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,data TEXT);''')
            db.execute('INSERT INTO session_v2 VALUES(?,?,?,?,?)', ('ses_example', None, str(self.project), None, now))
            payload = {'model': {'id': 'example'}, 'finish': 'stop', 'time': {'created': now, 'completed': now + 1},
                       'tokens': {'input': 1, 'output': 1, 'reasoning': 0}}
            db.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?)', ('m', 'ses_example', 'assistant', 1, now, json.dumps(payload)))
            db.commit()
        finally:
            db.close()
        self.inventory.instances['opencode']['version'] = '2.2.1'
        with patch.dict(os.environ, {'OPENCODE_DB': str(database)}):
            self.store.ingest(parse_opencode_database(database, False)[0],
                              {'content': False, 'retentionDays': 0, 'clearedBefore': 0})
            plan = preview_resume(self.store, 'opencode', 'ses_example', self.inventory)
            self.assertEqual(plan['command'][-3:], ['mini', '--session', 'ses_example'])
            with patch('session_handoff.subprocess.Popen') as popen:
                launch_target(self.store, 'opencode', 'ses_example', 'opencode', self.inventory)
            self.assertEqual(popen.call_args.args[0][-1:], ['mini'])

    @unittest.skipUnless(os.name == 'nt', 'Windows console launch')
    def test_dsh_requires_installed_supported_profile_and_opens_it(self):
        source = self.home / '.dsh/sessions/project/dsh.jsonl'
        now = int((time.time() - 5) * 1000)
        rows = [
            {'type': 'session', 'version': 4, 'id': 'dsh-session-1', 'cwd': str(self.project), 'createdAt': now},
            {'type': 'assistant/message', 'seq': 1, 'time': now + 1,
             'data': {'message': {'role': 'assistant', 'content': [], 'source': {}}, 'usage': {'inputTokens': 10}}},
        ]
        write_jsonl(source, rows)
        self.store.ingest(parse_native('dsh', source), {'content': False, 'retentionDays': 0, 'clearedBefore': 0})
        self.assertFalse(preview_resume(self.store, 'dsh', 'dsh-session-1', self.inventory)['available'])
        profile = self.home / '.dsh/profiles/tui'
        pkg = profile / 'node_modules/@dsh-tui/dsh-tui/package.json'
        pkg.parent.mkdir(parents=True)
        pkg.write_text('{}', encoding='utf-8')
        (profile / 'package.json').write_text(json.dumps({'dependencies': {'@dsh-tui/dsh-tui': '1'}}), encoding='utf-8')
        plan = preview_resume(self.store, 'dsh', 'dsh-session-1', self.inventory)
        self.assertEqual(plan['command'][-4:], ['--profile', 'tui', '--resume', 'dsh-session-1'])
        with patch('session_handoff.subprocess.Popen') as popen:
            launch_target(self.store, 'dsh', 'dsh-session-1', 'dsh', self.inventory)
        self.assertEqual(popen.call_args.args[0][-2:], ['--profile', 'tui'])


if __name__ == '__main__':
    unittest.main()
