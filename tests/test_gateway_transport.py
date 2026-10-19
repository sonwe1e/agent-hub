"""Exercise actual sockets, streamed bytes and ledger semantics without vendors."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gateway_server import Server, Handler, State, Observation, packed
from gateway_runtime import Runtime, route_id
from gateway_observation import read_requests


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.calls=[]
        self.started=threading.Event()
        self.release=threading.Event()
        owner=self
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path,dict(self.headers),value))
                if value['model']=='fail':
                    data=packed({'error':{'message':'bad test-credential-value'}})
                    self.send_response(429);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                    return
                self.send_response(200)
                if not self.path.startswith('/backend-api/codex'):self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                prefix=b'data: {"id":"resp_exact","model":"served","choices":[{"delta":{"tool_calls":[{"id":"call_1","function":{"arguments":"{}"}}]}}]}\n\n'
                self.wfile.write(prefix);self.wfile.flush();owner.started.set()
                if value['model']=='slow':owner.release.wait(4)
                if value['model']=='cut':return
                suffix=b'data: {"id":"resp_exact","choices":[{"finish_reason":"tool_calls","delta":{}}],"usage":{"prompt_tokens":100,"completion_tokens":9,"prompt_tokens_details":{"cached_tokens":40}}}\n\ndata: [DONE]\n\n'
                self.wfile.write(suffix);self.wfile.flush()
        self.upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        threading.Thread(target=self.upstream.serve_forever,daemon=True).start()
        self.addCleanup(self.upstream.server_close);self.addCleanup(self.upstream.shutdown)
        self.rid=route_id('pi','test')
        self.directory=self.root/'config/agent-hub';self.directory.mkdir(parents=True)
        self.directory.joinpath('providers.json').write_bytes(packed({'providers':[{'id':self.rid,'key':'test-credential-value','chat':f'http://127.0.0.1:{self.upstream.server_port}/v1','models':['ok','slow','cut','fail']}]}))
        self.directory.joinpath('hub-connections.json').write_bytes(packed({self.rid:{'agent':'pi','provider':'test'}}))
        self.state=State(self.directory,'admin-fixture-key')
        self.server=Server(('127.0.0.1',0),Handler);self.server.state=self.state
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)

    def request(self,model='ok',token='agent-hub-pi',**extra):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request('POST','/v1/chat/completions',packed({'model':self.rid+'/'+model,'stream':True,'tools':[{'type':'function','function':{'name':'read'}}],'messages':[{'role':'user','content':'hello'}]}),
                     {'Authorization':'Bearer '+token,'Content-Type':'application/json','X-Session-Id':'native-123',**extra})
        return conn,conn.getresponse()

    def ledger(self):
        path=self.directory/'usage.jsonl'
        for _ in range(100):
            if path.exists() and path.stat().st_size:return json.loads(path.read_text(encoding='utf-8').splitlines()[-1])
            time.sleep(.01)
        self.fail('ledger not written')

    def test_forwards_first_bytes_before_completion_and_preserves_tool_json(self):
        conn,response=self.request('slow')
        first=response.read1(4096)
        self.assertTrue(self.started.is_set());self.assertIn(b'call_1',first)
        self.assertNotIn(b'[DONE]',first)
        self.release.set();rest=response.read();conn.close()
        self.assertTrue(rest.endswith(b'data: [DONE]\n\n'))
        row=self.ledger()
        self.assertEqual((row['response_id'],row['served'],row['stop']),('resp_exact','served','completed'))
        self.assertEqual((row['in'],row['cache_read'],row['out']),(60,40,9))
        self.assertIsNotNone(row['ttft_ms']);self.assertNotIn('first_text_ms',row)
        self.assertEqual(self.calls[0][2]['model'],'slow')
        self.assertEqual(self.calls[0][1]['Authorization'],'Bearer test-credential-value')
        self.assertEqual(row['session'],'native-123')

    def test_early_stream_eof_is_interrupted_not_success(self):
        conn,response=self.request('cut');response.read();conn.close()
        row=self.ledger()
        self.assertEqual((row['status'],row['stop']),(499,'aborted'))
        record=read_requests(self.directory/'usage.jsonl')[0]
        self.assertEqual(record['status'],'interrupted');self.assertIsNone(record['usage']['output'])

    def test_rate_limit_is_one_attempt_and_error_is_scrubbed(self):
        conn,response=self.request('fail');self.assertEqual(response.status,429);response.read();conn.close()
        row=self.ledger();self.assertEqual(len(self.calls),1)
        self.assertEqual(row['tries'],1);self.assertNotIn('test-credential-value',json.dumps(row))
        self.assertEqual(read_requests(self.directory/'usage.jsonl')[0]['status'],'failed')

    def test_rejects_wrong_token_origin_and_unconfigured_model_before_forward(self):
        for model,token,headers in [('ok','wrong',{}),('unknown','agent-hub-pi',{}),('ok','agent-hub-pi',{'Origin':'http://evil.invalid'})]:
            conn,response=self.request(model,token,**headers)
            self.assertIn(response.status,(400,401,403));response.read();conn.close()
        self.assertFalse(self.calls)

    def test_disable_and_reenable_invalidates_inflight_content(self):
        self.state.consent(True)
        conn,response=self.request('slow');response.read1(4096)
        self.state.consent(False,True);self.state.consent(True)
        self.release.set();response.read();conn.close();self.ledger()
        self.assertFalse(list((self.directory/'gateway-conversations').rglob('turns.jsonl')))

    def test_responses_and_anthropic_completion_and_usage(self):
        for protocol,events,expected in [
            ('responses',[{'type':'response.created','response':{'id':'r1','model':'actual'}},{'type':'response.output_text.delta','delta':'hello'},{'type':'response.completed','response':{'id':'r1','usage':{'input_tokens':120,'input_tokens_details':{'cached_tokens':100},'output_tokens':12,'output_tokens_details':{'reasoning_tokens':3}}}}],(20,100,12)),
            ('anthropic',[{'type':'message_start','message':{'id':'m1','usage':{'input_tokens':20,'output_tokens':0,'cache_read_input_tokens':100}}},{'type':'content_block_delta','delta':{'type':'text_delta','text':'hello'}},{'type':'message_delta','usage':{'output_tokens':12}},{'type':'message_stop'}],(20,100,12))]:
            obs=Observation(protocol);obs.sse=True
            for i,e in enumerate(events):obs.feed(b'data: '+packed(e)+b'\n\n',100+i*50)
            obs.finish(500)
            self.assertTrue(obs.complete);self.assertEqual(tuple(obs.fields[x] for x in ('in','cache_read','out')),expected)

    def test_native_login_headers_are_forwarded_only_in_memory_and_sse_without_mime_is_observed(self):
        self.state.codex_base=f'http://127.0.0.1:{self.upstream.server_port}/backend-api/codex'
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request('POST','/backend-api/codex/responses',packed({'model':'ok','stream':True}),
                     {'Authorization':'Bearer native-access-fixture','ChatGPT-Account-Id':'account-fixture','session-id':'native-codex-thread'})
        response=conn.getresponse();self.assertEqual(response.status,200);response.read();conn.close()
        row=self.ledger()
        self.assertEqual((row['response_id'],row['stop'],row['session']),('resp_exact','completed','native-codex-thread'))
        self.assertEqual(self.calls[0][1]['Authorization'],'Bearer native-access-fixture')
        self.assertEqual(self.calls[0][1]['ChatGPT-Account-Id'],'account-fixture')
        stored=''.join(p.read_text(encoding='utf-8') for p in self.directory.rglob('*.jsonl'))
        self.assertNotIn('native-access-fixture',stored);self.assertNotIn('account-fixture',stored)

    def test_python_child_starts_without_downloading_external_binary(self):
        import socket
        with socket.socket() as port:
            port.bind(('127.0.0.1',0));number=port.getsockname()[1]
        runtime=Runtime(self.root/'owned',lambda:{'gateway':{'port':number}})
        try:
            status=runtime.start()
            self.assertEqual(status['engine'],'agent-hub');self.assertTrue(status['installed'])
            self.assertEqual(runtime.binary.suffix,'.py')
            pid=runtime.process.pid
            runtime.configure(True);runtime.configure(False)
            self.assertEqual(pid,runtime.process.pid)
            self.assertFalse(list(self.root.rglob('*.exe')))
        finally:runtime.close()
        self.assertIsNone(runtime.process)

    def test_multiline_sse_data_and_retention_enforce_actual_metadata_and_body_bounds(self):
        import os
        obs=Observation('responses');obs.sse=True
        obs.feed(b'event: response.completed\r\ndata: {"type":"response.completed",\r\ndata: "response":{"id":"r_multi"}}\r\n\r\n',100)
        self.assertTrue(obs.complete);self.assertEqual(obs.fields['response_id'],'r_multi')
        root=self.directory/'gateway-conversations';root.mkdir()
        expired=root/'expired.jsonl';expired.write_bytes(b'old')
        os.utime(expired,(time.time()-8*86400,)*2)
        older=root/'older.jsonl';older.write_bytes(b'a'*40)
        newer=root/'newer.jsonl';newer.write_bytes(b'b'*40)
        os.utime(older,(time.time()-10,)*2)
        ledger=self.directory/'usage.jsonl';ledger.write_bytes(b'keep ledger')
        with patch('gateway_server.BODY_BYTES',60):self.state.prune(force=True)
        self.assertFalse(expired.exists());self.assertFalse(older.exists());self.assertTrue(newer.exists())
        self.assertEqual(ledger.read_bytes(),b'keep ledger')


if __name__=='__main__':unittest.main()
