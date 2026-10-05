"""Preview/apply API contract tests. All files live in isolated temporary HOME."""
import json
import unittest
from unittest.mock import patch
from support import engine, server
import test_api as api_tests


class PreviewContractTests(unittest.TestCase):
    setUp = api_tests.APITests.setUp
    config = api_tests.APITests.config
    http = api_tests.APITests.http

    def test_status_defaults_missing_enabled_to_disabled_and_codex_to_official(self):
        cfg = self.config()
        del cfg['sync']['targets']['pi']['enabled']
        engine.save_json(server.HUB_CONFIG, cfg)
        status = server.api_status()
        self.assertFalse(status['targets']['pi']['enabled'])
        self.assertEqual(status['targets']['codex']['mode'], 'official')
        self.assertFalse(status['targets']['codex']['readonly'])
        with self.assertRaises(ValueError):
            server.api_preview({'targets': ['pi']})

    def test_cc_switch_is_not_offered_as_transactional_sync_target(self):
        cfg = self.config()
        cfg['sync']['targets']['cc-switch']['enabled'] = True
        engine.save_json(server.HUB_CONFIG, cfg)
        self.assertFalse(server.api_status()['targets']['cc-switch']['supported'])
        with self.assertRaises(ValueError):
            server.api_preview({'targets': ['cc-switch']})
        with self.assertRaises(ValueError):
            server.api_update_targets({'targets': {'cc-switch': {'enabled': True}}})

    def test_codex_mode_is_explicit_and_invalid_mode_is_atomic(self):
        before = server.HUB_CONFIG.read_bytes()
        for target, mode in [('codex', 'custom'), ('pi', 'third-party'), ('codex', True)]:
            with self.subTest(target=target, mode=mode), self.assertRaises(ValueError):
                server.api_update_targets({'targets': {'pi': {'enabled': False}, target: {'mode': mode}}})
            self.assertEqual(before, server.HUB_CONFIG.read_bytes())
        server.api_update_targets({'targets': {'codex': {'enabled': True, 'mode': 'third-party'}}})
        self.assertEqual(server.api_status()['targets']['codex']['mode'], 'third-party')
        server.api_update_targets({'targets': {'codex': {'mode': 'official'}}})
        self.assertEqual(server.api_status()['targets']['codex']['mode'], 'official')

    def test_codex_provider_responses_and_env_round_trip(self):
        server.api_add_provider({'id': 'env-gateway', 'name': 'Gateway',
            'baseUrl': 'https://gateway.example/v1', 'apiKeyEnv': 'VENDOR_API_KEY',
            'codexWireApi': 'responses'})
        provider = self.config()['providers']['env-gateway']
        self.assertEqual(provider['apiKeyEnv'], 'VENDOR_API_KEY')
        self.assertEqual(provider['codexWireApi'], 'responses')
        server.api_update_provider('env-gateway', {'openaiApiKey': 'fake-only', 'apiKeyEnv': ''})
        self.assertEqual(self.config()['providers']['env-gateway']['openaiApiKey'], 'fake-only')
        server.api_update_provider('env-gateway', {'api': 'openai-responses', 'codexWireApi': ''})
        self.assertIsNone(self.config()['providers']['env-gateway']['codexWireApi'])
        before = server.HUB_CONFIG.read_bytes()
        for invalid in [{'codexWireApi': 'chat'}, {'apiKeyEnv': 'invalid space'}, {'apiKeyEnv': True}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                server.api_update_provider('env-gateway', invalid)
            self.assertEqual(before, server.HUB_CONFIG.read_bytes())

    def test_codex_effort_uses_codex_vocabulary(self):
        entry = {'provider': 'deepseek', 'model': 'deepseek/model-1', 'reasoningEffort': 'none'}
        result = server.api_update_modelsync({'modelSync': {'overrides': {'codex': entry}}})
        self.assertEqual(result['modelSync']['overrides']['codex']['reasoningEffort'], 'none')
        # Codex 线路放开 max / ultra；其它客户端用的是全局词表（含 off，不含 none）
        for effort in ('max', 'ultra'):
            with self.subTest(effort=effort):
                server.api_update_modelsync({'modelSync': {
                    'overrides': {'codex': {**entry, 'reasoningEffort': effort}}}})
        before = server.HUB_CONFIG.read_bytes()
        for invalid in ['off', 'turbo', 'nope']:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                server.api_update_modelsync({'modelSync': {'overrides': {'codex': {**entry, 'reasoningEffort': invalid}}}})
            self.assertEqual(before, server.HUB_CONFIG.read_bytes())
        # 全局词表相反：接受 off / ultra，拒绝 none
        server.api_update_modelsync({'modelSync': {'default': {**entry, 'reasoningEffort': 'ultra'}}})
        with self.assertRaises(ValueError):
            server.api_update_modelsync({'modelSync': {'default': {**entry, 'reasoningEffort': 'none'}}})

    def test_preview_is_metadata_only_and_does_not_apply(self):
        before = server.HUB_CONFIG.read_bytes()
        plan = {'planId': 'test-plan', 'targets': ['dsh'], 'warnings': [],
                'files': [{'path': '/temporary/client.json', 'action': 'update',
                           'content': 'secret-key-never-render'}], 'internal': 'secret-key-never-render'}
        with patch.object(engine.Hub, 'preview_push', return_value=plan, create=True) as preview, \
             patch.object(engine.Hub, 'apply_preview', create=True) as apply:
            result = server.api_preview({'targets': ['dsh', 'dsh-desktop']})
        preview.assert_called_once_with(only=['dsh'], reconcile_codex=True)
        apply.assert_not_called()
        self.assertNotIn('secret-key', json.dumps(result))
        self.assertEqual(result['files'], [{'path': '/temporary/client.json', 'action': 'update'}])
        self.assertEqual(before, server.HUB_CONFIG.read_bytes())
        self.assertFalse(engine.HUB_STATE.exists())

    def test_apply_requires_plan_and_records_transaction(self):
        for body in ({}, {'planId': ''}, {'planId': 4}):
            with self.assertRaises(ValueError):
                server.api_apply(body)
        with patch.object(engine.Hub, 'apply_preview', return_value={
                'transactionId': 'tx-example', 'results': ['✓ pi: applied']}, create=True) as apply:
            result = server.api_apply({'planId': 'test-plan'})
        apply.assert_called_once_with('test-plan')
        self.assertTrue(result['ok'])
        self.assertEqual(result['details'][0]['target'], 'pi')
        self.assertEqual(server.api_status()['lastTransaction'], 'tx-example')
        self.assertTrue(server.api_status()['pendingSync'])

    def test_stale_plan_does_not_record_success(self):
        with patch.object(engine.Hub, 'apply_preview', side_effect=ValueError('预览已过期，请重新预览'), create=True):
            code, result = self.http('POST', '/api/apply', {'planId': 'stale-plan'})
        self.assertEqual(code, 400)
        self.assertIn('重新预览', result['error'])
        self.assertFalse(engine.HUB_STATE.exists())

    def test_restore_clears_transaction_and_marks_pending(self):
        engine.save_json(engine.HUB_STATE, {'lastTransaction': 'tx-example', 'configHash': engine.file_hash(server.HUB_CONFIG)})
        with patch.object(engine.Hub, 'restore_transaction', return_value={
                'transactionId': 'tx-example', 'restored': 1, 'results': ['restored']}, create=True) as restore:
            result = server.api_restore({'transactionId': 'tx-example'})
        restore.assert_called_once_with('tx-example')
        self.assertTrue(result['ok'])
        self.assertIsNone(server.api_status()['lastTransaction'])
        self.assertTrue(server.api_status()['pendingSync'])

    def test_real_codex_preview_apply_restore_api_round_trip(self):
        cfg = self.config()
        for target in cfg['sync']['targets'].values():
            target['enabled'] = False
        cfg['sync']['targets']['codex'] = {'enabled': True, 'mode': 'third-party',
            'files': ['~/.codex/auth.json']}
        cfg['providers']['deepseek']['codexWireApi'] = 'responses'
        engine.save_json(server.HUB_CONFIG, cfg)
        codex = self.home / '.codex'
        codex.mkdir()
        original = b'model = "gpt-official"\nmodel_reasoning_effort = "high"\n[features]\nunrelated = true\n'
        (codex / 'config.toml').write_bytes(original)
        (codex / 'auth.json').write_bytes(b'{"test":"untouched-auth-sentinel"}')
        preview = server.api_preview({'targets': ['codex']})
        self.assertEqual((codex / 'config.toml').read_bytes(), original)
        self.assertFalse((codex / '.agent-hub-state.json').exists())
        self.assertNotIn('auth.json', json.dumps(preview))
        applied = server.api_apply({'planId': preview['planId']})
        self.assertTrue(applied['ok'])
        self.assertEqual(applied['details'][0]['target'], 'codex')
        self.assertEqual(server.api_status()['lastTransaction'], applied['transactionId'])
        self.assertIn('model_provider', (codex / 'config.toml').read_text())
        restored = server.api_restore({'transactionId': applied['transactionId']})
        self.assertTrue(restored['ok'])
        self.assertEqual((codex / 'config.toml').read_bytes(), original)
        self.assertEqual((codex / 'auth.json').read_bytes(), b'{"test":"untouched-auth-sentinel"}')

    def test_preview_http_and_cross_origin_protection(self):
        with patch.object(engine.Hub, 'preview_push', return_value={
                'planId': 'test-plan', 'targets': ['pi'], 'files': [], 'warnings': []}, create=True):
            code, result = self.http('POST', '/api/preview', {'targets': ['pi']})
        self.assertEqual(code, 200)
        self.assertEqual(result['planId'], 'test-plan')
        for route, body in [('/api/preview', {'targets': ['pi']}), ('/api/apply', {'planId': 'p'}), ('/api/restore', {'transactionId': 't'})]:
            code, _ = self.http('POST', route, body, {'Origin': 'https://untrusted.example'})
            self.assertEqual(code, 403)


if __name__ == '__main__':
    unittest.main()
