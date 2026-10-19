import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from observation import Store, Parsed, parse_native, native_files, key
from daily_records import seed, write, iso


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.paths=seed(self.home)
        self.store=Store(self.home/'data',self.home)
        self.addCleanup(self.store.close)
        self.store.settings_path.write_text(json.dumps({'enabled':False,'content':False,'retentionDays':30,'clearedBefore':0}))
        # Direct ingest is used deliberately, so the collector cannot race test mutations.
        self.settings={'enabled':True,'content':False,'retentionDays':30,'clearedBefore':0}

    def import_all(self,content=False):
        for agent,path in self.paths:
            self.store.ingest(parse_native(agent,path,content,secrets=['fixture-secret-do-not-use']),{**self.settings,'content':content})

    def test_all_four_formats_feed_identical_observation_totals_and_identity(self):
        self.import_all()
        data=self.store.query()
        self.assertEqual(data['summary']['calls'],6)
        self.assertEqual(len(data['sessions']),4)
        self.assertEqual(sum(s['summary']['calls'] for s in data['sessions']),6)
        self.assertEqual(data['summary']['failed'],1)
        self.assertEqual(data['summary']['interrupted'],1)
        claude=next(r for r in data['requests'] if r['agent']=='claude')
        self.assertEqual(claude['usage']['output'],80) # two blocks are one response
        self.assertEqual(claude['usage']['context'],7500)
        codex=next(r for r in data['requests'] if r['agent']=='codex')
        self.assertEqual(codex['usage']['context'],10000) # cache is included already
        self.assertIsNone(codex['servedModel'])
        dsh=next(r for r in data['requests'] if r['agent']=='dsh')
        self.assertEqual(dsh['window'],128000)
        self.assertEqual(dsh['servedModel'],'dsh-actually-returned')
        self.assertEqual(self.store.detail(dsh['id'])['sessionId'],'dsh-native-session')
        self.assertEqual(self.store.query({'providerUnknown':'1'})['total'],1)

    def test_reimport_and_dsh_migrations_do_not_double_count(self):
        self.import_all(); self.import_all()
        self.assertEqual(self.store.query()['total'],6)
        with patch.dict('os.environ',{'CODEX_HOME':'','CLAUDE_CONFIG_DIR':'','PI_CODING_AGENT_DIR':'','DSH_HOME':''}):
            files,_=native_files(self.home)
        self.assertEqual(len([f for a,f in files if a=='dsh']),1)
        self.assertTrue(next(f for a,f in files if a=='dsh').name.startswith('session.v4'))

    def test_missing_prices_unknown_measurements_and_interruption(self):
        self.import_all()
        data=self.store.query()
        self.assertEqual(data['summary']['unpriced'],5)
        self.assertAlmostEqual(data['summary']['cost'],0.025)
        self.assertIsNone(data['summary']['retried'])
        self.assertIsNone(data['summary']['ttftMs'])
        self.assertIsNone(data['summary']['speed'])
        interrupted=self.store.query({'status':'interrupted'})
        self.assertEqual(interrupted['total'],1)
        self.assertEqual(interrupted['requests'][0]['usage']['output'],30)

    def test_summary_request_conversation_context_investigation(self):
        self.import_all()
        failed=self.store.query({'status':'failed','agent':'pi'})
        row=self.store.detail(failed['requests'][0]['id'])
        detail=self.store.session_detail(row['agent'],row['sessionId'],{'days':7})
        s=detail['session']
        point=next(p for p in s['points'] if p['id']==row['id'])
        self.assertEqual(s['summary']['calls'],3)
        self.assertEqual(point['input'],23000)
        self.assertIsNone(point['fill'])
        self.assertEqual(s['peakInput'],23000)
        self.assertEqual(len(s['compactions']),1)
        self.assertIn('429',row['error'])
        self.assertGreater(len(detail['messages']),0)
        self.assertEqual(detail['contentSource'],'native-on-demand')
        self.assertEqual(self.store.status()['contentRecords'],0)

    def test_content_opt_in_redaction_disable_and_clear_watermark(self):
        self.import_all(True)
        with self.store.db() as db:
            bodies=' '.join(x[0] for x in db.execute('SELECT data FROM messages'))
        self.assertNotIn('fixture-secret-do-not-use',bodies)
        self.assertIn('REDACTED',bodies)
        self.store.set_settings({'content':False})
        self.assertEqual(self.store.status()['contentRecords'],0)
        self.store.cleanup(clear=True)
        self.import_all()
        # A caller must respect saved settings, not pass an obsolete pre-clear snapshot.
        self.store.cleanup(clear=True)
        for agent,path in self.paths:self.store.ingest(parse_native(agent,path))
        self.assertEqual(self.store.query()['total'],0)
        self.assertFalse(self.store.settings()['enabled'])

    def test_retention_filters_unknown_session_and_native_zero_price(self):
        p=Parsed('pi',Path('native.jsonl'))
        p.request('unassociated',time.time()-2,{'input':0,'output':0,'cacheRead':0,'cacheWrite':0,'cost':{'total':0}},status='unknown')
        self.store.ingest(p,self.settings)
        data=self.store.query({'agent':'pi'})
        self.assertEqual(data['total'],1)
        self.assertEqual(data['sessions'],[])
        self.assertIsNone(data['requests'][0]['cost'])
        with self.assertRaises(ValueError):self.store.query({'agent':'other'})
        with self.assertRaises(ValueError):self.store.query({'start':time.time(),'end':time.time()-50})

    def test_dsh_seed_excluded_and_truncated_tail_not_completed(self):
        now=time.time()-30
        rows=[{'type':'session','version':4,'id':'fork','isSeeded':True,'parentSession':'main','delegationDepth':1,'createdAt':now*1000},
              {'type':'assistant/message','seq':1,'time':now*1000,'data':{'usage':{'inputTokens':100,'outputTokens':10}}},
              {'type':'session/end-seed','seq':2,'time':now*1000,'data':{'inherited':True}},
              {'type':'assistant/message','seq':3,'time':(now+2)*1000,'data':{'usage':{'inputTokens':200,'outputTokens':20}}}]
        path=write(self.home/'fork.jsonl',rows)
        with path.open('ab') as f:f.write(b'{"type":"assistant/message"')
        parsed=parse_native('dsh',path)
        self.assertEqual(len(parsed.requests),1)
        self.assertEqual(parsed.parent,'main')

    def test_explicit_same_response_final_success_does_not_remain_failed(self):
        p=Parsed('pi',Path('native'))
        p.sid='work'
        p.request('native-id',time.time()-5,{'input':100,'output':0},status='failed')
        p.request('native-id',time.time()-3,{'input':100,'output':10},status='succeeded')
        self.store.ingest(p,self.settings)
        s=self.store.query()['summary']
        self.assertEqual((s['calls'],s['failed'],s['succeeded']),(1,0,1))
        self.assertIsNone(s['attempts']) # Same id is proof of identity, not proof of retry count.

    def test_retention_change_removes_expired_events(self):
        p=Parsed('pi',Path('native'));p.sid='older'
        p.request('old',time.time()-20*86400,{'input':10,'output':1})
        self.store.ingest(p,self.settings)
        self.assertEqual(self.store.query({'days':30})['total'],1)
        self.store.set_settings({'retentionDays':7})
        self.assertEqual(self.store.query({'days':30})['total'],0)

    def test_real_collector_toggle_and_repeated_refresh(self):
        with patch.dict('os.environ',{'CODEX_HOME':'','CLAUDE_CONFIG_DIR':'','PI_CODING_AGENT_DIR':'','DSH_HOME':''}):
            self.store.set_settings({'enabled':True})
            self.store.worker.join(timeout=10)
            self.assertEqual(self.store.status()['records'],6)
            self.store.refresh();self.store.worker.join(timeout=10)
            self.assertEqual(self.store.status()['records'],6)
            self.store.set_settings({'enabled':False})
            extra=self.home/'.pi/agent/sessions/project/extra.jsonl'
            write(extra,[{'type':'session','id':'new','timestamp':iso(time.time()-2)},
                         {'type':'message','id':'new','timestamp':iso(time.time()-1),'message':{'role':'assistant','responseId':'new','usage':{'input':10,'output':1},'stopReason':'stop'}}])
            self.store.refresh()
            self.assertEqual(self.store.status()['records'],6)
            self.store.close()

    def test_legacy_codex_usage_snapshots_do_not_shift_call_to_later_activity(self):
        now=time.time()-50
        u={'input_tokens':10,'output_tokens':2,'cached_input_tokens':0}
        path=write(self.home/'legacy.jsonl',[
            {'type':'session_meta','timestamp':iso(now),'payload':{'id':'legacy'}},
            {'type':'turn_context','timestamp':iso(now),'payload':{'model':'native-model'}},
            *[{'type':'event_msg','timestamp':iso(now+delta),'payload':{'type':'token_count','info':{'last_token_usage':u,'total_token_usage':u}}} for delta in (1,20)]])
        rows=list(parse_native('codex',path).requests.values())
        self.assertEqual(len(rows),1)
        self.assertAlmostEqual(rows[0]['time'],now+1,places=4)


if __name__=='__main__':unittest.main()
