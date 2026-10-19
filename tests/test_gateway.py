import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from support import isolated, engine, server
from gateway_runtime import export_connections, route_id, Runtime, compile_codex_native
from gateway_observation import read_requests, speed
from observation import Store, Parsed, key


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/'data',self.root)
        self.addCleanup(self.store.close)
        self.path=self.root/'gateway/usage.jsonl'
        self.path.parent.mkdir()

    def ledger(self,rows):
        self.path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')

    def row(self,**kw):
        return {'route_id':123,'t':time.time(),'agent':'pi','provider':'fixture','model':'m',
            'req':'fixture/m','served':'actual-m','in':100,'out':20,'cache_read':50,'cache_write':0,
            'ms':1000,'ttft_ms':200,'first_text_ms':210,'flow_ms':800,'status':200,
            'stop':'stop','session':'native-session','response_id':'response-1',**kw}

    def test_exact_response_merge_both_ingestion_orders_and_no_cache_double_count(self):
        p=Parsed('pi',self.root/'native.jsonl',False,[])
        p.sid='native-session';p.started=time.time()
        p.request('response-1',time.time(),{'input':100,'output':20,'cacheRead':50,'cacheWrite':0},model='m')
        self.ledger([self.row(session='')])
        self.store.ingest(p);self.store.ingest_gateway(self.path);self.store.ingest(p)
        r=self.store.query()['requests'][0]
        self.assertEqual(self.store.query()['total'],1)
        self.assertEqual((r['sourceType'],r['sessionId'],r['usage']['context']),('gateway+native','native-session',150))
        self.assertEqual(r['durationMs'],1000)
        self.assertEqual(r['ttftMs'],200)

    def test_failed_handed_on_attempt_is_not_final_failure(self):
        self.ledger([self.row(response_id=None,status=503,err='busy',handed_on=True,stop='',out=0),self.row(tries=2)])
        self.store.ingest_gateway(self.path)
        d=self.store.query();r=d['requests'][0]
        self.assertEqual((d['summary']['calls'],d['summary']['failed'],d['summary']['succeeded']),(1,0,1))
        self.assertEqual((r['attempts'],r['retried']),(2,True))
        self.assertEqual(d['summary']['tokens']['input']['value'],200)
        self.assertEqual(r['usage']['context'],150)
        self.assertEqual(d['summary']['attempts'],2)
        self.assertIsNone(r['cost'])

    def test_same_session_never_merges_distinct_or_missing_response_ids(self):
        self.ledger([self.row(),self.row(route_id=124,response_id=None),self.row(route_id=125,response_id=None)])
        self.store.ingest_gateway(self.path)
        self.store.ingest_gateway(self.path)
        self.assertEqual(self.store.query()['total'],3)

    def test_unknown_product_uses_exact_native_response_identity_only(self):
        self.ledger([self.row(agent='custom-app')])
        self.assertEqual(read_requests(self.path),[])
        self.assertEqual(len(read_requests(self.path,identities={'response-1':{'codex'}})),1)
        self.assertEqual(read_requests(self.path,identities={'response-1':{'codex','pi'}}),[])

    def test_route_parts_have_estimate_provenance_and_no_body_fragments(self):
        routes=self.path.parent/'routing';routes.mkdir()
        (routes/'day.jsonl').write_text(json.dumps({'id':123,'done':True,'prompt':{
            'tokens':150,'counted':True,'window':128000,'parts':[{'kind':'system','tokens':75,'items':[{'name':'private fragment','tokens':75}]}]}})+'\n')
        self.ledger([self.row()])
        r=read_requests(self.path)[0]
        self.assertEqual(r['contextParts']['parts'],[{'kind':'system','tokens':75}])
        self.assertNotIn('private fragment',json.dumps(r))
        self.assertIn('ASCII',r['contextParts']['method'])

    def test_failed_zero_usage_is_unknown_and_no_stop_does_not_prove_success(self):
        self.ledger([self.row(status=401,err='Unauthorized',stop='',**{'in':0,'out':0,'cache_read':0})])
        r=read_requests(self.path)[0]
        self.assertIsNone(r['usage']['context'])
        self.assertEqual(r['status'],'failed')
        self.ledger([self.row(stop='')])
        self.assertEqual(read_requests(self.path)[0]['status'],'unknown')

    def test_stream_cancel_and_speed_burst_or_hidden_reasoning(self):
        self.ledger([self.row(status=499,stop='cancelled')])
        self.assertEqual(read_requests(self.path)[0]['status'],'interrupted')
        self.assertIsNone(speed(self.row(ms=201)))
        self.assertIsNone(speed(self.row(reasoning=10,first_text_ms=0)))
        self.assertIsNone(speed(self.row(flow_ms=1)))
        self.assertGreater(speed(self.row()),0)

    def test_stopped_collection_and_clear_watermark_apply_to_gateway(self):
        self.ledger([self.row()])
        self.store.set_settings({'enabled':False})
        self.store.ingest_gateway(self.path)
        self.assertEqual(self.store.query()['total'],0)
        self.store.settings_path.write_text(json.dumps({'enabled':True,'content':False,'retentionDays':0,'clearedBefore':0}))
        self.store.ingest_gateway(self.path)
        self.assertEqual(self.store.query()['total'],1)
        self.store.cleanup(clear=True)
        self.store.settings_path.write_text(json.dumps({**self.store.settings(),'enabled':True}))
        self.store.ingest_gateway(self.path)
        self.assertEqual(self.store.query()['total'],0)

    def test_export_only_explicit_four_agent_connections_and_no_fallback(self):
        with isolated():
            h=engine.Hub();rows,mapping=export_connections(h.cfg)
            self.assertEqual(set(r['agent'] for r in mapping.values()),{'claude','codex','pi','dsh'})
            self.assertTrue(all(not r['fallback'] for r in rows['providers']))
            self.assertNotEqual(route_id('pi','deepseek'),route_id('claude','deepseek'))

    def test_existing_transaction_renders_namespaces_and_restores_original_bytes(self):
        with isolated() as home:
            h=engine.Hub();h.cfg['gateway']={'enabled':True};engine.save_json(engine.HUB_CONFIG,h.cfg)
            (home/'.pi/agent').mkdir(parents=True,exist_ok=True)
            (home/'.pi/agent/models.json').write_text('{"providers":{},"custom":"preserve"}')
            original=(home/'.pi/agent/models.json').read_bytes()
            p=h.preview_push(['pi','claude'])
            self.assertEqual((home/'.pi/agent/models.json').read_bytes(),original)
            result=h.apply_preview(p['planId'])
            block=json.loads((home/'.pi/agent/models.json').read_text())['providers']['deepseek']
            self.assertEqual(block['baseUrl'],'http://127.0.0.1:8789/v1')
            self.assertTrue(block['models'][0]['id'].startswith(route_id('pi','deepseek')+'/'))
            h.restore_transaction(result['transactionId'])
            self.assertEqual((home/'.pi/agent/models.json').read_bytes(),original)

    def test_native_codex_preserves_login_config_and_restores_external_base(self):
        p=self.root/'.codex/config.toml';p.parent.mkdir()
        p.write_text('model="native-model"\nopenai_base_url="https://my-route.invalid/backend-api/codex"\n')
        compile_codex_native(self.root,{'gateway':{'enabled':True}})
        self.assertIn('model="native-model"',p.read_text())
        compile_codex_native(self.root,{'gateway':{'enabled':False}})
        self.assertIn('https://my-route.invalid/backend-api/codex',p.read_text())
        self.assertFalse((p.parent/'auth.json').exists())

    def test_native_codex_external_edit_is_not_overwritten(self):
        p=self.root/'.codex/config.toml';p.parent.mkdir();p.write_text('model="native"\n')
        compile_codex_native(self.root,{'gateway':{'enabled':True}})
        p.write_text('openai_base_url="https://external.invalid"\n')
        with self.assertRaisesRegex(ValueError,'外部修改'):
            compile_codex_native(self.root,{'gateway':{'enabled':False}})

    def test_runtime_default_settings_and_content_cleanup_are_isolated(self):
        r=Runtime(self.root/'managed')
        r.configure()
        settings=json.loads((r.directory/'settings.json').read_text())
        self.assertFalse(settings['accountDiscovery'])
        self.assertEqual(settings['engine'],'agent-hub')
        self.assertFalse(settings['gatewayConversations'])
        self.assertFalse(settings['requestArchive'])
        (r.directory/'gateway-conversations').mkdir()
        (r.directory/'gateway-conversations/old.jsonl').write_text('body')
        r.configure(False)
        self.assertFalse((r.directory/'gateway-conversations').exists())

    def test_runtime_refuses_foreign_port_without_adopting_process(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));s.listen()
            r=Runtime(self.root/'managed',lambda:{'gateway':{'port':s.getsockname()[1]}})
            with patch.object(r,'install'):
                with self.assertRaises(ValueError):r.start()
            self.assertIsNone(r.process)
            self.assertEqual(r.status()['state'],'failed')

    def test_gateway_body_fallback_is_opt_in_bounded_and_explicitly_replayed(self):
        import hashlib
        self.ledger([self.row()]);self.store.ingest_gateway(self.path)
        digest=hashlib.sha256(json.dumps(['pi','native-session'],separators=(',',':')).encode()).hexdigest()
        directory=self.store.root/'gateway/config/magpie/gateway-conversations/2026-10-17'/digest
        directory.mkdir(parents=True)
        (directory/'turns.jsonl').write_text(json.dumps({'agent':'pi','session':'native-session','time':time.time(),
            'input':[{'role':'user','text':'history'}],'output':[{'role':'assistant','text':'answer'}]})+'\n')
        self.assertEqual(self.store.session_detail('pi','native-session')['messages'],[])
        self.store.settings_path.write_text(json.dumps({**self.store.settings(),'content':True}))
        detail=self.store.session_detail('pi','native-session')
        self.assertEqual(detail['contentSource'],'gateway-on-demand')
        self.assertIn('重放历史',detail['messages'][0]['role'])
        self.assertEqual(self.store.status()['contentRecords'],0)

    def test_gateway_model_alias_retains_pi_per_model_options(self):
        with isolated() as home:
            h=engine.Hub();h.cfg['gateway']={'enabled':True};engine.save_json(engine.HUB_CONFIG,h.cfg)
            file=home/'.pi/agent/models.json';file.parent.mkdir(parents=True)
            file.write_text(json.dumps({'providers':{'deepseek':{'models':[{'id':'deepseek/model-1','headers':{'custom':'preserve'},'compat':{'customFlag':True}}]}}}))
            plan=h.preview_push(['pi']);h.apply_preview(plan['planId'])
            model=json.loads(file.read_text())['providers']['deepseek']['models'][0]
            self.assertEqual(model['headers']['custom'],'preserve')
            self.assertTrue(model['compat']['customFlag'])

    def test_codex_gateway_uses_existing_transaction_for_official_connect_disconnect(self):
        with isolated() as home:
            h=engine.Hub();h.cfg['gateway']={'enabled':True};h.cfg['sync']['targets']['codex']['enabled']=True
            file=home/'.codex/config.toml';file.parent.mkdir();file.write_text('model = "native-model"\n')
            engine.save_json(engine.HUB_CONFIG,h.cfg)
            plan=h.preview_push(['codex']);h.apply_preview(plan['planId'])
            self.assertIn('/backend-api/codex',file.read_text())
            self.assertIn('native-model',file.read_text())
            h.cfg['gateway']['enabled']=False;engine.save_json(engine.HUB_CONFIG,h.cfg)
            plan=h.preview_push(['codex']);h.apply_preview(plan['planId'])
            self.assertNotIn('openai_base_url',file.read_text())
            self.assertFalse((file.parent/'auth.json').exists())

    def test_claude_preserved_tier_uses_same_model_and_keeps_display_label(self):
        with isolated() as home:
            h=engine.Hub();h.cfg['gateway']={'enabled':True}
            h.cfg['modelSync']['overrides']={'claude':{'provider':'deepseek','model':'deepseek/model-1','preserveTiers':['opus']}}
            file=home/'.claude/settings.json';file.parent.mkdir()
            file.write_text(json.dumps({'env':{'ANTHROPIC_DEFAULT_OPUS_MODEL':'deepseek/model-2','ANTHROPIC_DEFAULT_OPUS_MODEL_NAME':'My Opus label'},'permissions':{'custom':'keep'}}))
            engine.save_json(engine.HUB_CONFIG,h.cfg)
            plan=h.preview_push(['claude']);h.apply_preview(plan['planId'])
            data=json.loads(file.read_text());env=data['env']
            self.assertEqual(env['ANTHROPIC_DEFAULT_OPUS_MODEL'],route_id('claude','deepseek')+'/deepseek/model-2')
            self.assertEqual(env['ANTHROPIC_DEFAULT_OPUS_MODEL_NAME'],'My Opus label')
            self.assertEqual(data['permissions']['custom'],'keep')

    def test_disabled_routed_target_prevents_stopping_gateway_and_keeps_config(self):
        with isolated() as home:
            h=engine.Hub();h.cfg['gateway']={'enabled':True};h.cfg['sync']['targets']['pi']['enabled']=False
            file=home/'.pi/agent/models.json';file.parent.mkdir(parents=True)
            file.write_text('{"providers":{"p":{"baseUrl":"http://127.0.0.1:8789/v1"}}}')
            engine.save_json(engine.HUB_CONFIG,h.cfg)
            with self.assertRaisesRegex(ValueError,'请先重新启用'):
                server.api_gateway_preview({'enabled':False})
            self.assertTrue(server.hub().cfg['gateway']['enabled'])
            self.assertIn('8789/v1',file.read_text())

    def test_known_nonstandard_credential_is_removed_from_gateway_errors(self):
        self.store.config=lambda:{'providers':{'fixture':{'apiKey':'fixture-private-credential-value'}}}
        self.ledger([self.row(status=401,err='bad fixture-private-credential-value',stop='')])
        self.store.ingest_gateway(self.path)
        self.assertNotIn('fixture-private-credential-value',json.dumps(self.store.query()))

    def test_native_batch_loads_price_configuration_once(self):
        from unittest.mock import Mock
        self.store.config=Mock(return_value={})
        p=Parsed('pi',self.root/'native.jsonl',False,[])
        for i in range(20):p.request(str(i),time.time(),{'input':1,'output':2,'cacheRead':0,'cacheWrite':0},model='m',provider='fixture')
        self.store.ingest(p)
        self.assertEqual(self.store.config.call_count,1)

    def test_gateway_rejects_unresolved_codex_environment_key_before_writes(self):
        with isolated() as home, patch.dict(os.environ,{'HUB_TEST_MISSING_KEY':''}):
            h=engine.Hub();h.cfg['gateway']={'enabled':True}
            h.cfg['sync']['targets']['codex'].update(enabled=True,mode='third-party')
            h.cfg['providers']['deepseek']['agents']['codex'].update(apiKey='',apiKeyEnv='HUB_TEST_MISSING_KEY')
            engine.save_json(engine.HUB_CONFIG,h.cfg)
            with self.assertRaisesRegex(ValueError,'认证信息'):
                h.preview_push(['codex'])
            self.assertFalse((home/'.codex/config.toml').exists())


if __name__=='__main__':unittest.main()
