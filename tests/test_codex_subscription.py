"""Codex subscription sharing using synthetic auth and an isolated HTTP upstream."""
import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from support import isolated, engine, server
from test_proxy_transport import PROXY_ENV
import codex_subscription as codex
from gateway_runtime import export_connections
from gateway_server import Handler, Server, State, packed


class CodexSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated(); self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.codex_home = self.home/'.codex'; self.codex_home.mkdir()
        self.auth_file = self.codex_home/'auth.json'
        self.auth = {'auth_mode':'chatgpt','tokens':{'access_token':'fixture-access-secret',
                    'refresh_token':'fixture-refresh-secret','account_id':'fixture-account'}}
        self.auth_file.write_text(json.dumps(self.auth), encoding='utf-8')
        (self.codex_home/'models_cache.json').write_text(json.dumps({'client_version':'0.164.0'}), encoding='utf-8')
        env = {**PROXY_ENV, 'NO_PROXY':'127.0.0.1,localhost', 'CODEX_HOME':str(self.codex_home)}
        environment = patch.dict(os.environ, env); environment.start(); self.addCleanup(environment.stop)
        self.calls = []
        self.fail = False; self.truncate = False; self.omit_content_type = False;self.fail_event=False
        self.hold=False;self.release=threading.Event()
        owner = self
        class Vendor(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                owner.calls.append((self.path,dict(self.headers),None))
                value={'models':[{'slug':'fixture-model','display_name':'Fixture Model','visibility':'list',
                                  'context_window':123456,'input_modalities':['text','image'],
                                  'supported_reasoning_levels':[{'effort':'low'}]},
                                 {'slug':'hidden-model','visibility':'hide'}]}
                wire=packed(value);self.send_response(200);self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path,dict(self.headers),request))
                if owner.fail:
                    wire=packed({'error':'fixture-access-secret'});self.send_response(401)
                    self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire);return
                self.send_response(200)
                if not owner.omit_content_type:self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                def emit(e):self.wfile.write(b'event: '+e['type'].encode()+b'\ndata: '+packed(e)+b'\n\n');self.wfile.flush()
                emit({'type':'response.created','response':{'id':'resp-fixture','model':'fixture-model','status':'in_progress'}})
                if owner.fail_event:
                    emit({'type':'response.failed','response':{'error':{'code':'usage_limit_reached','message':'fixture-access-secret'}}});return
                if owner.hold:
                    emit({'type':'response.output_text.delta','output_index':0,'delta':'partial'})
                    owner.release.wait(4);return
                if owner.truncate:return
                tools=request.get('tools') or []
                outputs=[x for x in request.get('input',[]) if isinstance(x,dict) and x.get('type')=='function_call_output']
                if tools and not outputs:
                    name=tools[0]['name']; item={'type':'function_call','id':'fc-fixture','call_id':'call-fixture',
                                                 'name':name,'arguments':'{"path":"probe.txt"}','status':'completed'}
                    emit({'type':'response.output_item.added','output_index':0,'item':{**item,'arguments':'','status':'in_progress'}})
                    emit({'type':'response.function_call_arguments.delta','output_index':0,'delta':'{"path":'})
                    emit({'type':'response.function_call_arguments.delta','output_index':0,'delta':'"probe.txt"}'})
                else:
                    item={'type':'message','id':'msg-fixture','role':'assistant','status':'completed',
                          'content':[{'type':'output_text','text':'fixture-result','annotations':[]}]}
                    emit({'type':'response.output_item.added','output_index':0,'item':{**item,'content':[],'status':'in_progress'}})
                    emit({'type':'response.output_text.delta','output_index':0,'delta':'fixture-'})
                    emit({'type':'response.output_text.delta','output_index':0,'delta':'result'})
                emit({'type':'response.output_item.done','output_index':0,'item':item})
                emit({'type':'response.completed','response':{'id':'resp-fixture','model':'fixture-model',
                     'status':'completed','output':[item],'usage':{'input_tokens':10,'output_tokens':3,'total_tokens':13,
                     'input_tokens_details':{'cached_tokens':2},'output_tokens_details':{'reasoning_tokens':0}}}})
        self.vendor=ThreadingHTTPServer(('127.0.0.1',0),Vendor)
        threading.Thread(target=self.vendor.serve_forever,daemon=True).start()
        self.addCleanup(self.vendor.server_close);self.addCleanup(self.vendor.shutdown)
        self.base=f'http://127.0.0.1:{self.vendor.server_port}/backend-api/codex'
        self.directory=self.home/'gateway/config/agent-hub';self.directory.mkdir(parents=True)
        self.http=Server(('127.0.0.1',0),Handler)
        self.http.state=State(self.directory,'fixture-control',codex_base=self.base)
        self.cfg={'gateway':{'enabled':False,'port':self.http.server_port},'providers':{}}
        self.cfg['providers']['codex-chatgpt']=codex.provider_config(self.cfg,[{'id':'fixture-model'}])
        providers,mapping=export_connections(self.cfg)
        (self.directory/'providers.json').write_bytes(packed(providers))
        (self.directory/'hub-connections.json').write_bytes(packed(mapping))
        threading.Thread(target=self.http.serve_forever,daemon=True).start()
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)

    def request(self, agent, protocol, body, token=None):
        path={'chat':'/v1/chat/completions','anthropic':'/v1/messages','responses':'/v1/responses'}[protocol]
        conn=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=8)
        conn.request('POST','/codex-subscription/'+agent+path,packed(body),{
            'Content-Type':'application/json','Authorization':'Bearer '+(token or self.cfg['providers']['codex-chatgpt']['apiKey'])})
        return conn,conn.getresponse()

    def test_status_and_export_do_not_copy_or_expose_codex_credentials(self):
        public=codex.status()
        self.assertTrue(public['signedIn'])
        providers,mapping=export_connections(self.cfg)
        self.assertEqual({row['agent'] for row in mapping.values()},{'claude','codex','pi','dsh','opencode'})
        text=json.dumps(public)+json.dumps(providers)+json.dumps(mapping)
        self.assertNotIn('fixture-access-secret',text);self.assertNotIn('fixture-refresh-secret',text)
        self.assertTrue(all(p['kind']=='codex-chatgpt' for p in providers['providers']))

    def test_refresh_preserves_per_agent_models_and_target_selection(self):
        old=self.cfg['providers']['codex-chatgpt']
        old['targets']=['claude']
        old['agents']['claude']['models']=[{'id':'local-choice'}]
        updated=codex.provider_config(self.cfg,[{'id':'fixture-model'}],old)
        self.assertEqual(updated['targets'],['claude'])
        self.assertEqual(updated['agents']['claude']['models'],[{'id':'local-choice'}])

    def test_catalog_reads_account_models_without_fabricated_limits(self):
        entries=codex.models(codex.read_auth(),backend=self.base)
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['contextWindow'],123456)
        self.assertNotIn('maxTokens',entries[0])
        self.assertEqual(self.calls[0][1]['Authorization'],'Bearer fixture-access-secret')
        self.assertEqual(self.calls[0][1]['ChatGPT-Account-Id'],'fixture-account')
        self.assertIn('client_version=0.164.0',self.calls[0][0])

    def test_connect_and_disconnect_leave_codex_auth_unchanged(self):
        before=self.auth_file.read_bytes()
        runtime=Mock(); obs=Mock();obs.settings.return_value={'content':False}
        with patch.object(codex,'BACKEND',self.base), patch.object(server,'gateway',return_value=runtime), \
             patch.object(server,'observations',return_value=obs):
            result=server.api_codex_subscription_connect()
            self.assertTrue(result['connected']); self.assertEqual(result['models'],1)
            config=json.loads(server.HUB_CONFIG.read_text(encoding='utf-8'))
            self.assertNotIn('fixture-access-secret',json.dumps(config))
            self.assertNotIn('fixture-refresh-secret',json.dumps(config))
            self.assertEqual(self.auth_file.read_bytes(),before)
            self.assertFalse(server.api_codex_subscription_disconnect()['connected'])
        self.assertEqual(self.auth_file.read_bytes(),before)

    def test_account_change_during_catalog_fetch_does_not_connect(self):
        def change(_auth):
            auth=json.loads(self.auth_file.read_text(encoding='utf-8'))
            auth['tokens']['account_id']='other-account'
            self.auth_file.write_text(json.dumps(auth),encoding='utf-8')
            return [{'id':'fixture-model'}]
        with patch.object(codex,'models',side_effect=change),self.assertRaisesRegex(ValueError,'账号已切换'):
            server.api_codex_subscription_connect()
        self.assertNotIn('codex-chatgpt',json.loads(server.HUB_CONFIG.read_text(encoding='utf-8'))['providers'])

    def test_five_client_previews_apply_and_restore_without_modifying_auth(self):
        for global_gateway in (False,True):
            with self.subTest(global_gateway=global_gateway), isolated() as home:
                h=engine.Hub();h.cfg['gateway']={'enabled':global_gateway}
                h.cfg['providers']['codex-chatgpt']=codex.provider_config(h.cfg,[{'id':'fixture-model','contextWindow':128000,'maxTokens':8192}])
                h.cfg['sync']['targets']['codex'].update(enabled=True,mode='third-party')
                h.cfg['sync']['targets']['opencode']={'enabled':True}
                h.cfg['modelSync']={'default':{'provider':'codex-chatgpt','model':'fixture-model'}}
                engine.save_json(engine.HUB_CONFIG,h.cfg)
                (home/'.codex').mkdir(exist_ok=True)
                native_auth=home/'.codex/auth.json';native_auth.write_text('{"synthetic":"original"}',encoding='utf-8')
                (home/'.codex/config.toml').write_text('model = "official-model"\n',encoding='utf-8')
                before=native_auth.read_bytes()
                plan=h.preview_push(['claude','codex','pi','dsh','opencode'])
                self.assertEqual(native_auth.read_bytes(),before)
                result=h.apply_preview(plan['planId'])
                claude=json.loads((home/'.claude/settings.json').read_text(encoding='utf-8'))
                self.assertIn('127.0.0.1:8789',claude['env']['ANTHROPIC_BASE_URL'])
                self.assertIn('codex-chatgpt',(home/'.pi/agent/models.json').read_text(encoding='utf-8'))
                opencode=json.loads((home/'.config/opencode/opencode.json').read_text(encoding='utf-8'))
                self.assertEqual(opencode['model'],'codex-chatgpt/fixture-model')
                self.assertIn('/codex-subscription/opencode/v1',opencode['provider']['codex-chatgpt']['options']['baseURL'])
                self.assertEqual(native_auth.read_bytes(),before)
                h.restore_transaction(result['transactionId'])
                self.assertEqual(native_auth.read_bytes(),before)

    def test_all_five_client_endpoints_receive_real_response_shapes(self):
        for agent, protocol in [('claude','anthropic'),('codex','responses'),('pi','chat'),('dsh','chat'),('opencode','chat')]:
            with self.subTest(agent=agent):
                body={'model':'fixture-model','stream':False}
                body.update({'input':'hello'} if protocol=='responses' else {'messages':[{'role':'user','content':'hello'}]})
                conn,response=self.request(agent,protocol,body);data=json.loads(response.read());conn.close()
                self.assertEqual(response.status,200,data)
                if protocol=='anthropic':self.assertEqual(data['content'][0]['text'],'fixture-result')
                elif protocol=='responses':self.assertEqual(data['output'][0]['content'][0]['text'],'fixture-result')
                else:self.assertEqual(data['choices'][0]['message']['content'],'fixture-result')
        self.assertTrue(all(call[1].get('Authorization')=='Bearer fixture-access-secret' for call in self.calls))
        saved=''.join(p.read_text(encoding='utf-8') for p in self.directory.rglob('*.jsonl'))
        self.assertNotIn('fixture-access-secret',saved);self.assertNotIn('fixture-account',saved)

    def test_anthropic_tool_roundtrip_preserves_tool_identity_and_result(self):
        body={'model':'fixture-model','stream':False,'system':'Keep this exact instruction.',
              'messages':[{'role':'user','content':'Read the file.'}],
              'tools':[{'name':'read_fixture','description':'Read file','input_schema':{'type':'object','properties':{'path':{'type':'string'}}}}]}
        conn,response=self.request('claude','anthropic',body);first=json.loads(response.read());conn.close()
        self.assertEqual(response.status,200,first)
        tool=next(x for x in first['content'] if x['type']=='tool_use')
        self.assertEqual((tool['id'],tool['name'],tool['input']),( 'call-fixture','read_fixture',{'path':'probe.txt'}))
        body['messages'] += [{'role':'assistant','content':first['content']},
                             {'role':'user','content':[{'type':'tool_result','tool_use_id':tool['id'],'content':'random-value'}]}]
        conn,response=self.request('claude','anthropic',body);second=json.loads(response.read());conn.close()
        self.assertEqual(response.status,200,second);self.assertEqual(second['content'][0]['text'],'fixture-result')
        self.assertEqual(self.calls[-1][2]['instructions'],'Keep this exact instruction.')
        self.assertTrue(any(i.get('type')=='function_call_output' and i.get('output')=='random-value' for i in self.calls[-1][2]['input']))

    def test_anthropic_tool_result_image_stays_attached_to_tool_output(self):
        body={'model':'fixture-model','stream':False,'messages':[{'role':'user','content':'inspect'},
              {'role':'assistant','content':[{'type':'tool_use','id':'call-fixture','name':'inspect','input':{}}]},
              {'role':'user','content':[{'type':'tool_result','tool_use_id':'call-fixture','content':[
                  {'type':'text','text':'caption'},{'type':'image','source':{'type':'base64','media_type':'image/png','data':'YWJj'}}]}]}]}
        conn,response=self.request('claude','anthropic',body);data=json.loads(response.read());conn.close()
        self.assertEqual(response.status,200,data)
        output=next(i['output'] for i in self.calls[-1][2]['input'] if i.get('type')=='function_call_output')
        self.assertEqual([x['type'] for x in output],['input_text','input_image'])
        self.assertEqual(output[1]['image_url'],'data:image/png;base64,YWJj')

    def test_missing_sse_content_type_and_responses_string_input(self):
        self.omit_content_type=True
        body={'model':'fixture-model','stream':False,'input':'hello'}
        conn,response=self.request('codex','responses',body);data=json.loads(response.read());conn.close()
        self.assertEqual(response.status,200,data)
        self.assertEqual(self.calls[-1][2]['input'],[{'role':'user','content':[{'type':'input_text','text':'hello'}]}])
        self.assertEqual(data['output'][0]['content'][0]['text'],'fixture-result')

    def test_streamed_chat_tool_call_has_deltas_and_completion(self):
        body={'model':'fixture-model','stream':True,'messages':[{'role':'user','content':'Read.'}],
              'tools':[{'type':'function','function':{'name':'read_fixture','parameters':{'type':'object'}}}]}
        conn,response=self.request('pi','chat',body);wire=response.read();conn.close()
        self.assertEqual(response.status,200);self.assertIn(b'call-fixture',wire)
        self.assertIn(b'probe.txt',wire);self.assertIn(b'tool_calls',wire);self.assertTrue(wire.endswith(b'data: [DONE]\n\n'))

    def test_chat_tool_history_accepts_null_assistant_content(self):
        body={'model':'fixture-model','stream':False,'messages':[{'role':'user','content':'Read.'},
              {'role':'assistant','content':None,'tool_calls':[{'id':'call-fixture','type':'function',
                'function':{'name':'read_fixture','arguments':'{"path":"probe.txt"}'}}]},
              {'role':'tool','tool_call_id':'call-fixture','content':'fixture-value'}],
              'tools':[{'type':'function','function':{'name':'read_fixture','parameters':{'type':'object'}}}]}
        conn,response=self.request('opencode','chat',body);data=json.loads(response.read());conn.close()
        self.assertEqual(response.status,200,data)
        self.assertEqual(data['choices'][0]['message']['content'],'fixture-result')
        self.assertTrue(any(item.get('type')=='function_call_output' and item.get('output')=='fixture-value'
                            for item in self.calls[-1][2]['input']))

    def test_consecutive_and_parallel_tool_rounds_keep_ids_and_results(self):
        call=lambda ident:{'id':ident,'type':'function','function':{'name':'read_fixture','arguments':json.dumps({'path':ident})}}
        body={'model':'fixture-model','messages':[{'role':'user','content':'read files'},
              {'role':'assistant','content':None,'tool_calls':[call('a'),call('b')]},
              {'role':'tool','tool_call_id':'a','content':'first'},
              {'role':'tool','tool_call_id':'b','content':'second'},
              {'role':'assistant','content':None,'tool_calls':[call('c')]},
              {'role':'tool','tool_call_id':'c','content':'third'},
              {'role':'user','content':'summarize'}]}
        conn,response=self.request('pi','chat',body);data=json.loads(response.read());conn.close()
        self.assertEqual(response.status,200,data)
        items=self.calls[-1][2]['input']
        self.assertEqual([i['call_id'] for i in items if i.get('type')=='function_call'],['a','b','c'])
        self.assertEqual([(i['call_id'],i['output']) for i in items if i.get('type')=='function_call_output'],
                         [('a','first'),('b','second'),('c','third')])

    def test_client_cancellation_does_not_retry_and_records_interruption(self):
        self.hold=True
        body=packed({'model':'fixture-model','stream':True,'messages':[{'role':'user','content':'hello'}]})
        headers=('POST /codex-subscription/pi/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                 'Authorization: Bearer '+self.cfg['providers']['codex-chatgpt']['apiKey']+'\r\n'
                 'Content-Type: application/json\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()
        sock=socket.create_connection(('127.0.0.1',self.http.server_port),timeout=5)
        try:
            sock.sendall(headers+body);self.assertIn(b'200',sock.recv(1024))
            sock.shutdown(socket.SHUT_RDWR);sock.close()
            until=time.monotonic()+3
            while self.http.state.active and time.monotonic()<until:time.sleep(.02)
            self.assertEqual(self.http.state.active,0)
            ledger=[json.loads(x) for x in (self.directory/'usage.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(ledger[-1]['status'],499);self.assertEqual(ledger[-1]['stop'],'aborted')
            self.assertEqual(len(self.calls),1)
        finally:
            sock.close();self.release.set()

    def test_streamed_quota_error_is_sanitized_and_recorded_as_429(self):
        self.fail_event=True
        conn,response=self.request('pi','chat',{'model':'fixture-model','messages':[{'role':'user','content':'hello'}]})
        data=response.read().decode();conn.close()
        self.assertEqual(response.status,429);self.assertIn('额度或速率受限',data);self.assertNotIn('fixture-access-secret',data)
        until=time.monotonic()+3
        while self.http.state.active and time.monotonic()<until:time.sleep(.02)
        ledger=[json.loads(x) for x in (self.directory/'usage.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(ledger[-1]['status'],429);self.assertEqual(len(self.calls),1)

    def test_expired_auth_and_upstream_rejection_are_reported_without_secrets(self):
        claims=base64.urlsafe_b64encode(json.dumps({'exp':time.time()-10}).encode()).decode().rstrip('=')
        auth={**self.auth,'tokens':{**self.auth['tokens'],'access_token':'e30.'+claims+'.signature'}}
        self.auth_file.write_text(json.dumps(auth),encoding='utf-8')
        conn,response=self.request('pi','chat',{'model':'fixture-model','messages':[{'role':'user','content':'x'}]})
        data=response.read().decode();conn.close();self.assertEqual(response.status,401)
        self.assertNotIn('signature',data);self.assertEqual(self.calls,[])
        self.auth_file.write_text(json.dumps(self.auth),encoding='utf-8');self.fail=True
        conn,response=self.request('pi','chat',{'model':'fixture-model','messages':[{'role':'user','content':'x'}]})
        data=response.read().decode();conn.close();self.assertEqual(response.status,401)
        self.assertNotIn('fixture-access-secret',data)


if __name__=='__main__':unittest.main()
