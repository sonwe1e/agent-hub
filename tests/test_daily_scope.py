import copy
import json
import unittest
from support import isolated, engine, server
from switch_plans import compile_plan, normalize_plan


class DailyScopeTests(unittest.TestCase):
    def setUp(self):
        self.context=isolated();self.home=self.context.__enter__()
        self.addCleanup(self.context.__exit__,None,None,None)

    def test_single_agent_choice_preserves_other_agents_and_legacy_data(self):
        cfg=engine.Hub().cfg
        before=copy.deepcopy(cfg['modelSync'])
        plan=normalize_plan({'name':'one','targets':['pi'],'agents':{'pi':{'policy':'fixed','provider':'openrouter','model':'openrouter/model-1'}}})
        changed,_,_,blocked=compile_plan(cfg,plan)
        self.assertFalse(blocked)
        self.assertEqual(changed['modelSync']['default'],before['default'])
        for agent in ('claude','codex','dsh'):
            self.assertEqual(engine.resolve_agent_model(changed,agent),engine.resolve_agent_model(cfg,agent))

    def test_keep_agent_excluded_from_preview_and_claude_keep_tier_retains_native(self):
        native=self.home/'.claude/settings.json';native.parent.mkdir(parents=True)
        native.write_text(json.dumps({'env':{'ANTHROPIC_DEFAULT_OPUS_MODEL':'native-kept-opus','ANTHROPIC_DEFAULT_OPUS_MODEL_NAME':'native-label'},'hooks':{'keep':True}}), encoding="utf-8")
        pi=self.home/'.pi/agent/settings.json';pi.parent.mkdir(parents=True);pi.write_text('{"defaultModel":"native-pi"}', encoding="utf-8")
        before=pi.read_bytes()
        plan={'name':'keep','targets':['claude','pi'],'base':{'provider':'deepseek','model':'deepseek/model-1'},'agents':{'pi':{'policy':'keep'},'claude':{'policy':'follow','tiers':{'opus':{'policy':'keep'}}}}}
        result=server.api_switch_plan_apply({'plan':plan})
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['targets'],['claude'])
        self.assertEqual(pi.read_bytes(),before)
        env=json.loads(native.read_text(encoding="utf-8"))['env']
        self.assertEqual(env['ANTHROPIC_DEFAULT_OPUS_MODEL'],'native-kept-opus')
        self.assertEqual(env['ANTHROPIC_DEFAULT_OPUS_MODEL_NAME'],'native-label')

    def test_hidden_legacy_connection_survives_active_form_save(self):
        cfg=engine.Hub().cfg
        legacy={'api':'openai-responses','apiKey':'old-private','models':[{'id':'legacy-model'}]}
        cfg['providers']['deepseek']['agents']['zcode']=legacy
        engine.save_json(server.HUB_CONFIG,cfg)
        server.api_update_provider('deepseek',{'agents':{'claude':{'api':'anthropic-messages'}}})
        self.assertEqual(engine.Hub().cfg['providers']['deepseek']['agents']['zcode'],legacy)

    def test_default_batch_excludes_historical_enabled_targets(self):
        import sync_transaction as tx
        cfg=engine.Hub().cfg
        chosen=tx._select(cfg,None)
        self.assertEqual(set(chosen),{'claude','pi','dsh'})
        self.assertTrue(cfg['sync']['targets']['zcode']['enabled'])

    def test_api_rejects_nonlocal_host_and_service_can_exit_cleanly(self):
        import threading
        import urllib.request
        import urllib.error
        from http.server import ThreadingHTTPServer
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{httpd.server_port}'
        try:
            request=urllib.request.Request(url+'/api/status',headers={'Host':'untrusted.example'})
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,403)
            error.exception.close()
            request=urllib.request.Request(url+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as response:self.assertTrue(json.load(response)['ok'])
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        finally:
            httpd.shutdown();httpd.server_close()


if __name__=='__main__':unittest.main()
