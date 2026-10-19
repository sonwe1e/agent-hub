"""WorkBuddy auth and protocol integration against a real, isolated HTTP vendor."""
import concurrent.futures
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from support import isolated, engine, server
import workbuddy
from gateway_runtime import export_connections, route_id
from gateway_server import State, Server, Handler, packed
from gateway_observation import read_requests
from workbuddy_protocol import to_chat, conversation_headers


class WorkBuddyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)/'config/agent-hub';self.directory.mkdir(parents=True)
        self.calls=[];self.refreshes=0;self.entered=threading.Event();self.release=threading.Event()
        self.addCleanup(self.release.set)
        owner=self
        class Vendor(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,value,status=200):
                wire=packed(value);self.send_response(status);self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
            def do_GET(self):
                if self.path.startswith('/v2/plugin/auth/token?'):
                    if getattr(owner,'authReady',True) is False:return self.reply({'code':11217,'msg':'pending'})
                    return self.reply({'code':0,'data':{'accessToken':'new-access-secret','refreshToken':'new-refresh-secret','expiresIn':3600}})
                if self.path.startswith('/v2/plugin/login/account?'):
                    return self.reply({'code':0,'data':{'uid':'fixture-user','nickname':'Fixture User','enterpriseId':'fixture-team'}})
                if self.path=='/v3/config':
                    owner.catalog_headers=dict(self.headers)
                    return self.reply({'code':0,'data':{'agents':[{'name':'ide','models':['not-cli']},{'name':'cli','models':['ok','tools','fail','cut','slow','quiet']}],
                        'models':[{'id':'ok','name':'Fixture Model','maxInputTokens':128000,'maxOutputTokens':8192,'supportsImages':True,'credits':'x0.29'}]}})
                self.reply({'code':404},404)
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
                if self.path.startswith('/v2/plugin/auth/state'):
                    return self.reply({'code':0,'data':{'state':'fixture-state','authUrl':'https://www.workbuddy.ai/login?state=fixture-state'}})
                if self.path=='/v2/plugin/auth/token/refresh':
                    owner.refreshes+=1;time.sleep(.05)
                    return self.reply({'code':0,'data':{'accessToken':'rotated-access-secret','refreshToken':'rotated-refresh-secret','expiresIn':3600}})
                owner.calls.append((self.path,dict(self.headers),value))
                if value['model']=='fail': return self.reply({'code':11140,'msg':'restricted access-secret refresh-secret'},403)
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                def emit(v):self.wfile.write(b'data: '+packed(v)+b'\n\n');self.wfile.flush()
                if value['model']=='slow':owner.entered.set();owner.release.wait(5)
                try:
                    if value.get('tools') and not any(m.get('role')=='tool' for m in value['messages']):
                        name=value['tools'][0]['function']['name']
                        custom='input' in value['tools'][0]['function']['parameters'].get('properties',{})
                        emit({'id':'upstream-id','model':'served-fixture','choices':[{'delta':{'tool_calls':[{'index':0,'id':'call_exact','function':{'name':name,'arguments':'{"input":' if custom else '{"path":'}}]}}]})
                        emit({'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':json.dumps('*** Begin Patch\n*** End Patch')+'}' if custom else '"file.txt"}'}}]}}]})
                        finish='tool_calls'
                    else:
                        emit({'id':'upstream-id','model':'served-fixture','choices':[{'delta':{'reasoning_content':'thought'}}]})
                        emit({'choices':[{'delta':{'content':'你好 '}}]});owner.entered.set()
                        if getattr(owner,'pauseAfterText',False):owner.release.wait(3)
                        if value['model']=='cut':return
                        emit({'choices':[{'delta':{'content':'world'}}]});finish='stop'
                    end={'choices':[{'delta':{},'finish_reason':finish}]}
                    if value['model']!='quiet':end['usage']={'prompt_tokens':100,'completion_tokens':12,'total_tokens':112,'prompt_tokens_details':{'cached_tokens':30},'completion_tokens_details':{'reasoning_tokens':4}}
                    emit(end);self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
                except OSError:pass
        self.vendor=ThreadingHTTPServer(('127.0.0.1',0),Vendor)
        threading.Thread(target=self.vendor.serve_forever,daemon=True).start()
        self.addCleanup(self.vendor.server_close);self.addCleanup(self.vendor.shutdown)
        endpoint=patch.object(workbuddy,'ENDPOINT',f'http://127.0.0.1:{self.vendor.server_port}')
        endpoint.start();self.addCleanup(endpoint.stop)
        self.accounts=workbuddy.Accounts(self.directory)
        with self.accounts.locked():self.accounts.save({'access':'access-secret','refresh':'refresh-secret','expires':time.time()+3600,'uid':'fixture-user','account':'Fixture User','enterpriseId':'fixture-team'})
        self.state=State(self.directory,'fixture-control-secret')
        self.http=Server(('127.0.0.1',0),Handler);self.http.state=self.state
        self.cfg={'gateway':{'enabled':False,'port':self.http.server_port},'providers':{}}
        self.cfg['providers']['workbuddy-ai']=workbuddy.provider_config(self.cfg,[{'id':m} for m in ('ok','tools','fail','cut','slow','quiet')])
        providers,mapping=export_connections(self.cfg)
        (self.directory/'providers.json').write_bytes(packed(providers));(self.directory/'hub-connections.json').write_bytes(packed(mapping))
        threading.Thread(target=self.http.serve_forever,daemon=True).start()
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)

    def request(self,protocol='chat',model='ok',stream=True,agent=None,headers=None,**extra):
        agent=agent or {'chat':'pi','anthropic':'claude','responses':'codex'}[protocol]
        suffix={'chat':'/v1/chat/completions','anthropic':'/v1/messages','responses':'/v1/responses'}[protocol]
        body={'model':model,'stream':stream}
        if protocol=='responses':body['input']='hello'
        else:body['messages']=[{'role':'user','content':'hello'}]
        body.update(extra)
        conn=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=6)
        conn.request('POST','/workbuddy/'+agent+suffix,packed(body),{'Content-Type':'application/json','Authorization':'Bearer '+self.cfg['providers']['workbuddy-ai']['apiKey'],'X-Session-Id':'session-fixture',**(headers or {})})
        return conn,conn.getresponse()

    def ledger(self, count=1):
        file=self.directory/'usage.jsonl'
        for _ in range(100):
            rows=file.read_text(encoding='utf-8').splitlines() if file.exists() else []
            if len(rows)>=count:return json.loads(rows[-1])
            time.sleep(.01)
        self.fail('gateway did not save observation')

    def test_browser_login_saves_private_credentials_and_team_identity(self):
        login=workbuddy.Login(self.accounts)
        try:
            public=login.start()
            for _ in range(100):
                if login.status()['status']!='pending':break
                time.sleep(.01)
            self.assertEqual(login.status()['status'],'succeeded')
            auth=self.accounts.read()
            self.assertEqual((auth['uid'],auth['enterpriseId']),('fixture-user','fixture-team'))
            self.assertEqual(auth['access'],'new-access-secret')
            self.assertNotIn('secret',json.dumps(public)+json.dumps(login.status())+json.dumps(self.accounts.status()))
            self.assertGreater(auth['expires'],time.time()+3500)
        finally:login.cancel()

    def test_refresh_is_serialized_and_persisted_across_concurrent_callers(self):
        auth=self.accounts.read();auth['expires']=time.time()+5
        with self.accounts.locked():self.accounts.save(auth)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:workbuddy.Accounts(self.directory).fresh(),range(6)))
        self.assertEqual(self.refreshes,1)
        self.assertTrue(all(a['access']=='rotated-access-secret' for a in results))
        self.assertEqual(self.accounts.read()['refresh'],'rotated-refresh-secret')

    def test_failed_refresh_keeps_valid_access_but_expired_login_requires_signin(self):
        auth=self.accounts.read();auth['expires']=time.time()+10
        with self.accounts.locked():self.accounts.save(auth)
        with patch.object(workbuddy,'call',side_effect=workbuddy.WorkBuddyError('failed')):
            self.assertEqual(self.accounts.fresh()['access'],'access-secret')
            auth['expires']=time.time()-1
            with self.accounts.locked():self.accounts.save(auth)
            with self.assertRaisesRegex(ValueError,'刷新失败'):self.accounts.fresh()
        self.assertEqual(self.accounts.read()['refresh'],'refresh-secret')

    def test_dynamic_catalog_uses_cli_and_enterprise_without_inventing_usd_prices(self):
        models=self.accounts.models()
        self.assertEqual(models[0]['contextWindow'],128000)
        self.assertEqual(models[0]['input'],['text','image'])
        self.assertNotIn('not-cli',[m['id'] for m in models]);self.assertNotIn('cost',models[0])
        self.assertEqual(self.catalog_headers['X-Enterprise-Id'],'fixture-team')
        self.assertEqual(self.catalog_headers['User-Agent'],'CLI/2.0.0 WorkBuddy/5.5.6')

    def test_all_protocols_stream_text_reasoning_and_real_usage(self):
        for i,protocol in enumerate(('chat','anthropic','responses'),1):
            with self.subTest(protocol=protocol):
                conn,res=self.request(protocol);wire=res.read();conn.close()
                self.assertEqual(res.status,200);self.assertIn('你好'.encode(),wire);self.assertIn(b'world',wire)
                self.assertIn({'chat':b'[DONE]','anthropic':b'message_stop','responses':b'response.completed'}[protocol],wire)
                row=self.ledger(i)
                self.assertEqual((row['in'],row['cache_read'],row['out']),(70,30,12))
                self.assertEqual(row['served'],'served-fixture');self.assertEqual(row['stop'],'completed')
                upstream=self.calls[-1]
                self.assertEqual(upstream[0],'/v2/chat/completions');self.assertTrue(upstream[2]['stream'])
                self.assertEqual(upstream[1]['X-Agent-Type'],'main')
                self.assertEqual(upstream[1]['X-Tenant-Id'],'fixture-team')

    def test_nonstream_responses_and_anthropic_are_valid_complete_objects(self):
        for protocol in ('chat','anthropic','responses'):
            with self.subTest(protocol=protocol):
                conn,res=self.request(protocol,stream=False);value=json.loads(res.read());conn.close()
                self.assertEqual(res.status,200)
                if protocol=='chat':self.assertEqual(value['choices'][0]['message']['content'],'你好 world')
                elif protocol=='anthropic':self.assertEqual(value['content'][1]['text'],'你好 world')
                else:self.assertEqual(value['output'][1]['content'][0]['text'],'你好 world')

    def test_stream_delivers_events_while_upstream_is_still_generating(self):
        self.pauseAfterText=True
        try:
            conn,res=self.request('responses')
            self.assertTrue(self.entered.wait(1))
            first=res.read1(4096)
            self.assertIn(b'response.created',first);self.assertNotIn(b'response.completed',first)
            self.release.set();rest=res.read();conn.close()
            self.assertIn(b'response.completed',rest)
        finally:self.release.set()

    def test_tool_calls_round_trip_with_safe_names_and_fragmented_json(self):
        anthropic_tool={'name':'mcp.read/file','description':'read','input_schema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}
        conn,res=self.request('anthropic',model='tools',stream=False,tools=[anthropic_tool]);value=json.loads(res.read());conn.close()
        call=value['content'][0]
        self.assertEqual(call,{'type':'tool_use','id':'call_exact','name':'mcp.read/file','input':{'path':'file.txt'}})
        self.assertEqual(value['stop_reason'],'tool_use')
        first_headers=self.calls[-1][1]
        messages=[{'role':'user','content':'hello'},{'role':'assistant','content':value['content']},
                  {'role':'user','content':[{'type':'tool_result','tool_use_id':'call_exact','content':'file contents'}]}]
        conn,res=self.request('anthropic',model='tools',stream=False,tools=[anthropic_tool],messages=messages)
        self.assertEqual(res.status,200);res.read();conn.close()
        sent=self.calls[-1][2]['messages']
        self.assertEqual(sent[-1],{'role':'tool','tool_call_id':'call_exact','content':'file contents'})
        self.assertEqual(first_headers['X-Conversation-Request-ID'],self.calls[-1][1]['X-Conversation-Request-ID'])
        self.assertNotEqual(first_headers['X-Request-ID'],self.calls[-1][1]['X-Request-ID'])

    def test_responses_tool_continuation_replays_history_and_is_session_scoped(self):
        tools=[{'type':'function','name':'read','parameters':{'type':'object'}}]
        conn,res=self.request('responses',model='tools',stream=False,tools=tools);value=json.loads(res.read());conn.close()
        self.assertEqual(value['output'][0]['type'],'function_call')
        conn,res=self.request('responses',model='tools',stream=False,previous_response_id=value['id'],tools=tools,
            input=[{'type':'function_call_output','call_id':'call_exact','output':'file contents'}])
        self.assertEqual(res.status,200);res.read();conn.close()
        self.assertTrue(any(m.get('tool_calls') for m in self.calls[-1][2]['messages']))
        self.assertEqual(self.calls[-1][2]['messages'][-1]['role'],'tool')
        conn,res=self.request('responses',stream=False,previous_response_id=value['id'],headers={'X-Session-Id':'another-session'})
        self.assertEqual(res.status,400);res.read();conn.close()

    def test_namespaced_route_also_works_when_other_connections_use_gateway(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=5)
        conn.request('POST','/v1/messages',packed({'model':route_id('claude','workbuddy-ai')+'/ok','messages':[{'role':'user','content':'hi'}]}),{'x-api-key':'agent-hub-claude'})
        res=conn.getresponse();self.assertEqual(res.status,200);self.assertIn('你好'.encode(),res.read());conn.close()

    def test_codex_custom_text_tools_stream_and_replay_as_custom_calls(self):
        tools=[{'type':'custom','name':'apply_patch','description':'Apply a patch','format':{'type':'text'}}]
        conn,res=self.request('responses',model='tools',tools=tools);wire=res.read();conn.close()
        self.assertIn(b'response.custom_tool_call_input.delta',wire);self.assertIn(b'response.custom_tool_call_input.done',wire)
        events=[json.loads(line[6:]) for line in wire.decode().splitlines() if line.startswith('data: ')]
        output=next(e['response'] for e in events if e['type']=='response.completed')
        call=output['output'][0]
        self.assertEqual(call['type'],'custom_tool_call');self.assertEqual(call['input'],'*** Begin Patch\n*** End Patch')
        conn,res=self.request('responses',model='tools',tools=tools,stream=False,input=[
            {'role':'user','content':'hello'},call,{'type':'custom_tool_call_output','call_id':'call_exact','output':'Done'}])
        self.assertEqual(res.status,200);res.read();conn.close()
        self.assertEqual(json.loads(self.calls[-1][2]['messages'][-2]['tool_calls'][0]['function']['arguments'])['input'],call['input'])

    def test_responses_namespace_identity_survives_function_and_custom_round_trips(self):
        for kind in ('function','custom'):
            with self.subTest(kind=kind):
                tool={'type':kind,'name':'read'}
                if kind=='function':tool['parameters']={'type':'object'}
                tools=[{'type':'namespace','name':'functions','description':'Local tools','tools':[tool]}]
                conn,res=self.request('responses',model='tools',stream=False,tools=tools)
                value=json.loads(res.read());conn.close();self.assertEqual(res.status,200)
                call=value['output'][0]
                self.assertEqual((call['namespace'],call['name']),('functions','read'))
                conn,res=self.request('responses',model='tools',stream=False,tools=tools,input=[
                    {'role':'user','content':'hello'},call,{'type':kind+'_call_output' if kind=='function' else 'custom_tool_call_output','call_id':'call_exact','output':'result'}])
                self.assertEqual(res.status,200);res.read();conn.close()
                self.assertEqual(self.calls[-1][2]['messages'][-2]['tool_calls'][0]['function']['name'],'functions_read')

    def test_vendor_denial_preserves_status_scrubs_tokens_and_does_not_retry(self):
        conn,res=self.request(model='fail');wire=res.read();conn.close()
        self.assertEqual(res.status,403);self.assertIn(b'11140',wire)
        row=self.ledger();self.assertEqual(len(self.calls),1);self.assertEqual(row['tries'],1)
        self.assertNotIn('access-secret',wire.decode()+json.dumps(row));self.assertNotIn('refresh-secret',json.dumps(row))
        self.assertTrue(self.accounts.status()['signedIn'])

    def test_incomplete_stream_has_error_and_no_success_marker(self):
        conn,res=self.request('responses',model='cut');wire=res.read();conn.close()
        self.assertIn(b'response.failed',wire);self.assertNotIn(b'response.completed',wire)
        row=self.ledger();self.assertEqual(row['status'],502)

    def test_missing_usage_stays_unknown_and_body_not_retained_by_default(self):
        conn,res=self.request(model='quiet');res.read();conn.close();self.ledger()
        row=read_requests(self.directory/'usage.jsonl')[0]
        self.assertIsNone(row['usage']['input']);self.assertIsNone(row['usage']['output']);self.assertIsNone(row['cost'])
        self.assertFalse((self.directory/'gateway-conversations').exists())

    def test_origin_bad_token_and_invalid_input_do_not_reach_vendor(self):
        for headers,extra in [({'Origin':'https://evil.invalid'},{}),({'Authorization':'Bearer wrong'},{}),({}, {'messages':[{'content':[]}]}),({}, {'n':2})]:
            conn,res=self.request(headers=headers,**extra)
            self.assertIn(res.status,(400,401,403));res.read();conn.close()
        self.assertEqual(len(self.calls),0)

    def test_disconnect_before_first_byte_cancels_upstream_without_waiting_for_model(self):
        sock=socket.create_connection(('127.0.0.1',self.http.server_port),timeout=3)
        value=packed({'model':'slow','stream':True,'messages':[{'role':'user','content':'hello'}]})
        key=self.cfg['providers']['workbuddy-ai']['apiKey']
        sock.sendall((f'POST /workbuddy/pi/v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {key}\r\nContent-Type: application/json\r\nContent-Length: {len(value)}\r\n\r\n').encode()+value)
        self.assertTrue(self.entered.wait(2));sock.close()
        row=self.ledger();self.assertEqual((row['status'],row['stop']),(499,'aborted'))
        self.assertLess(row['ms'],2000);self.release.set()

    def test_account_api_connect_refresh_logout_preserves_native_configs(self):
        with isolated() as home:
            login=server.workbuddy_login()
            with login.accounts.locked():login.accounts.save(self.accounts.read())
            runtime=server.gateway()
            with patch.object(runtime,'start',return_value={'state':'running'}),patch.object(runtime,'configure'):
                result=server.api_workbuddy_connect()
                self.assertTrue(result['connected']);self.assertEqual(result['models'],6)
                cfg=json.loads(engine.HUB_CONFIG.read_text(encoding='utf-8'))
                self.assertEqual(cfg['providers']['workbuddy-ai']['kind'],'workbuddy-ai')
                self.assertNotIn('access-secret',json.dumps(cfg));self.assertNotIn('refresh-secret',json.dumps(result))
                for agent in ('claude','codex','pi','dsh'):
                    self.assertTrue(engine.agent_connection_ready(cfg['providers']['workbuddy-ai'],agent))
                self.assertFalse((home/'.codex/config.toml').exists());self.assertFalse((home/'.claude/settings.json').exists())
                result=server.api_workbuddy_logout()
                self.assertFalse(result['signedIn']);self.assertFalse(result['connected']);self.assertFalse(login.accounts.path.exists())
                self.assertEqual(len(server.hub().providers['workbuddy-ai']['models']),6)

    def test_connection_export_never_contains_oauth_and_all_agents_remain_selectable(self):
        providers,mapping=export_connections(self.cfg)
        self.assertEqual(len(providers['providers']),4)
        self.assertEqual({r['agent'] for r in mapping.values()},{'claude','codex','pi','dsh'})
        self.assertTrue(all(r['kind']=='workbuddy-ai' for r in providers['providers']))
        self.assertNotIn('access-secret',json.dumps(providers))

    def test_logout_during_catalog_fetch_cannot_reenable_the_old_account(self):
        with isolated():
            login=server.workbuddy_login()
            with login.accounts.locked():login.accounts.save(self.accounts.read())
            def logout_during_fetch():
                with login.accounts.locked():login.accounts.path.unlink()
                return [{'id':'ok'}]
            with patch.object(login.accounts,'models',side_effect=logout_during_fetch):
                with self.assertRaisesRegex(ValueError,'账号已退出或切换'):server.api_workbuddy_connect()
            self.assertNotIn('workbuddy-ai',server.hub().providers)

    def test_actual_client_previews_use_workbuddy_and_keep_oauth_out_of_files(self):
        for global_gateway in (False,True):
            with self.subTest(global_gateway=global_gateway),isolated() as home:
                h=engine.Hub()
                h.cfg['gateway']={'enabled':global_gateway}
                h.cfg['providers']['workbuddy-ai']=workbuddy.provider_config(h.cfg,[{'id':'ok','name':'Fixture','contextWindow':128000,'maxTokens':8192}])
                h.cfg['sync']['targets']['codex'].update(enabled=True,mode='third-party')
                h.cfg['modelSync']={'default':{'provider':'workbuddy-ai','model':'ok'}}
                engine.save_json(engine.HUB_CONFIG,h.cfg)
                (home/'.codex').mkdir(exist_ok=True)
                (home/'.codex/config.toml').write_text('model = "official-model"\n',encoding='utf-8')
                plan=h.preview_push(['claude','codex','pi','dsh']);h.apply_preview(plan['planId'])
                claude=json.loads((home/'.claude/settings.json').read_text(encoding='utf-8'))
                self.assertIn('127.0.0.1:8789',claude['env']['ANTHROPIC_BASE_URL'])
                self.assertEqual(claude['env']['ANTHROPIC_MODEL'],route_id('claude','workbuddy-ai')+'/ok' if global_gateway else 'ok')
                codex=(home/'.codex/config.toml').read_text(encoding='utf-8')
                self.assertIn('responses',codex);self.assertIn('127.0.0.1:8789',codex)
                pi=(home/'.pi/agent/models.json').read_text(encoding='utf-8');self.assertIn('workbuddy-ai',pi)
                from sync_transaction import TARGET_FILES
                for agent in ('claude','codex','pi','dsh'):
                    for rel in TARGET_FILES[agent]:
                        p=home/rel
                        if p.is_file():self.assertNotIn('access-secret',p.read_text(encoding='utf-8'))
                self.assertFalse((home/'.codex/auth.json').exists())


class ConversionTests(unittest.TestCase):
    def test_client_metadata_is_normalized_on_a_copy_and_tool_results_stay_exact(self):
        billing='x-anthropic-billing-header: cc_version=1.0; secretless_test=1;\n'
        body={'model':'m','messages':[{'role':'system','content':billing},
            {'role':'system','content':"You are Claude Code, Anthropic's official CLI for Claude"},
            {'role':'user','content':'Read the requested file.'},
            {'role':'tool','tool_call_id':'c','content':billing}]}
        original=json.dumps(body)
        chat,_=to_chat(body,'chat')
        self.assertEqual(len(chat['messages']),3)
        self.assertEqual(chat['messages'][0]['content'],"You are Claude Code, Anthropic's CLI for Claude")
        self.assertEqual(chat['messages'][-1]['content'],billing)
        self.assertEqual(json.dumps(body),original)

    def test_tool_result_images_are_kept_and_do_not_create_an_extra_user_turn(self):
        tool={'name':'view','input_schema':{'type':'object'}}
        messages=[{'role':'user','content':'see image'},{'role':'assistant','content':[{'type':'tool_use','id':'c','name':'view','input':{}}]}]
        first,_=to_chat({'model':'m','messages':messages,'tools':[tool]},'anthropic')
        messages.append({'role':'user','content':[{'type':'tool_result','tool_use_id':'c','content':[{'type':'text','text':'image result'},{'type':'image','source':{'type':'base64','media_type':'image/png','data':'AAAA'}}]}]})
        next_chat,_=to_chat({'model':'m','messages':messages,'tools':[tool]},'anthropic')
        self.assertEqual(next_chat['messages'][-2]['content'],'image result')
        self.assertEqual(next_chat['messages'][-1]['content'][-1]['image_url']['url'],'data:image/png;base64,AAAA')
        self.assertEqual(conversation_headers(first,'s','salt')['X-Conversation-Request-ID'],conversation_headers(next_chat,'s','salt')['X-Conversation-Request-ID'])

    def test_responses_messages_images_tools_and_developer_are_preserved(self):
        body={'model':'m','instructions':'system','max_output_tokens':200,'reasoning':{'effort':'high'},
              'input':[{'role':'developer','content':[{'type':'input_text','text':'policy'}]},
                       {'role':'user','content':[{'type':'input_image','image_url':'data:image/png;base64,AAAA'}]}],
              'tools':[{'type':'function','name':'mcp.read','parameters':{'anyOf':[{'properties':{'path':{'enum':['a','b']}}}]}}],
              'tool_choice':{'type':'function','name':'mcp.read'}}
        chat,names=to_chat(body,'responses')
        self.assertEqual(chat['messages'][1]['role'],'system');self.assertEqual(chat['messages'][2]['content'][0]['type'],'image_url')
        self.assertEqual(chat['max_tokens'],200);self.assertEqual(chat['reasoning_effort'],'high')
        self.assertEqual(chat['tool_choice'],'mcp_read');self.assertEqual(names['mcp_read'],'mcp.read')
        self.assertEqual(chat['tools'][0]['function']['parameters']['type'],'object')
        self.assertNotIn('stream',body)

    def test_conversation_ids_stable_across_tool_steps_change_for_next_user(self):
        chat={'messages':[{'role':'user','content':'hello'}]}
        first=conversation_headers(chat,'session','salt')
        chat['messages'] += [{'role':'assistant','tool_calls':[{}]},{'role':'tool','content':'result'}]
        same=conversation_headers(chat,'session','salt')
        chat['messages'].append({'role':'user','content':'next'})
        other=conversation_headers(chat,'session','salt')
        self.assertEqual(first['X-Conversation-ID'],other['X-Conversation-ID'])
        self.assertEqual(first['X-Conversation-Request-ID'],same['X-Conversation-Request-ID'])
        self.assertNotEqual(first['X-Conversation-Request-ID'],other['X-Conversation-Request-ID'])


if __name__=='__main__':unittest.main()
