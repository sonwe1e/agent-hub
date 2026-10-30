"""Magpie-style local defaults: usage on, native text on demand, copies bounded."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import observation
from observation import Store, parse_native, CONTENT_DAYS, CONTENT_MAX_BYTES
from daily_records import write, iso


class ObservationDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.store=Store(self.home/'data',self.home,config=lambda:{'providers':{'test':{'apiKey':'private-read-key'}}})
        self.addCleanup(self.store.close)
        self.path=self.home/'.pi/agent/sessions/work/session.jsonl'
        self.now=time.time()-5
        self.paused={**observation.DEFAULTS,'enabled':False}

    def session(self, at=None):
        at=at or self.now
        rows=[{'type':'session','id':'native-work','timestamp':iso(at),'cwd':'/workspace'},
              {'type':'message','id':'user-a','timestamp':iso(at+1),'message':{'role':'user','content':'Native text private-read-key'}},
              {'type':'message','id':'answer-a','timestamp':iso(at+2),'message':{'role':'assistant','responseId':'resp-a',
               'model':'known','provider':'test','stopReason':'stop','content':'answer',
               'usage':{'input':10,'output':2,'cacheRead':0,'cacheWrite':0}}}]
        write(self.path,rows)
        return parse_native('pi',self.path,True)

    def test_defaults_match_local_usage_and_separate_recording(self):
        s=self.store.status()
        self.assertTrue(s['settings']['enabled'])
        self.assertFalse(s['settings']['content'])
        self.assertEqual(s['settings']['retentionDays'],0)
        self.assertEqual((s['contentRetentionDays'],s['contentMaxBytes']),(7,256*1024*1024))

    def test_old_usage_survives_but_extra_text_expires(self):
        parsed=self.session(time.time()-400*86400)
        self.store.ingest(parsed,{**self.paused,'content':True})
        data=self.store.query({'days':'all'})
        self.assertEqual(data['total'],1)
        self.assertEqual(self.store.status()['contentRecords'],0)
        native=self.store.session_detail('pi','native-work',{'days':'all'})
        self.assertEqual(native['contentSource'],'native-on-demand')
        self.assertEqual(len(native['messages']),2)

    def test_native_read_does_not_create_a_body_copy_or_enable_collection(self):
        self.store.settings_path.write_text(json.dumps(self.paused), encoding="utf-8")
        self.store.ingest(self.session(),self.paused)
        result=self.store.session_detail('pi','native-work')
        self.assertEqual(result['contentSource'],'native-on-demand')
        self.assertIn('REDACTED',result['messages'][0]['body'])
        self.assertNotIn('private-read-key',json.dumps(result))
        self.assertFalse(self.store.settings()['enabled'])
        self.assertEqual(self.store.status()['contentRecords'],0)

    def test_missing_native_file_uses_only_unexpired_opt_in_copy(self):
        s={**self.paused,'content':True}
        self.store.settings_path.write_text(json.dumps(s), encoding="utf-8")
        self.store.ingest(self.session(),s)
        self.path.unlink()
        result=self.store.session_detail('pi','native-work')
        self.assertEqual(result['contentSource'],'retained')
        self.assertTrue(result['contentError'])
        self.assertEqual(len(result['messages']),2)
        self.store.set_settings({'content':False})
        self.assertFalse(self.store.session_detail('pi','native-work')['messages'])

    def test_body_limit_discards_oldest_records(self):
        s={**self.paused,'content':True}
        parsed=self.session()
        self.store.settings_path.write_text(json.dumps(s), encoding="utf-8")
        self.store.ingest(parsed,s)
        limit=max(len(json.dumps(m).encode()) for m in parsed.messages)+1
        with patch.object(observation,'CONTENT_MAX_BYTES',limit):
            self.store.cleanup()
            status=self.store.status()
        self.assertLessEqual(status['contentBytes'],limit)
        self.assertEqual(status['contentRecords'],1)
        with self.store.db() as db:
            remaining=json.loads(db.execute('SELECT data FROM messages').fetchone()[0])
        self.assertEqual(remaining['role'],'assistant')

    def test_copy_older_than_seven_days_is_removed_without_removing_usage(self):
        s={**self.paused,'content':True}
        self.store.settings_path.write_text(json.dumps(s), encoding="utf-8")
        self.store.ingest(self.session(),s)
        with self.store.db() as db:
            db.execute('UPDATE messages SET at=?',(time.time()-8*86400,))
        self.store.cleanup()
        self.assertEqual(self.store.status()['contentRecords'],0)
        self.assertEqual(self.store.query()['total'],1)

    def test_native_text_respects_clear_watermark(self):
        self.store.settings_path.write_text(json.dumps(self.paused), encoding="utf-8")
        self.store.ingest(self.session(),self.paused)
        self.store.cleanup(clear=True)
        mark=self.store.settings()['clearedBefore']
        parsed=self.session(mark+1)
        parsed.messages[0]['time']=mark-10
        # Persist a real pre-clear message in the native file as well.
        rows=[json.loads(l) for l in self.path.read_text(encoding='utf-8').splitlines()]
        rows[1]['timestamp']=iso(mark-10)
        write(self.path,rows)
        self.store.ingest(parse_native('pi',self.path),self.store.settings())
        result=self.store.session_detail('pi','native-work',{'start':mark-100,'end':mark+20})
        self.assertEqual(len(result['messages']),1)
        self.assertEqual(result['messages'][0]['role'],'assistant')

    def test_untrusted_source_outside_native_root_is_never_opened(self):
        parsed=self.session()
        self.store.ingest(parsed,self.paused)
        outside=self.home/'unrelated.jsonl'
        outside.write_bytes(self.path.read_bytes())
        with self.store.db() as db:
            sid,raw=db.execute('SELECT id,data FROM sessions').fetchone()
            value=json.loads(raw);value['source']=str(outside)
            db.execute('UPDATE sessions SET data=? WHERE id=?',(json.dumps(value),sid))
        self.assertFalse(self.store.session_detail('pi','native-work')['messages'])

    def test_native_payload_larger_than_gateway_body_bound_keeps_usage(self):
        rows=[{'type':'session_meta','timestamp':iso(self.now),'payload':{'id':'large-native','model_provider':'openai'}},
              {'type':'response_item','timestamp':iso(self.now),'payload':{'type':'message','role':'user',
               'content':[{'type':'input_image','image_url':'data:image/jpeg;base64,'+'A'*(9*1024*1024)}]}},
              {'type':'token_usage_record','timestamp':iso(self.now),'payload':{'thread_id':'large-native',
               'response_id':'resp-large','usage':{'input_tokens':123,'output_tokens':7}}}]
        path=write(self.home/'.codex/sessions/large.jsonl',rows)
        parsed=parse_native('codex',path)
        self.assertEqual(len(parsed.requests),1)
        self.assertEqual(next(iter(parsed.requests.values()))['usage']['input'],123)
