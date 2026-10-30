"""Regressions from 2026-10-17 installed-client probes (no vendor calls)."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from observation import Store, parse_native, redact


class LiveFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.at = time.time() - 2

    def file(self, rows, name='session.jsonl'):
        p = self.home/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
        return p

    def dsh(self, reason=None, stop=None):
        response = {'kind':'deepseek-messages','version':1,'model':'deepseek-flash'}
        if stop:
            response['stopReason'] = stop
        return [
            {'type':'session','version':4,'id':'real-format-dsh','createdAt':self.at-1,'cwd':'/isolated'},
            {'type':'request/context','time':self.at,'data':{'contextWindow':1000000}},
            {'type':'assistant/message','seq':17,'time':self.at,'data':{
                'turn':1,'step':1, 'usage':{'inputTokens':6123,'outputTokens':71,'cacheReadTokens':0,'cacheWriteTokens':0},
                'message':{'content':[{'type':'tool-call','id':'call-read','name':'read','arguments':'{"file_path":"probe.txt"}'}],
                           'source':{'provider':'deepseek-account','model':'deepseek-flash','replayState':{'response':response}}},
                'stream':[{'type':'chunk','time':self.at*1000,'chunk':{'type':'finish','reason':reason}}] if reason else []}},
            {'type':'tool/result','seq':18,'time':self.at+.1,'data':{'message':{
                'role':'tool','toolCallId':'call-read','content':[{'type':'text','text':'HUB_READ_EXCERPT'}],'isError':False}}},
        ]

    def test_deepseek_native_finish_is_explicit_request_outcome(self):
        for reason, expected in [('tool-calls','succeeded'),('stop','succeeded'),('length','succeeded'),
                                 ('error','failed'),('aborted','interrupted'),('cancelled','interrupted')]:
            with self.subTest(reason=reason):
                p = parse_native('dsh',self.file(self.dsh({'kind':reason,'error':{'message':'native error'}})),True)
                r = next(iter(p.requests.values()))
                self.assertEqual(r['status'],expected)
                self.assertEqual(r['usage']['context'],6123)
                self.assertEqual(r['window'],1000000)
        unknown = parse_native('dsh',self.file(self.dsh()))
        self.assertEqual(next(iter(unknown.requests.values()))['status'],'unknown')

    def test_dsh_tool_result_keeps_native_call_id(self):
        p = parse_native('dsh',self.file(self.dsh({'kind':'tool-calls'})),True)
        body = json.loads(p.messages[-1]['body'])
        self.assertEqual(body['toolCallId'],'call-read')
        self.assertFalse(body['isError'])
        self.assertIn('HUB_READ_EXCERPT',p.messages[-1]['body'])

    def test_codex_custom_tool_input_and_turn_timing_keep_distinct_scopes(self):
        rows = [
            {'type':'session_meta','timestamp':self.at-1,'payload':{'id':'live-codex','model_provider':'openai'}},
            {'type':'turn_context','timestamp':self.at,'payload':{'model':'gpt-6.1-sol'}},
            {'type':'response_item','timestamp':self.at,'payload':{'type':'custom_tool_call','id':'ctc-a','call_id':'call-a',
                'name':'exec','input':'text(await tools.exec_command({cmd:"Get-Content probe.txt"}));'}},
            {'type':'response_item','timestamp':self.at,'payload':{'type':'custom_tool_call_output','id':'ctco-a','call_id':'call-a','output':'HUB_READ_EXCERPT'}},
            {'type':'token_usage_record','timestamp':self.at,'payload':{'thread_id':'live-codex','turn_id':'turn-a','response_id':'response-a',
                'usage':{'input_tokens':14573,'output_tokens':60,'cached_input_tokens':12544,'cache_write_input_tokens':0,'reasoning_output_tokens':0}}},
            {'type':'event_msg','timestamp':self.at+1,'payload':{'type':'task_complete','turn_id':'turn-a','duration_ms':1200,'time_to_first_token_ms':400}},
        ]
        p = parse_native('codex',self.file(rows),True)
        tool = json.loads(p.messages[0]['body'])
        self.assertEqual(tool['call_id'],'call-a')
        self.assertIn('Get-Content',tool['content'])
        self.assertEqual(json.loads(p.messages[1]['body'])['call_id'],'call-a')
        self.assertEqual(p.messages[2]['role'],'task_complete')
        self.assertEqual(json.loads(p.messages[2]['body'])['duration_ms'],1200)
        r = next(iter(p.requests.values()))
        self.assertEqual(r['status'],'unknown')
        self.assertIsNone(r['durationMs'])  # turn timing is not individual response timing
        self.assertIsNone(r['ttftMs'])
        self.assertEqual(r['usage']['context'],14573)  # cached input already included

    def test_pi_tool_result_preserves_identity_and_error_flag(self):
        rows = [{'type':'session','id':'live-pi','timestamp':self.at},
                {'type':'message','id':'tool-a','timestamp':self.at,'message':{
                    'role':'toolResult','toolCallId':'call-read','toolName':'read','isError':False,
                    'durationMs':1,'content':[{'type':'text','text':'HUB_READ_EXCERPT'}]}}]
        p = parse_native('pi',self.file(rows),True)
        m = json.loads(p.messages[0]['body'])
        self.assertEqual(m['toolCallId'],'call-read')
        self.assertEqual(m['toolName'],'read')
        self.assertFalse(m['isError'])
        self.assertFalse(p.requests)  # a tool message is not a model request

    def test_parser_revision_reindexes_existing_sources(self):
        path = self.file(self.dsh({'kind':'stop'}),'.dsh/sessions/work/session.v4.jsonl')
        store = Store(self.home/'data',self.home)
        self.addCleanup(store.close)
        store.settings_path.write_text(json.dumps({'enabled':True,'content':False,'retentionDays':30,'clearedBefore':0}), encoding="utf-8")
        st = path.stat()
        with store.db() as db:
            db.execute('INSERT INTO sources VALUES(?,?)',(str(path),f'{st.st_size}:{st.st_mtime_ns}:False'))
        with patch.dict('os.environ',{'DSH_HOME':'','CODEX_HOME':'','PI_CODING_AGENT_DIR':'','CLAUDE_CONFIG_DIR':''}):
            store.collect()
        self.assertEqual(store.query()['summary']['succeeded'],1)

    def test_pi_failed_zero_usage_is_unconfirmed_not_free(self):
        rows = [{'type':'session','id':'rejected-model','timestamp':self.at},
                {'type':'message','id':'a','timestamp':self.at,'message':{'role':'assistant','model':'gpt-5.3-codex',
                 'provider':'openai','content':[],'stopReason':'error','errorMessage':'model not supported',
                 'usage':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0,'cost':{'total':0}}}}]
        p = parse_native('pi',self.file(rows),True)
        r = next(iter(p.requests.values()))
        self.assertEqual(r['status'],'failed')
        self.assertIsNone(r['usage']['input'])
        self.assertIsNone(r['usage']['context'])
        self.assertIsNone(r['cost'])
        self.assertIn('初始化值',r['gaps'][0])
        self.assertIn('model not supported',p.messages[0]['body'])

    def test_signed_urls_do_not_retain_authentication_query(self):
        value = redact('download failed https://example.org/file?sig=private-signature&token=credential')
        self.assertNotIn('private-signature',value)
        self.assertNotIn('credential',value)
        self.assertIn('https://example.org/file?',value)


if __name__ == '__main__':
    unittest.main()
