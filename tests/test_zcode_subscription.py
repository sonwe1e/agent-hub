"""ZCode subscription tests with local synthetic encrypted credentials and vendor."""
import base64
import hashlib
import http.client
import json
import os
import socket
import time
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch, Mock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from support import isolated, engine, server
from gateway_server import Handler, Server, State, packed
from gateway_runtime import export_connections
from test_proxy_transport import PROXY_ENV
import zcode_subscription as zcode


class ZCodeSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.ctx=isolated(); self.home=self.ctx.__enter__(); self.addCleanup(self.ctx.__exit__,None,None,None)
        env=patch.dict(os.environ,{**PROXY_ENV,'NO_PROXY':'127.0.0.1,localhost','ZCODE_CREDENTIAL_SECRET':'fixture-seed'})
        env.start();self.addCleanup(env.stop)
        self.root=self.home/'.zcode/v2';self.root.mkdir(parents=True)
        self.key='fixture-key.fixture-secret'
        def enc(value):
            iv=b'123456789012'; key=hashlib.sha256(b'fixture-seed').digest()
            raw=AESGCM(key).encrypt(iv,value.encode(),None)
            b64=lambda x:base64.urlsafe_b64encode(x).decode().rstrip('=')
            return 'enc:v1:'+b64(iv)+'.'+b64(raw[-16:])+'.'+b64(raw[:-16])
        self.enc=enc
        self.creds={'oauth:bigmodel:user_info':enc(json.dumps({'id':'fixture-id'})),
                    'account-provider:coding-plan:account:bigmodel-individual-coding-plan:account:fixture-id:api-key':enc(self.key)}
        self.auth_file=self.root/'credentials.json';self.write_creds()
        self.settings={'providerFamilyDomain':'bigmodel','providerFamilyConnectionSelections':{'bigmodel':{'kind':'individual-coding-plan'}}}
        self.write_settings()
        runtime=self.root/'runtime/provider/windows/1.0/endpoint';runtime.mkdir(parents=True)
        rules={'revision':1,'config':{'modelConfigRules':{'builtinProviderModelRules':[
          {'providerId':'account:bigmodel-individual-coding-plan','modelId':'fixture-model','config':{'enabled':True}}],
          'modelRules':[{'modelMatch':'fixture-.*','config':{'properties':{'contextWindow':12345,
          'inputFormat':{'supportsImage':False}},'optionSpecs':{'reasoningLevel':{'values':['enabled']}}}}]}}}
        (runtime/'zcode-builtin.json').write_text(json.dumps(rules),encoding='utf-8')
        self.calls=[];self.get_calls=[];self.truncate=False;self.fail=False;self.quota_json=False;self.fail_event=False;self.hold=False;self.release=threading.Event();self.team_keys=[{'name':'zcode-team-api-key','keyType':2,'apiKey':'team-key'}];owner=self
        zcode._KEY_CACHE.clear()
        class Vendor(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.get_calls.append((self.path,dict(self.headers)))
                value={'secretKey':'team-secret'} if '/copy/' in self.path else owner.team_keys
                wire=packed({'code':200,'data':value});self.send_response(200)
                self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path,dict(self.headers),body))
                if owner.quota_json:
                    wire=packed({'code':1005,'msg':'fixture-key.fixture-secret'});self.send_response(200)
                    self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire);return
                if owner.fail:
                    wire=packed({'error':'fixture-key.fixture-secret'});self.send_response(401)
                    self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire);return
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                def emit(event):self.wfile.write(b'event: '+event['type'].encode()+b'\ndata: '+packed(event)+b'\n\n');self.wfile.flush()
                emit({'type':'message_start','message':{'id':'msg_fixture','type':'message','role':'assistant',
                      'model':'fixture-model','content':[],'stop_reason':None,'usage':{'input_tokens':10,'output_tokens':0}}})
                if owner.fail_event:
                    emit({'type':'error','error':{'type':'rate_limit_error','message':'fixture-key.fixture-secret'}});return
                if owner.hold:
                    emit({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})
                    emit({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'partial'}})
                    owner.release.wait(4);return
                if owner.truncate:return
                results=[part for msg in body['messages'] for part in (msg['content'] if isinstance(msg['content'],list) else [])
                         if isinstance(part,dict) and part.get('type')=='tool_result']
                if body.get('tools') and not results:
                    emit({'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'call_fixture',
                          'name':body['tools'][0]['name'],'input':{}}})
                    emit({'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':'{"path":"probe.txt"}'}})
                    reason='tool_use'
                else:
                    emit({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})
                    emit({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'fixture-result'}})
                    reason='end_turn'
                emit({'type':'content_block_stop','index':0})
                emit({'type':'message_delta','delta':{'stop_reason':reason,'stop_sequence':None},'usage':{'output_tokens':3}})
                emit({'type':'message_stop'})
        self.vendor=ThreadingHTTPServer(('127.0.0.1',0),Vendor);threading.Thread(target=self.vendor.serve_forever,daemon=True).start()
        self.addCleanup(self.vendor.server_close);self.addCleanup(self.vendor.shutdown)
        base=patch.dict(zcode.BASES,{'bigmodel':f'http://127.0.0.1:{self.vendor.server_port}/anthropic'})
        base.start();self.addCleanup(base.stop)
        business=patch.dict(zcode.BUSINESS,{'bigmodel':f'http://127.0.0.1:{self.vendor.server_port}'})
        business.start();self.addCleanup(business.stop)
        self.data=self.home/'gateway';self.data.mkdir()
        self.http=Server(('127.0.0.1',0),Handler)
        self.cfg={'gateway':{'enabled':False,'port':self.http.server_port},'providers':{}}
        self.cfg['providers']['zcode-subscription']=zcode.provider_config(self.cfg,[{'id':'fixture-model'}])
        providers,mapping=export_connections(self.cfg)
        (self.data/'providers.json').write_bytes(packed(providers));(self.data/'hub-connections.json').write_bytes(packed(mapping))
        self.http.state=State(self.data,'fixture-control');threading.Thread(target=self.http.serve_forever,daemon=True).start()
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)

    def write_creds(self):self.auth_file.write_text(json.dumps(self.creds),encoding='utf-8')
    def write_settings(self):(self.root/'setting.json').write_text(json.dumps(self.settings),encoding='utf-8')
    def request(self,agent,protocol,body):
        path={'chat':'/v1/chat/completions','anthropic':'/v1/messages','responses':'/v1/responses'}[protocol]
        c=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=8)
        c.request('POST','/zcode-subscription/'+agent+path,packed(body),{'Content-Type':'application/json',
                  'Authorization':'Bearer '+self.cfg['providers']['zcode-subscription']['apiKey']})
        return c,c.getresponse()

    def test_auth_and_models_are_read_only_and_key_never_exported(self):
        before=self.auth_file.read_bytes();auth=zcode.read_auth();self.assertEqual(auth['key'],self.key)
        self.assertEqual(zcode.models(auth),[{'id':'fixture-model','name':'fixture-model','contextWindow':12345,'input':['text'],'reasoning':True}])
        providers,mapping=export_connections(self.cfg)
        self.assertEqual({x['agent'] for x in mapping.values()},{'claude','codex','pi','dsh','opencode'})
        self.assertNotIn(self.key,json.dumps(providers));self.assertEqual(self.auth_file.read_bytes(),before)
        self.assertNotIn(self.key,json.dumps(zcode.status()))

    def test_rejects_wrong_account_and_incomplete_team_selection(self):
        self.creds['oauth:bigmodel:user_info']=self.enc(json.dumps({'id':'other-id'}));self.write_creds()
        with self.assertRaisesRegex(ValueError,'没有可读取的密钥'):zcode.read_auth()
        self.settings['providerFamilyConnectionSelections']['bigmodel']['kind']='team-coding-plan';self.write_settings()
        with self.assertRaisesRegex(ValueError,'团队订阅缺少'):zcode.read_auth()

    def test_legacy_team_selection_never_falls_back_to_personal_key(self):
        self.creds['oauth:bigmodel:access_token']=self.enc('fixture-team-token');self.write_creds()
        self.settings.pop('providerFamilyConnectionSelections')
        self.settings['modelProviderFamilySelectedKeys']={'bigmodel':'team-plan:builtin:bigmodel-coding-plan:x:org%3A1:project-1'}
        self.write_settings();auth=zcode.read_auth()
        self.assertEqual((auth['kind'],auth['org'],auth['project']),('team-coding-plan','org:1','project-1'))
        self.assertEqual(zcode.resolve_auth(auth)['key'],'team-key.team-secret')
        self.settings['modelProviderFamilySelectedKeys']['bigmodel']='team-plan:builtin:bigmodel-coding-plan:bad'
        self.write_settings()
        with self.assertRaisesRegex(ValueError,'团队选择格式无效'):zcode.read_auth()

    def test_team_reads_existing_project_key_and_never_creates_one(self):
        self.creds['oauth:bigmodel:access_token']=self.enc('fixture-team-token');self.write_creds()
        self.settings['providerFamilyConnectionSelections']['bigmodel']={'kind':'team-coding-plan','organizationId':'org-1','projectId':'project-1'}
        self.write_settings();before=self.auth_file.read_bytes()
        auth=zcode.read_auth();resolved=zcode.resolve_auth(auth)
        self.assertEqual(resolved['key'],'team-key.team-secret');self.assertEqual(len(self.get_calls),2)
        self.assertTrue(all(x[1]['Bigmodel-Organization']=='org-1' for x in self.get_calls))
        self.assertEqual(self.auth_file.read_bytes(),before)
        for agent,protocol in [('claude','anthropic'),('codex','responses'),('pi','chat'),('dsh','chat'),('opencode','chat')]:
            with self.subTest(agent=agent):
                body={'model':'fixture-model','stream':False}
                body.update({'input':'hello'} if protocol=='responses' else {'messages':[{'role':'user','content':'hello'}]})
                if protocol=='anthropic':body['max_tokens']=50
                conn,response=self.request(agent,protocol,body)
                value=json.loads(response.read());conn.close();self.assertEqual(response.status,200,value)
                self.assertEqual(self.calls[-1][1]['x-api-key'],'team-key.team-secret')
        self.assertEqual(len(self.get_calls),2)  # cached, no new key creation
        ledger=''.join(p.read_text(encoding='utf-8') for p in self.data.rglob('*.jsonl'))
        self.assertNotIn('fixture-team-token',ledger);self.assertNotIn('team-secret',ledger)

    def test_team_missing_key_fails_without_creating(self):
        self.creds['oauth:bigmodel:access_token']=self.enc('fixture-team-token');self.write_creds()
        self.settings['providerFamilyConnectionSelections']['bigmodel']={'kind':'team-coding-plan','organizationId':'org','projectId':'proj'}
        self.write_settings();self.team_keys=[]
        with self.assertRaisesRegex(ValueError,'现有团队套餐密钥'):zcode.resolve_auth(zcode.read_auth())
        self.assertEqual(len(self.get_calls),1);self.assertEqual(self.calls,[])

    def test_account_or_plan_change_requires_reconnect_before_sending(self):
        p=zcode.provider_config(self.cfg,[{'id':'fixture-model'}],auth=zcode.read_auth())
        self.cfg['providers']['zcode-subscription']=p
        providers,mapping=export_connections(self.cfg)
        (self.data/'providers.json').write_bytes(packed(providers));(self.data/'hub-connections.json').write_bytes(packed(mapping))
        self.creds['oauth:bigmodel:user_info']=self.enc(json.dumps({'id':'other-id'}))
        self.creds['account-provider:coding-plan:account:bigmodel-individual-coding-plan:account:other-id:api-key']=self.enc('other.key')
        self.write_creds()
        conn,response=self.request('pi','chat',{'model':'fixture-model','messages':[{'role':'user','content':'hello'}]})
        value=json.loads(response.read());conn.close()
        self.assertEqual(response.status,401,value);self.assertIn('套餐已切换',value['error']['message']);self.assertEqual(self.calls,[])

    def test_settings_report_changed_account_without_exposing_identity(self):
        runtime=Mock();obs=Mock();obs.settings.return_value={'content':False}
        with patch.object(server,'gateway',return_value=runtime),patch.object(server,'observations',return_value=obs):
            self.assertTrue(server.api_zcode_subscription_connect()['connected'])
            self.creds['oauth:bigmodel:user_info']=self.enc(json.dumps({'id':'other-id'}))
            self.creds['account-provider:coding-plan:account:bigmodel-individual-coding-plan:account:other-id:api-key']=self.enc('other.key')
            self.write_creds();status=server.api_zcode_subscription()
        self.assertTrue(status['needsRefresh']);self.assertIn('账号或套餐已切换',status['message'])
        self.assertNotIn('other-id',json.dumps(status));self.assertNotIn('other.key',json.dumps(status))

    def test_switch_during_key_resolution_stops_request_before_upstream(self):
        resolve=zcode.resolve_auth
        def switch(auth):
            result=resolve(auth)
            self.creds['oauth:bigmodel:user_info']=self.enc(json.dumps({'id':'other-id'}))
            self.creds['account-provider:coding-plan:account:bigmodel-individual-coding-plan:account:other-id:api-key']=self.enc('other.key')
            self.write_creds()
            return result
        with patch.object(zcode,'resolve_auth',side_effect=switch):
            conn,response=self.request('pi','chat',{'model':'fixture-model','messages':[{'role':'user','content':'hello'}]})
            value=json.loads(response.read());conn.close()
        self.assertEqual(response.status,401,value);self.assertIn('期间 ZCode 账号已切换',value['error']['message']);self.assertEqual(self.calls,[])

    def test_old_model_is_rejected_when_current_plan_catalog_excludes_it(self):
        p=self.cfg['providers']['zcode-subscription'];p['subscriptionModels']=['current-model']
        self.assertEqual(engine.agent_connection(p,'pi')['models'],[])
        self.assertEqual(p['models'],[{'id':'fixture-model'}])  # historical metadata is retained
        providers,mapping=export_connections(self.cfg)
        (self.data/'providers.json').write_bytes(packed(providers));(self.data/'hub-connections.json').write_bytes(packed(mapping))
        conn,response=self.request('pi','chat',{'model':'fixture-model','messages':[{'role':'user','content':'hello'}]})
        value=json.loads(response.read());conn.close();self.assertEqual(response.status,400,value)
        self.assertIn('当前 ZCode 套餐目录',value['error']['message']);self.assertEqual(self.calls,[])

    def test_individual_key_location_is_selected_by_current_account(self):
        from urllib.parse import quote
        name='project-key-location:v1:'+quote(json.dumps(['https://evil.invalid','bigmodel','fixture-id',None,1,'non-team']),safe='')
        self.creds.pop('account-provider:coding-plan:account:bigmodel-individual-coding-plan:account:fixture-id:api-key')
        self.creds['oauth:bigmodel:access_token']=self.enc('fixture-personal-token')
        self.creds[name]=self.enc(json.dumps({'organizationId':'org-1','projectId':'project-1','apiKeyId':'personal-key'}));self.write_creds()
        auth=zcode.read_auth();resolved=zcode.resolve_auth(auth)
        self.assertEqual(resolved['key'],'personal-key.team-secret')
        self.assertTrue(self.get_calls[0][0].endswith('/copy/personal-key'))

    def test_start_plan_keeps_user_system_and_messages_and_does_not_write_login(self):
        self.creds['zcodejwttoken']=self.enc('fixture.start.token');self.write_creds();before=self.auth_file.read_bytes()
        self.settings['providerFamilyConnectionSelections']['bigmodel']={'kind':'start-plan'};self.write_settings()
        body={'model':'fixture-model','stream':False,'system':'USER SYSTEM','max_tokens':50,'messages':[{'role':'user','content':'hello'}]}
        with patch.object(zcode,'START_BASE',f'http://127.0.0.1:{self.vendor.server_port}/anthropic'):
            conn,response=self.request('claude','anthropic',body);value=json.loads(response.read());conn.close()
        self.assertEqual(response.status,200,value)
        sent=self.calls[-1];self.assertEqual(sent[1]['Authorization'],'Bearer fixture.start.token')
        self.assertNotIn('x-api-key',sent[1]);self.assertIn('USER SYSTEM',sent[2]['system'][-1]['text'])
        self.assertEqual(sent[2]['messages'][-1]['content'][0]['text'],'hello')
        self.assertEqual(self.auth_file.read_bytes(),before)
        ledger=''.join(p.read_text(encoding='utf-8') for p in self.data.rglob('*.jsonl'))
        self.assertNotIn('fixture.start.token',ledger)

    def test_start_plan_five_client_protocols_with_mock_upstream(self):
        self.creds['zcodejwttoken']=self.enc('fixture.start.token');self.write_creds()
        self.settings['providerFamilyConnectionSelections']['bigmodel']={'kind':'start-plan'};self.write_settings()
        with patch.object(zcode,'START_BASE',f'http://127.0.0.1:{self.vendor.server_port}/anthropic'):
            for agent,protocol in [('claude','anthropic'),('codex','responses'),('pi','chat'),('dsh','chat'),('opencode','chat')]:
                with self.subTest(agent=agent):
                    body={'model':'fixture-model','stream':False}
                    body.update({'input':'hello'} if protocol=='responses' else {'messages':[{'role':'user','content':'hello'}]})
                    if protocol=='anthropic':body['max_tokens']=50
                    conn,response=self.request(agent,protocol,body)
                    value=json.loads(response.read());conn.close();self.assertEqual(response.status,200,value)
                    self.assertEqual(self.calls[-1][1]['Authorization'],'Bearer fixture.start.token')
                    self.assertIn('hello',json.dumps(self.calls[-1][2]['messages']))

    def test_client_capabilities_are_explicit(self):
        with self.assertRaisesRegex(ValueError,'web_search.*disabled'):
            zcode.request_body({'input':'hello','tools':[{'type':'web_search'}]},'responses','fixture-model')
        with self.assertRaisesRegex(ValueError,'previous_response_id'):
            zcode.request_body({'input':'hello','previous_response_id':'resp_old'},'responses','fixture-model')

    def test_multimodal_tool_results_and_reasoning_history_are_preserved(self):
        body={'input':[{'role':'user','content':[{'type':'input_text','text':'look'},
              {'type':'input_image','image_url':'data:image/png;base64,YWJj'}]},
              {'type':'reasoning','summary':[{'type':'summary_text','text':'remember this context'}]},
              {'type':'function_call','call_id':'call-a','name':'inspect','arguments':'{}'},
              {'type':'function_call_output','call_id':'call-a','output':[{'type':'input_text','text':'caption'},
              {'type':'input_image','image_url':'data:image/png;base64,YWJj'}]}]}
        request,_=zcode.request_body(body,'responses','fixture-model')
        messages=request['messages']
        self.assertIn('remember this context',json.dumps(messages))
        result=next(part for m in messages for part in m['content'] if part.get('type')=='tool_result')
        self.assertEqual([p['type'] for p in result['content']],['text','image'])
        self.assertEqual(result['content'][1]['source']['data'],'YWJj')
        body['input'][1]['encrypted_content']='opaque'
        with self.assertRaisesRegex(ValueError,'加密推理'):zcode.request_body(body,'responses','fixture-model')
        body['input'][1].pop('encrypted_content')
        body['input'][-1]['output']=[{'type':'redacted_thinking','data':'opaque'}]
        with self.assertRaisesRegex(ValueError,'工具结果包含无法安全迁移'):zcode.request_body(body,'responses','fixture-model')

    def test_consecutive_and_parallel_tool_rounds_keep_ids_and_results(self):
        call=lambda ident:{'id':ident,'type':'function','function':{'name':'read_file','arguments':json.dumps({'path':ident})}}
        body={'model':'fixture-model','messages':[{'role':'user','content':'read files'},
              {'role':'assistant','content':None,'tool_calls':[call('a'),call('b')]},
              {'role':'tool','tool_call_id':'a','content':'first'},
              {'role':'tool','tool_call_id':'b','content':'second'},
              {'role':'assistant','content':None,'tool_calls':[call('c')]},
              {'role':'tool','tool_call_id':'c','content':'third'},
              {'role':'user','content':'summarize'}]}
        conn,response=self.request('pi','chat',body);data=json.loads(response.read());conn.close()
        self.assertEqual(response.status,200,data)
        messages=self.calls[-1][2]['messages']
        calls=[part['id'] for row in messages for part in row['content'] if part.get('type')=='tool_use']
        results=[(part['tool_use_id'],part['content']) for row in messages for part in row['content'] if part.get('type')=='tool_result']
        self.assertEqual(calls,['a','b','c']);self.assertEqual(results,[('a','first'),('b','second'),('c','third')])

    def test_start_plan_model_matching_rejects_spent_or_unassociated_buckets(self):
        balance={'server_time':1000,'plans':[{'user_plan_id':'a','status':'active','ends_at':'2000'}],
                 'balances':[{'user_plan_id':'a','total_units':'100','used_units':'100','capabilities':['model:spent']},
                             {'user_plan_id':'a','remaining_units':'1','capabilities':['model:available']},
                             {'remaining_units':5,'capabilities':['model:unassociated']}]}
        self.assertEqual(zcode._start_models(balance),['available'])

    def test_start_plan_connect_lists_only_current_eligible_models(self):
        self.creds['zcodejwttoken']=self.enc('fixture.start.token');self.write_creds()
        self.settings['providerFamilyConnectionSelections']['bigmodel']={'kind':'start-plan'};self.write_settings()
        balance={'server_time':1000,'plans':[{'user_plan_id':'p','status':'active','ends_at':2000}],
                 'balances':[{'user_plan_id':'p','remaining_units':2,'capabilities':['model:fixture-model']},
                             {'user_plan_id':'p','remaining_units':0,'capabilities':['model:spent-model']}]}
        runtime=Mock();obs=Mock();obs.settings.return_value={'content':False}
        with patch.object(zcode,'start_balance',return_value=balance),patch.object(server,'gateway',return_value=runtime),patch.object(server,'observations',return_value=obs):
            result=server.api_zcode_subscription_connect()
        self.assertTrue(result['connected']);self.assertEqual(result['models'],1);self.assertEqual(result['planKind'],'start-plan')
        config=json.loads(server.HUB_CONFIG.read_text(encoding='utf-8'))['providers']['zcode-subscription']
        self.assertEqual(config['subscriptionModels'],['fixture-model']);self.assertNotIn('fixture.start.token',json.dumps(config))

    def test_known_global_agent_token_cannot_access_subscription(self):
        c=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=8)
        c.request('POST','/zcode-subscription/pi/v1/chat/completions',packed({'model':'fixture-model','messages':[{'role':'user','content':'hello'}]}),
                  {'Authorization':'Bearer agent-hub-pi','Content-Type':'application/json'})
        response=c.getresponse();response.read();c.close();self.assertEqual(response.status,401)

    def test_five_previews_apply_and_restore_without_touching_zcode_login(self):
        before=self.auth_file.read_bytes()
        for global_gateway in (False,True):
            with self.subTest(global_gateway=global_gateway), isolated() as home:
                h=engine.Hub();h.cfg['gateway']={'enabled':global_gateway}
                h.cfg['providers']['zcode-subscription']=zcode.provider_config(h.cfg,[{'id':'fixture-model','contextWindow':12345,'maxTokens':4096}])
                h.cfg['sync']['targets']['codex'].update(enabled=True,mode='third-party')
                h.cfg['sync']['targets']['opencode']={'enabled':True}
                h.cfg['modelSync']={'default':{'provider':'zcode-subscription','model':'fixture-model'}}
                engine.save_json(engine.HUB_CONFIG,h.cfg)
                (home/'.codex').mkdir(exist_ok=True)
                native_auth=home/'.codex/auth.json';native_auth.write_text('{"synthetic":"original"}',encoding='utf-8')
                (home/'.codex/config.toml').write_text('model = "official-model"\n',encoding='utf-8')
                plan=h.preview_push(['claude','codex','pi','dsh','opencode'])
                self.assertIn('web_search',json.dumps(plan,ensure_ascii=False))
                result=h.apply_preview(plan['planId'])
                config=(home/'.codex/config.toml').read_text(encoding='utf-8')
                self.assertIn(h.cfg['providers']['zcode-subscription']['apiKey'],config)
                self.assertNotIn('web_search =',config)
                self.assertEqual(json.loads((home/'.config/opencode/opencode.json').read_text(encoding='utf-8'))['model'],'zcode-subscription/fixture-model')
                h.restore_transaction(result['transactionId'])
                self.assertEqual(native_auth.read_text(encoding='utf-8'),'{"synthetic":"original"}')
        self.assertEqual(self.auth_file.read_bytes(),before)

    def test_connect_disconnect_and_account_change(self):
        before=self.auth_file.read_bytes();runtime=Mock();obs=Mock();obs.settings.return_value={'content':False}
        with patch.object(server,'gateway',return_value=runtime),patch.object(server,'observations',return_value=obs):
            result=server.api_zcode_subscription_connect();self.assertTrue(result['connected'])
            config=server.HUB_CONFIG.read_text(encoding='utf-8');self.assertNotIn(self.key,config)
            self.assertFalse(server.api_zcode_subscription_disconnect()['connected'])
        self.assertEqual(self.auth_file.read_bytes(),before)
        original=zcode.models
        def change(auth):
            value=original(auth);self.creds['account-provider:coding-plan:account:bigmodel-individual-coding-plan:account:fixture-id:api-key']=self.enc('new.key');self.write_creds();return value
        with patch.object(zcode,'models',side_effect=change),self.assertRaisesRegex(ValueError,'账号已切换'):
            server.api_zcode_subscription_connect()

    def test_five_endpoints_and_tool_roundtrip(self):
        for agent,protocol in [('claude','anthropic'),('codex','responses'),('pi','chat'),('dsh','chat'),('opencode','chat')]:
            with self.subTest(agent=agent):
                body={'model':'fixture-model','stream':False}
                body.update({'input':'hello'} if protocol=='responses' else {'messages':[{'role':'user','content':'hello'}]})
                if protocol=='anthropic':body['max_tokens']=50
                conn,response=self.request(agent,protocol,body);data=json.loads(response.read());conn.close()
                self.assertEqual(response.status,200,data)
                if protocol=='anthropic':self.assertEqual(data['content'][0]['text'],'fixture-result')
                elif protocol=='responses':self.assertEqual(data['output'][0]['content'][0]['text'],'fixture-result')
                else:self.assertEqual(data['choices'][0]['message']['content'],'fixture-result')
        body={'model':'fixture-model','stream':False,'messages':[{'role':'user','content':'read'}],
              'tools':[{'type':'function','function':{'name':'read_file','parameters':{'type':'object','properties':{'path':{'type':'string'}}}}}]}
        conn,response=self.request('pi','chat',body);first=json.loads(response.read());conn.close();self.assertEqual(response.status,200,first)
        call=first['choices'][0]['message']['tool_calls'][0];self.assertEqual(call['function']['name'],'read_file')
        body['messages'] += [first['choices'][0]['message'],{'role':'tool','tool_call_id':call['id'],'content':'file-data'}]
        conn,response=self.request('pi','chat',body);second=json.loads(response.read());conn.close()
        self.assertEqual(response.status,200,second);self.assertEqual(second['choices'][0]['message']['content'],'fixture-result')
        self.assertTrue(any(part.get('type')=='tool_result' and part.get('content')=='file-data' for m in self.calls[-1][2]['messages'] for part in m['content']))
        self.assertTrue(all(call[1].get('x-api-key')==self.key for call in self.calls))
        ledger=''.join(p.read_text(encoding='utf-8') for p in self.data.rglob('*.jsonl'))
        self.assertNotIn(self.key,ledger);self.assertNotIn('fixture-id',ledger)

    def test_stream_and_truncation_and_upstream_error(self):
        body={'model':'fixture-model','stream':True,'messages':[{'role':'user','content':'read'}],
              'tools':[{'type':'function','function':{'name':'read_file','parameters':{'type':'object'}}}]}
        conn,response=self.request('opencode','chat',body);wire=response.read();conn.close()
        self.assertEqual(response.status,200);self.assertIn(b'call_fixture',wire);self.assertIn(b'probe.txt',wire)
        self.assertTrue(wire.endswith(b'data: [DONE]\n\n'))
        self.truncate=True;body['stream']=False
        conn,response=self.request('pi','chat',body);data=response.read();conn.close();self.assertEqual(response.status,502,data)
        self.truncate=False;self.fail=True
        conn,response=self.request('pi','chat',body);data=response.read();conn.close();self.assertEqual(response.status,401,data)
        self.assertNotIn(self.key.encode(),data)

    def test_client_cancellation_closes_upstream_and_records_interruption(self):
        self.hold=True
        body=packed({'model':'fixture-model','stream':True,'messages':[{'role':'user','content':'read'}]})
        headers=('POST /zcode-subscription/pi/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                 'Authorization: Bearer '+self.cfg['providers']['zcode-subscription']['apiKey']+'\r\n'
                 'Content-Type: application/json\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()
        sock=socket.create_connection(('127.0.0.1',self.http.server_port),timeout=5)
        try:
            sock.sendall(headers+body);self.assertIn(b'200',sock.recv(1024))
            sock.shutdown(socket.SHUT_RDWR);sock.close()
            until=time.monotonic()+3
            while self.http.state.active and time.monotonic()<until:time.sleep(.02)
            self.assertEqual(self.http.state.active,0)
            ledger=[json.loads(x) for x in (self.data/'usage.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(ledger[-1]['status'],499);self.assertEqual(ledger[-1]['stop'],'aborted')
            self.assertEqual(len(self.calls),1)
        finally:
            sock.close();self.release.set()

    def test_quota_error_inside_http_200_becomes_actionable_429_without_retry(self):
        self.quota_json=True
        conn,response=self.request('pi','chat',{'model':'fixture-model','messages':[{'role':'user','content':'hello'}]})
        data=response.read().decode();conn.close()
        self.assertEqual(response.status,429);self.assertIn('额度或速率受限',data)
        self.assertNotIn(self.key,data);self.assertEqual(len(self.calls),1)

    def test_streamed_quota_error_is_sanitized_and_recorded_as_429(self):
        self.fail_event=True
        conn,response=self.request('pi','chat',{'model':'fixture-model','messages':[{'role':'user','content':'hello'}]})
        data=response.read().decode();conn.close()
        self.assertEqual(response.status,429);self.assertIn('额度或速率受限',data);self.assertNotIn(self.key,data)
        until=time.monotonic()+3
        while self.http.state.active and time.monotonic()<until:time.sleep(.02)
        ledger=[json.loads(x) for x in (self.data/'usage.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(ledger[-1]['status'],429);self.assertEqual(len(self.calls),1)

    def test_native_thinking_signature_is_preserved(self):
        collector=zcode.AnthropicCollector('alias')
        for event in [{'type':'message_start','message':{'model':'upstream','usage':{}}},
          {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}},
          {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'reason'}},
          {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'signed'}},
          {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':2}},
          {'type':'message_stop'}]:collector.feed(event)
        self.assertTrue(collector.finished);self.assertEqual(collector.value['content'][0]['signature'],'signed')
        self.assertEqual(collector.value['model'],'alias')


if __name__=='__main__':unittest.main()
