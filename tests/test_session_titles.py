"""Session list title derivation from synthetic native data only."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from observation import Store, parse_native, parse_opencode_database
from session_handoff import recent_sessions
import session_titles as titles


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    return path


class SessionTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-title-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = patch.dict(os.environ, {'CODEX_HOME': '', 'CLAUDE_CONFIG_DIR': '', 'DSH_HOME': '',
                                      'PI_CODING_AGENT_DIR': '', 'OPENCODE_DB': '', 'XDG_DATA_HOME': ''})
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time() - 30
        self.store = Store(self.home / 'hub', self.home,
                           config=lambda: {'providers': {'demo': {'apiKey': 'fixture-secret-never-expose'}}})
        self.addCleanup(self.store.close)
        self.store.settings_path.write_text(json.dumps(
            {'enabled': False, 'content': False, 'retentionDays': 0, 'clearedBefore': 0}), encoding='utf-8')

    def _ingest(self, agent, path):
        self.store.ingest(parse_native(agent, path, False), self.store.settings())

    def _codex(self, sid='c1', user='改进菜单选择', extra=None):
        ts = self.now
        path = self.home / '.codex/sessions/2026/01/01' / (sid + '.jsonl')
        rows = [{'type': 'session_meta', 'timestamp': iso(ts), 'payload': {'id': sid, 'cwd': str(self.home)}},
                {'type': 'response_item', 'timestamp': iso(ts+1), 'payload': {'role': 'user',
                   'content': [{'type': 'input_text', 'text': user}]}},
                *(extra or []),
                {'type': 'token_usage_record', 'timestamp': iso(ts+5),
                 'payload': {'thread_id': sid, 'response_id': sid+'-response',
                             'usage': {'input_tokens': 10, 'output_tokens': 1}}}]
        self._ingest('codex', write_rows(path, rows))
        return path

    def test_no_extra_disk_copy_and_name_source(self):
        self._codex(user='修复加载问题 api_key=fixture-secret-never-expose')
        rows = recent_sessions(self.store)['sessions']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['titleSource'], 'user-message')
        self.assertIn('修复加载问题', rows[0]['title'])
        self.assertNotIn('fixture-secret-never-expose', rows[0]['title'])
        self.assertNotIn('source', rows[0])
        with self.store.db() as db:
            stored = '\n'.join(r[0] for r in db.execute('SELECT data FROM sessions'))
        self.assertNotIn('修复加载问题', stored)
        self.assertNotIn('fixture-secret-never-expose', stored)

    def test_native_title_wins_and_generic_or_tools_are_not_titles(self):
        ts = self.now
        path = self.home / '.claude/projects/demo/a.jsonl'
        rows = [
            {'type': 'user', 'sessionId': 'a', 'timestamp': iso(ts),
             'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': '工具输出不是标题'}]}},
            {'type': 'user', 'sessionId': 'a', 'timestamp': iso(ts+1), 'message': {'content': '继续'}},
            {'type': 'user', 'sessionId': 'a', 'timestamp': iso(ts+2), 'message': {'content': '调查登录慢的问题'}},
            {'type': 'ai-title', 'sessionId': 'a', 'timestamp': iso(ts+3), 'aiTitle': '客户端已有标题'},
            {'type': 'assistant', 'sessionId': 'a', 'timestamp': iso(ts+4),
             'message': {'id': 'a-r', 'model': 'x', 'stop_reason': 'end_turn', 'usage': {'input_tokens': 1}}}]
        self._ingest('claude', write_rows(path, rows))
        row = recent_sessions(self.store)['sessions'][0]
        self.assertEqual((row['title'], row['titleSource']), ('客户端已有标题', 'native-title'))

    def test_automatic_injected_content_and_short_generic_messages_are_skipped(self):
        ts = self.now
        extra = [
            {'type':'response_item','timestamp':iso(ts+2),'payload':{'role':'user','content':[{'type':'input_text','text':'继续'}]}},
            {'type':'response_item','timestamp':iso(ts+3),'payload':{'role':'user','content':[{'type':'input_text','text':str(self.home / 'path') }]}},
            {'type':'response_item','timestamp':iso(ts+4),'payload':{'role':'user','content':[{'type':'input_text','text':'真正要做的是修复标题显示'}]}}]
        self._codex(user='# AGENTS.md instructions\n禁止把它作为任务标题', extra=extra)
        row = recent_sessions(self.store)['sessions'][0]
        self.assertEqual(row['title'], '真正要做的是修复标题显示')
        self.assertEqual(row['titleSource'], 'user-message')

    def test_embedded_system_block_removed_and_long_user_text_truncated(self):
        text = '<environment_context>不应显示</environment_context> 我想整理一个真正的长任务：' + '扩展支持' * 40
        self._codex(user=text)
        row = recent_sessions(self.store)['sessions'][0]
        self.assertNotIn('不应显示', row['title'])
        self.assertTrue(row['title'].startswith('我想整理'))
        self.assertTrue(row['title'].endswith('…'))
        self.assertLessEqual(len(row['title']), 61)
        self.assertEqual(titles._clean_title('用户任务 <system>未结束的注入块'), '用户任务')

    def test_no_usable_user_text_does_not_fabricate_title(self):
        self._codex(user='继续')
        row = recent_sessions(self.store)['sessions'][0]
        self.assertIsNone(row['title'])
        self.assertEqual(row['titleSource'], 'unavailable')

    def test_labels_private_keys_and_chinese_secrets(self):
        for label in ('#标签', '@agent', '/compact', 'c:/private/file', '继续工作。',
                      '未命名会话', 'Untitled session'):
            with self.subTest(label=label):
                self.assertIsNone(titles._clean_title(label))
        candidate = titles._clean_title('排查 密钥：fixture-unlisted-secret '
            '-----BEGIN PRIVATE KEY-----fixture-private-key-----END PRIVATE KEY-----')
        self.assertIn('排查', candidate)
        self.assertNotIn('fixture-unlisted-secret', candidate)
        self.assertNotIn('fixture-private-key', candidate)

    def test_secret_lookup_failure_fails_closed(self):
        self._codex(user='保护这个合成内容')
        with patch.object(self.store, 'secrets', side_effect=RuntimeError('unavailable')):
            with patch.object(titles, '_native_file_title', wraps=titles._native_file_title) as parse:
                row = recent_sessions(self.store)['sessions'][0]
                self.assertEqual((row['title'], row['titleSource']), (None, 'unavailable'))
                self.assertEqual(parse.call_count, 0)

    def test_pi_and_dsh_titles_and_dsh_seed_ownership(self):
        ts = self.now
        pi = [
            {'type':'session','id':'pi-a','timestamp':iso(ts),'cwd':str(self.home)},
            {'type':'message','id':'p-u','timestamp':iso(ts+1),'message':{'role':'user','content':[{'type':'text','text':'整理测试结果'}]}},
            {'type':'session_info','timestamp':iso(ts+2),'name':'pi 原生命名'},
            {'type':'message','id':'p-r','timestamp':iso(ts+3),'message':{'role':'assistant','responseId':'p-response',
              'stopReason':'stop','usage':{'input':3,'output':1,'cacheRead':0,'cacheWrite':0}}}]
        dsh = [
            {'type':'session','version':4,'id':'d-a','cwd':str(self.home),'createdAt':int(ts*1000),'isSeeded':True},
            {'type':'user/message','seq':1,'time':int((ts+1)*1000),'data':{'content':'父会话内容不可冒充子会话'}},
            {'type':'session/end-seed','seq':2,'time':int((ts+2)*1000),'data':{'inherited':True}},
            {'type':'user/message','seq':3,'time':int((ts+3)*1000),'data':{'content':'当前任务：检查导入错误'}},
            {'type':'assistant/message','seq':4,'time':int((ts+4)*1000),'data':{
               'message':{'source':{'provider':'x','model':'y'}},'usage':{'inputTokens':1,'outputTokens':1}}}]
        self._ingest('pi', write_rows(self.home/'.pi/agent/sessions/project/p.jsonl', pi))
        self._ingest('dsh', write_rows(self.home/'.dsh/sessions/project/d.jsonl', dsh))
        by_agent={x['agent']: x for x in recent_sessions(self.store)['sessions']}
        self.assertEqual((by_agent['pi']['title'], by_agent['pi']['titleSource']), ('pi 原生命名', 'native-title'))
        self.assertEqual((by_agent['dsh']['title'], by_agent['dsh']['titleSource']), ('当前任务：检查导入错误', 'user-message'))

    def test_pi_latest_cleared_or_filtered_name_does_not_revive_an_older_title(self):
        ts = self.now
        for new_name in ('', '继续'):
            with self.subTest(new_name=new_name):
                sid = 'pi-clear-' + ('empty' if not new_name else 'generic')
                rows = [
                    {'type':'session','id':sid,'timestamp':iso(ts),'cwd':str(self.home)},
                    {'type':'message','id':'user','timestamp':iso(ts+1),
                     'message':{'role':'user','content':'当前用户任务'}},
                    {'type':'session_info','id':'old','timestamp':iso(ts+2),'name':'不再使用的旧标题'},
                    {'type':'session_info','id':'clear','timestamp':iso(ts+3),'name':new_name},
                    {'type':'message','id':'answer','timestamp':iso(ts+4),'message':{
                        'role':'assistant','responseId':sid+'-response','stopReason':'stop',
                        'usage':{'input':1,'output':1,'cacheRead':0,'cacheWrite':0}}}]
                path=write_rows(self.home/'.pi/agent/sessions/project'/(sid+'.jsonl'),rows)
                self._ingest('pi',path)
                row=next(x for x in recent_sessions(self.store)['sessions'] if x['sessionId']==sid)
                self.assertEqual((row['title'],row['titleSource']),('当前用户任务','user-message'))

    def test_pi_clear_after_watermark_cannot_revive_pre_clear_title_or_message(self):
        ts = self.now
        rows = [
            {'type':'session','id':'pi-retention','timestamp':iso(ts),'cwd':str(self.home)},
            {'type':'message','id':'user','timestamp':iso(ts+1),
             'message':{'role':'user','content':'清理前的用户任务'}},
            {'type':'session_info','id':'old','timestamp':iso(ts+2),'name':'清理前的标题'},
            {'type':'session_info','id':'clear','timestamp':iso(ts+4),'name':''},
            {'type':'message','id':'answer','timestamp':iso(ts+5),'message':{
                'role':'assistant','responseId':'pi-retention-response','stopReason':'stop',
                'usage':{'input':1,'output':1,'cacheRead':0,'cacheWrite':0}}}]
        self._ingest('pi',write_rows(self.home/'.pi/agent/sessions/project/retention.jsonl',rows))
        self.store.settings_path.write_text(json.dumps({'enabled':False,'content':False,'retentionDays':0,
                                                       'clearedBefore':ts+3}),encoding='utf-8')
        row=recent_sessions(self.store)['sessions'][0]
        self.assertEqual((row['title'],row['titleSource']),(None,'unavailable'))

    def test_clear_watermark_and_retention_never_resurrect_old_messages(self):
        ts = self.now
        more = [{'type':'response_item','timestamp':iso(ts+3),'payload':{'role':'user',
                 'content':[{'type':'input_text','text':'新任务：复核剩余额度'}]}}]
        self._codex(user='旧任务：不应在清理后出现', extra=more)
        self.store.settings_path.write_text(json.dumps({'enabled':False,'content':False,'retentionDays':0,
                                                       'clearedBefore':ts+2}),encoding='utf-8')
        row=recent_sessions(self.store)['sessions'][0]
        self.assertEqual(row['title'], '新任务：复核剩余额度')
        self.assertNotIn('旧任务', row['title'])

    def test_stored_native_title_without_its_own_timestamp_cannot_bypass_clear(self):
        ts = self.now
        self.store.settings_path.write_text(json.dumps({'enabled':False,'content':True,'retentionDays':0,
                                                       'clearedBefore':0}),encoding='utf-8')
        records = [
            {'type':'session','id':'pi-clear','timestamp':iso(ts),'cwd':str(self.home)},
            {'type':'session_info','timestamp':iso(ts+1),'name':'清除前原生标题'},
            {'type':'message','id':'new','timestamp':iso(ts+4),
             'message':{'role':'user','content':'清除后当前任务'}},
            {'type':'message','id':'answer','timestamp':iso(ts+5),'message':{
                'role':'assistant','responseId':'answer','stopReason':'stop',
                'usage':{'input':1,'output':1,'cacheRead':0,'cacheWrite':0}}}]
        path=write_rows(self.home/'.pi/agent/sessions/project/clear.jsonl', records)
        self.store.ingest(parse_native('pi',path,True), self.store.settings())
        self.store.settings_path.write_text(json.dumps({'enabled':False,'content':True,'retentionDays':0,
                                                       'clearedBefore':ts+3}),encoding='utf-8')
        row=recent_sessions(self.store)['sessions'][0]
        self.assertEqual((row['title'],row['titleSource']),('清除后当前任务','user-message'))

    def test_watermark_rechecked_after_inflight_source_read(self):
        self._codex(user='清除期间的旧任务')
        original = titles._native_file_title
        def clear_during_read(*args):
            result = original(*args)
            self.store.settings_path.write_text(json.dumps({'enabled':False,'content':False,'retentionDays':0,
                                                           'clearedBefore':self.now+2}),encoding='utf-8')
            return result
        with patch.object(titles, '_native_file_title', side_effect=clear_during_read):
            row=recent_sessions(self.store)['sessions'][0]
        self.assertEqual((row['title'],row['titleSource']), (None, 'unavailable'))
        self.assertNotIn('_titleAt', row)

    def test_pi_fork_does_not_use_inherited_parent_message(self):
        ts = self.now
        records = [
            {'type':'session','id':'pi-child','parentSession':'parent.jsonl',
             'timestamp':iso(ts+3),'cwd':str(self.home)},
            {'type':'message','id':'old','timestamp':iso(ts+1),
             'message':{'role':'user','content':'父任务不能作为子会话标题'}},
            {'type':'message','id':'uncertain',
             'message':{'role':'user','content':'缺少时间戳不能确认所有权'}},
            {'type':'message','id':'new','timestamp':iso(ts+4),
             'message':{'role':'user','content':'子任务：检查配置'}},
            {'type':'message','id':'answer','timestamp':iso(ts+5),'message':{
                'role':'assistant','responseId':'answer','stopReason':'stop',
                'usage':{'input':1,'output':1,'cacheRead':0,'cacheWrite':0}}}]
        self._ingest('pi',write_rows(self.home/'.pi/agent/sessions/project/fork.jsonl',records))
        row=recent_sessions(self.store)['sessions'][0]
        self.assertEqual((row['title'],row['titleSource']), ('子任务：检查配置','user-message'))

    def test_damaged_source_isolated_and_compressed_source_bounded(self):
        path=self._codex()
        with patch.object(titles, '_segments', side_effect=RuntimeError('corrupt source')):
            row=recent_sessions(self.store)['sessions'][0]
            self.assertEqual((row['title'],row['titleSource']), (None,'unavailable'))
        try:
            import zstandard
        except ImportError:
            self.skipTest('zstandard is optional')
        compressed=path.with_suffix('.jsonl.zst')
        compressed.write_bytes(zstandard.ZstdCompressor().compress(path.read_bytes()))
        chunks=titles._segments(compressed)
        self.assertLessEqual(len(chunks[0]),titles._READ_BYTES)
        title, source, at = titles._native_file_title(compressed,'codex','c1',0,())
        self.assertEqual((title, source), ('改进菜单选择','user-message'))
        self.assertAlmostEqual(at, self.now+1, places=5)

    def test_dense_file_keeps_header_and_latest_native_title(self):
        ts=self.now
        rows=[{'type':'user','sessionId':'dense','timestamp':iso(ts),
               'message':{'role':'user','content':'继续'}}]
        rows.extend({'type':'user','sessionId':'dense','timestamp':iso(ts+1),
                     'message':{'role':'user','content':'继续'}} for _ in range(3500))
        rows.append({'type':'ai-title','sessionId':'dense','timestamp':iso(ts+2),'aiTitle':'新原生标题'})
        path=write_rows(self.home/'.claude/projects/demo/dense.jsonl',rows)
        self.assertLess(path.stat().st_size, titles._READ_BYTES)
        title, source, _ = titles._native_file_title(path,'claude','dense',0,())
        self.assertEqual((title,source),('新原生标题','native-title'))

    def test_bad_identity_and_outside_path_are_not_read(self):
        path=self._codex()
        with self.store.db() as db:
            ident, data = db.execute('SELECT id,data FROM sessions').fetchone()
            row=json.loads(data)
            row['source']=str(self.home/'outside.jsonl')
            db.execute('UPDATE sessions SET data=? WHERE id=?',(json.dumps(row),ident))
        write_rows(self.home/'outside.jsonl', [{'type':'response_item','payload':{'role':'user','content':'不应显示'}}])
        row=recent_sessions(self.store)['sessions'][0]
        self.assertIsNone(row['title'])
        self.assertEqual(row['titleSource'], 'unavailable')
        # The legitimate directory but wrong native identity also stays unknown.
        with self.store.db() as db:
            data=json.loads(db.execute('SELECT data FROM sessions').fetchone()[0])
            data['source']=str(path)
            db.execute('UPDATE sessions SET data=?',(json.dumps(data),))
        raw=path.read_text(encoding='utf-8').replace('"id": "c1"', '"id": "other"')
        path.write_text(raw,encoding='utf-8')
        self.assertIsNone(recent_sessions(self.store)['sessions'][0]['title'])

    def test_cache_reuses_and_invalidates_source_without_changing_retention(self):
        path=self._codex()
        self.store.settings_path.write_text(json.dumps({'enabled':False,'content':False,'retentionDays':30,'clearedBefore':0}),encoding='utf-8')
        with patch.object(titles, '_native_file_title', wraps=titles._native_file_title) as parse:
            first=recent_sessions(self.store)['sessions'][0]
            second=recent_sessions(self.store)['sessions'][0]
            self.assertEqual(first['title'],second['title'])
            self.assertEqual(parse.call_count,1)
            with path.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'type':'event_msg','timestamp':iso(self.now+10),'payload':{}})+'\n')
            recent_sessions(self.store)
            self.assertEqual(parse.call_count,2)

    def test_scan_budget_defers_uncached_sources_and_later_poll_fills_them(self):
        first=self._codex('c1','整理本机配置')
        second=self._codex('c2','排查服务故障')
        items=[{'agent':'codex','sessionId':'c1','source':str(first)},
               {'agent':'codex','sessionId':'c2','source':str(second)}]
        with patch.object(titles.time, 'monotonic', side_effect=(0,0,3)):
            rows=titles.recent_titles(self.store,items)
        self.assertEqual(rows[0]['title'],'整理本机配置')
        self.assertEqual((rows[1]['title'],rows[1]['titleSource']),(None,'unavailable'))
        rows=titles.recent_titles(self.store,items)
        self.assertEqual(rows[1]['title'],'排查服务故障')

    def test_file_change_during_read_retries_on_next_poll(self):
        path=self._codex()
        original=titles._native_file_title
        def change_during_read(*args):
            result=original(*args)
            with path.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'type':'event_msg','timestamp':iso(self.now+11),'payload':{}})+'\n')
            return result
        with patch.object(titles, '_native_file_title', side_effect=change_during_read):
            row=recent_sessions(self.store)['sessions'][0]
        self.assertEqual((row['title'],row['titleSource']),(None,'unavailable'))
        self.assertEqual(recent_sessions(self.store)['sessions'][0]['title'],'改进菜单选择')

    def test_opencode_reads_native_title_or_user_text_from_wal_read_only(self):
        path=self.home/'.local/share/opencode/opencode.db'
        path.parent.mkdir(parents=True)
        db=sqlite3.connect(path)
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''CREATE TABLE session_v2(id TEXT PRIMARY KEY,parent_id TEXT,directory TEXT,title TEXT,time_created INTEGER);
          CREATE TABLE session_message(id TEXT PRIMARY KEY,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,data TEXT);''')
        ms=int(self.now*1000)
        db.execute('INSERT INTO session_v2 VALUES(?,?,?,?,?)',('o-a',None,str(self.home),'OpenCode 原生标题',ms))
        message={'model':{'id':'test','providerID':'test'},'finish':'stop',
                 'time':{'created':ms+1000,'completed':ms+2000},'tokens':{'input':1,'output':1,'reasoning':0}}
        db.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?)',('o-r','o-a','assistant',1,ms+1000,json.dumps(message)))
        db.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?)',('o-u','o-a','user',2,ms+1500,json.dumps({'text':'查找证据'})))
        db.commit()
        self.addCleanup(db.close)
        self.store.ingest(parse_opencode_database(path,False)[0], self.store.settings())
        first=recent_sessions(self.store)['sessions'][0]
        self.assertEqual((first['title'],first['titleSource']),('OpenCode 原生标题','native-title'))
        db.execute('UPDATE session_v2 SET title=? WHERE id=?',('更新的原生标题','o-a'))
        db.commit()
        second=recent_sessions(self.store)['sessions'][0]
        self.assertEqual(second['title'],'更新的原生标题')
        db.execute('UPDATE session_v2 SET title=NULL WHERE id=?',('o-a',))
        db.commit()
        third=recent_sessions(self.store)['sessions'][0]
        self.assertEqual((third['title'],third['titleSource']),('查找证据','user-message'))

        # A v2 database may lack a helpful message index. A long scan can be
        # interrupted and a later request can retry rather than hanging.
        db.executemany('INSERT INTO session_message VALUES(?,?,?,?,?,?)',
                       [('filler-'+str(i),'other','user',i,ms,json.dumps({'text':'ignored'}))
                        for i in range(5000)])
        db.commit()
        with patch.object(titles, '_SQL_BUDGET_SECONDS', -1):
            bounded=recent_sessions(self.store)['sessions'][0]
        self.assertEqual((bounded['title'],bounded['titleSource']),(None,'unavailable'))
        self.assertEqual(recent_sessions(self.store)['sessions'][0]['title'],'查找证据')


if __name__ == '__main__':
    unittest.main()
