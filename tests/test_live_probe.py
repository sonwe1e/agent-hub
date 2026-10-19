"""The real-call harness must fail safely without requiring real calls in CI."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('live_probe',Path(__file__).resolve().parents[1]/'scripts/verify-live-clients.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class LiveProbeTests(unittest.TestCase):
    def test_workbuddy_prepare_uses_only_private_config_and_no_oauth_copy(self):
        from workbuddy import provider_config
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir()
            cfg={'providers':{}}
            cfg['providers']['workbuddy-ai']=provider_config(cfg,[{'id':'model','name':'Fixture','maxTokens':1024}])
            probe.write(source/'providers.json',cfg)
            with patch.object(probe,'ROOT',source):
                for agent in ('claude','codex','pi','dsh'):
                    home=root/agent;home.mkdir()
                    probe.prepare_workbuddy(home,agent,'model')
                    self.assertFalse((home/'.codex/auth.json').exists())
                    self.assertFalse((home/'.claude/.credentials.json').exists())
                    self.assertFalse((home/'workbuddy-auth.json').exists())
                    if agent=='dsh':
                        credentials=yaml.safe_load((home/'.dsh/.credentials.yaml').read_text(encoding='utf-8'))
                        self.assertEqual(credentials['refs']['WORKBUDDY_AI_API_KEY'],cfg['providers']['workbuddy-ai']['apiKey'])
            self.assertEqual(probe.read(source/'providers.json'),cfg)

    def test_expiring_or_unknown_oauth_copy_never_rotates_live_refresh_token(self):
        with patch.object(probe.time,'time',return_value=1000):
            self.assertTrue(probe.oauth_refresh_risk({'type':'oauth','expires':1100},150))
            self.assertTrue(probe.oauth_refresh_risk({'type':'oauth'},150))
            self.assertFalse(probe.oauth_refresh_risk({'type':'oauth','expires':2000},150))
            self.assertFalse(probe.oauth_refresh_risk({'type':'oauth','expires':2000000000000},150))
            self.assertFalse(probe.oauth_refresh_risk({'type':'api_key'},150))

    def test_model_ids_are_not_treated_as_authentication_secrets(self):
        value = {'providers':{'tokenfree':{'api':'openai-responses','models':[{'id':'step-5-preview','name':'Long model display name'}],
                 'apiKey':'private-credential-value','headers':{'Authorization':'Bearer private-auth-value'}}}}
        secrets = list(probe.credential_values(value))
        self.assertNotIn('step-5-preview',secrets)
        self.assertNotIn('openai-responses',secrets)
        self.assertIn('private-credential-value',secrets)
        self.assertIn('Bearer private-auth-value',secrets)
        text = probe.scrub(json.dumps(value),secrets)
        self.assertIn('step-5-preview',text)
        self.assertNotIn('private-credential-value',text)

    def test_query_redaction_does_not_corrupt_json_quotes(self):
        text = json.dumps({'message':'Download "https://example.org/file?sig=private-signature" failed'})
        value = probe.scrub(text,[])
        self.assertNotIn('private-signature',value)
        self.assertIn('[REDACTED-QUERY]',json.loads(value)['message'])

    def test_copied_credentials_are_removed_on_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            cleanup = {}
            with self.assertRaises(RuntimeError):
                with probe.private_home(Path(folder),cleanup) as scratch:
                    home = Path(scratch)
                    probe.write(home/'.codex/auth.json',{'access_token':'not-a-real-token'})
                    raise RuntimeError('test failure')
            self.assertTrue(cleanup['credentialsRemoved'])
            self.assertFalse(cleanup['remainingFiles'])
            self.assertFalse(home.exists())
