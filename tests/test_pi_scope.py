"""Synthetic Pi scope regression tests; never read real credentials or call providers."""
import json
import unittest

from support import isolated, engine, server


class PiScopeTests(unittest.TestCase):
    def setUp(self):
        context = isolated()
        self.home = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.directory = self.home / '.pi' / 'agent'
        self.directory.mkdir(parents=True)
        self.cfg = json.loads(server.HUB_CONFIG.read_text(encoding="utf-8"))
        self.cfg['providers'] = {'openrouter': self.cfg['providers']['openrouter']}
        self.cfg['providers']['openrouter']['models'] = [{'id': 'openrouter/spacebunny:free'}]
        self.cfg['modelSync'] = {'default': {'provider': 'openrouter', 'model': 'openrouter/spacebunny:free'}}
        self.save()

    def save(self):
        engine.save_json(server.HUB_CONFIG, self.cfg)

    def write(self, filename, data):
        engine.save_json(self.directory / filename, data)

    def read(self, filename='settings.json'):
        return json.loads((self.directory / filename).read_text(encoding="utf-8"))

    def push(self):
        warnings = []
        engine.Hub().push_pi(warnings=warnings)
        return warnings

    def test_openrouter_single_model_gets_exact_scope_not_builtin_catalog(self):
        self.push()
        self.assertEqual(self.read()['enabledModels'], ['openrouter/openrouter/spacebunny:free'])
        self.assertEqual(len(self.read('models.json')['providers']['openrouter']['models']), 1)

    def test_scope_is_entire_connection_library_not_only_active_model(self):
        self.cfg['providers']['openrouter']['models'].append({'id': 'vendor/second'})
        self.save()
        self.push()
        self.assertEqual(self.read()['enabledModels'], ['openrouter/openrouter/spacebunny:free', 'openrouter/vendor/second'])

    def test_pi_override_library_and_active_model_first(self):
        self.cfg['providers']['openrouter']['agents']['pi'] = {'models': [{'id': 'other'}, {'id': 'only-pi'}]}
        self.cfg['modelSync'] = {'overrides': {'pi': {'provider': 'openrouter', 'model': 'only-pi'}}}
        self.save()
        self.push()
        self.assertEqual(self.read()['enabledModels'], ['openrouter/only-pi', 'openrouter/other'])

    def test_unrelated_settings_provider_and_auth_are_preserved(self):
        self.write('settings.json', {'theme': 'dark', 'enabledModels': ['openrouter/*', 'local/manual']})
        self.write('models.json', {'providers': {'local': {'models': [{'id': 'manual'}], 'custom': 1}}})
        self.write('auth.json', {'local': {'type': 'oauth', 'token': 'synthetic-token'}})
        self.push()
        self.assertEqual(self.read()['theme'], 'dark')
        self.assertEqual(self.read()['enabledModels'], ['openrouter/openrouter/spacebunny:free', 'local/manual'])
        self.assertEqual(self.read('models.json')['providers']['local'], {'models': [{'id': 'manual'}], 'custom': 1})
        self.assertEqual(self.read('auth.json')['local'], {'type': 'oauth', 'token': 'synthetic-token'})

    def test_global_patterns_are_preserved_with_sanitized_warning(self):
        self.write('settings.json', {'enabledModels': ['*space*', 'claude', 'local/*']})
        warnings = self.push()
        self.assertEqual(self.read()['enabledModels'][1:], ['*space*', 'claude', 'local/*'])
        self.assertEqual(len(warnings), 1)
        self.assertNotIn('*space*', warnings[0])

    def test_user_default_without_modelsync_remains_in_scope_and_first(self):
        self.cfg.pop('modelSync')
        self.save()
        self.write('settings.json', {'defaultProvider': 'local', 'defaultModel': 'manual'})
        self.push()
        self.assertEqual(self.read()['enabledModels'], ['local/manual', 'openrouter/openrouter/spacebunny:free'])
        self.assertEqual(self.read()['defaultProvider'], 'local')

    def test_repeated_push_and_library_replacement_are_idempotent(self):
        self.push()
        first = self.read()
        self.push()
        self.assertEqual(self.read(), first)
        self.cfg['providers']['openrouter']['models'] = [{'id': 'replacement'}]
        self.cfg['modelSync'] = {'default': {'provider': 'openrouter', 'model': 'replacement'}}
        self.save()
        self.push()
        self.assertEqual(self.read()['enabledModels'], ['openrouter/replacement'])

    def test_disabled_provider_scope_is_removed_without_removing_credentials(self):
        self.push()
        self.write('settings.json', {'enabledModels': ['openrouter/*', 'local/manual']})
        self.cfg['providers']['openrouter']['enabled'] = False
        self.save()
        self.push()
        self.assertEqual(self.read()['enabledModels'], ['local/manual'])
        self.assertIn('openrouter', self.read('auth.json'))

    def test_empty_result_is_not_silently_changed_to_all(self):
        self.push()
        previous = {f: (self.directory / f).read_bytes() for f in ['settings.json', 'models.json', 'auth.json']}
        self.cfg['providers']['openrouter']['enabled'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, '空数组'):
            self.push()
        for filename, data in previous.items():
            self.assertEqual((self.directory / filename).read_bytes(), data)

    def test_glob_model_ids_fail_closed_before_any_client_write(self):
        for model_id in ['vendor/*', 'vendor/q?', 'vendor/back\\slash']:
            with self.subTest(model_id=model_id):
                self.cfg['providers']['openrouter']['models'] = [{'id': model_id}]
                self.save()
                with self.assertRaisesRegex(ValueError, '通配符'):
                    self.push()
                self.assertFalse((self.directory / 'models.json').exists())

    def test_real_spacebunny_bracket_id_remains_literal(self):
        model_id = 'anthropic/stealth/space-bunny-alpha[1m]'
        self.cfg['providers']['openrouter']['models'] = [{'id': model_id}]
        self.cfg['modelSync'] = {'default': {'provider': 'openrouter', 'model': model_id}}
        self.save()
        self.push()
        self.assertEqual(self.read()['enabledModels'], ['openrouter/' + model_id])
        self.push()
        self.assertEqual(self.read()['enabledModels'], ['openrouter/' + model_id])

    def test_bracket_free_suffix_and_plain_high_are_literal(self):
        for model_id in ['vendor/model[1m]:free', 'vendor/model:high']:
            with self.subTest(model_id=model_id):
                self.assertEqual(engine._pi_model_reference('openrouter', model_id), 'openrouter/' + model_id)

    def test_bracket_thinking_suffix_is_rejected_without_client_writes(self):
        self.cfg['providers']['openrouter']['models'] = [{'id': 'vendor/model[1m]:high'}]
        self.save()
        with self.assertRaisesRegex(ValueError, '歧义'):
            self.push()
        self.assertFalse((self.directory / 'models.json').exists())

    def test_unmanaged_bracket_default_uses_same_literal_validation(self):
        self.cfg.pop('modelSync')
        self.save()
        model_id = 'anthropic/stealth/space-bunny-alpha[1m]'
        self.write('settings.json', {'defaultProvider': 'local', 'defaultModel': model_id})
        self.push()
        self.assertEqual(self.read()['enabledModels'][0], 'local/' + model_id)
        original = {'defaultProvider': 'local', 'defaultModel': 'vendor/model[1m]:high'}
        self.write('settings.json', original)
        with self.assertRaisesRegex(ValueError, '歧义'):
            self.push()
        self.assertEqual(self.read(), original)

    def test_no_managed_pi_provider_leaves_user_scope_alone(self):
        self.cfg['providers'] = {}
        self.save()
        original = {'defaultProvider': 'local', 'defaultModel': 'manual'}
        self.write('settings.json', original)
        self.push()
        self.assertEqual(self.read(), original)

    def test_empty_new_library_does_not_enable_builtin_catalog(self):
        self.cfg['providers']['openrouter']['models'] = []
        self.save()
        with self.assertRaisesRegex(ValueError, '空数组'):
            self.push()
        self.assertFalse((self.directory / 'settings.json').exists())

    def test_invalid_user_scope_is_not_overwritten(self):
        self.write('settings.json', {'enabledModels': 'local/*'})
        with self.assertRaisesRegex(ValueError, '字符串数组'):
            self.push()
        self.assertEqual(self.read(), {'enabledModels': 'local/*'})

    def test_same_model_ids_on_different_providers_stay_qualified(self):
        self.cfg['providers']['local'] = dict(self.cfg['providers']['openrouter'])
        self.save()
        self.push()
        self.assertEqual(self.read()['enabledModels'], [
            'openrouter/openrouter/spacebunny:free', 'local/openrouter/spacebunny:free'])

    def test_preview_surfaces_warning_and_does_not_write_client(self):
        self.write('settings.json', {'enabledModels': ['*']})
        before = (self.directory / 'settings.json').read_bytes()
        plan = engine.Hub().preview_push(['pi'])
        self.assertTrue(any('pi' in warning and '范围' in warning for warning in plan['warnings']))
        self.assertEqual((self.directory / 'settings.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
