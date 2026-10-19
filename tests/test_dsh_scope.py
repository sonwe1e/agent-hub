"""Dsh routes must never expand an empty Hub library to a built-in catalog."""
import copy
import json
import unittest

from support import isolated, engine, server
from test_modelsync import dsh_fixture


class DshScopeTests(unittest.TestCase):
    def setUp(self):
        context = isolated()
        self.home = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        dsh_fixture(self.home)
        self.dsh = self.home / '.dsh'
        self.patch = self.dsh / 'profiles' / 'desktop' / 'cordis.patch.yml'
        self.cfg = json.loads(server.HUB_CONFIG.read_text(encoding='utf-8'))
        # Seed all layers with an older, populated OpenRouter route and a route
        # outside Hub ownership, just as a user can have before clearing models.
        existing = {
            'openrouter': {'apiKeyEnv': 'MYOPENROUTER_API_KEY',
                           'models': [{'id': 'previous-model'}]},
            'local': {'models': [{'id': 'manual'}]},
        }
        for name in ('settings.yaml', 'settings.yaml.imported'):
            (self.dsh / name).write_text(
                engine.dump_yaml({'llm-pi-ai': {'providers': existing}, 'locale': 'zh'}) + '\n',
                encoding='utf-8')
        items = engine.parse_yaml(self.patch.read_text(encoding='utf-8'))
        next(b for b in items if b['id'] == 'llm-pi-ai')['config']['providers'].update(
            copy.deepcopy(existing))
        self.patch.write_text(engine.dump_yaml(items) + '\n', encoding='utf-8')
        self.credentials = self.dsh / '.credentials.yaml'
        self.credentials.write_text(
            'version: 1\nrefs:\n  KEEP_ME: keep-value\n'
            '  MYOPENROUTER_API_KEY: stored-openrouter-key\n', encoding='utf-8')

    def save(self):
        engine.save_json(server.HUB_CONFIG, self.cfg)

    def test_empty_inline_credentials_refs_expand_once_and_preserve_records(self):
        for heading in ('refs: {}','refs: null','refs:'):
            with self.subTest(heading=heading):
                source='version: 1\n'+heading+'\nrecords:\n  retained:\n    kind: api-key\n'
                self.credentials.write_text(source,encoding='utf-8')
                self.save()
                h=engine.Hub();plan=h.preview_push(['dsh']);h.apply_preview(plan['planId'])
                rendered=self.credentials.read_text(encoding='utf-8')
                self.assertEqual(sum(line.startswith('refs:') for line in rendered.splitlines()),1)
                parsed=engine.parse_yaml(rendered)
                self.assertEqual(parsed['refs']['MYOPENROUTER_API_KEY'],'test-not-a-real-key')
                self.assertEqual(parsed['records']['retained']['kind'],'api-key')

    def provider_layers(self):
        layers = []
        for name in ('settings.yaml', 'settings.yaml.imported'):
            data = engine.parse_yaml((self.dsh / name).read_text(encoding='utf-8'))
            self.assertEqual(data['locale'], 'zh')
            layers.append(data['llm-pi-ai']['providers'])
        items = engine.parse_yaml(self.patch.read_text(encoding='utf-8'))
        layers.append(next(b for b in items if b['id'] == 'llm-pi-ai')['config']['providers'])
        return layers

    def native_bytes(self):
        return {name: (self.dsh / name).read_bytes() for name in (
            '.credentials.yaml', 'settings.yaml', 'settings.yaml.imported',
            'profiles/desktop/cordis.patch.yml')}

    def test_empty_openrouter_is_removed_from_every_layer_transactionally(self):
        self.cfg['providers']['openrouter']['models'] = []
        self.save()
        before = self.native_bytes()
        hub = engine.Hub()
        plan = hub.preview_push(['dsh', 'dsh-desktop'])
        self.assertEqual(self.native_bytes(), before)
        self.assertEqual(plan['targets'], ['dsh'])
        hub.apply_preview(plan['planId'])
        for layer in self.provider_layers():
            self.assertNotIn('openrouter', layer)
            self.assertEqual(layer['local'], {'models': [{'id': 'manual'}]})
            self.assertTrue(layer['deepseek']['models'])
        self.assertIn('MYOPENROUTER_API_KEY: stored-openrouter-key',
                      self.credentials.read_text(encoding='utf-8'))

    def test_deleted_provider_route_is_removed_from_every_layer(self):
        # 真实删除 = providers 里移除 + removedProviders 留档；三层配置里的
        # 残留路由（含 baseURL / apiKeyEnv 引用）必须在下次同步时清掉。
        del self.cfg['providers']['firework']
        self.cfg.setdefault('removedProviders', {})['firework'] = {
            'reason': 'manual remove', 'removedAt': '2026-10-09'}
        stale = {'firework': {'apiKeyEnv': 'FIREWORK_API_KEY',
                              'baseURL': 'https://old.example/v1',
                              'models': [{'id': 'glm-x'}]}}
        for name in ('settings.yaml', 'settings.yaml.imported'):
            data = engine.parse_yaml((self.dsh / name).read_text(encoding='utf-8'))
            data['llm-pi-ai']['providers'].update(copy.deepcopy(stale))
            (self.dsh / name).write_text(engine.dump_yaml(data) + '\n', encoding='utf-8')
        items = engine.parse_yaml(self.patch.read_text(encoding='utf-8'))
        next(b for b in items if b['id'] == 'llm-pi-ai')['config']['providers'].update(
            copy.deepcopy(stale))
        self.patch.write_text(engine.dump_yaml(items) + '\n', encoding='utf-8')
        self.credentials.write_text(
            'version: 1\nrefs:\n  KEEP_ME: keep-value\n'
            '  FIREWORK_API_KEY: old-firework-key\n', encoding='utf-8')

        self.save()
        engine.Hub().push_dsh()
        for layer in self.provider_layers():
            self.assertNotIn('firework', layer)
            self.assertIn('deepseek', layer)
            self.assertEqual(layer['local'], {'models': [{'id': 'manual'}]})
        # 已删供应商的已存凭据按 dsh 约定保留：不主动删除用户的密钥条目
        self.assertIn('FIREWORK_API_KEY: old-firework-key',
                      self.credentials.read_text(encoding='utf-8'))

    def test_explicit_empty_dsh_override_does_not_inherit_default_library(self):
        self.cfg['providers']['openrouter']['agents']['dsh'] = {'models': []}
        self.save()
        engine.Hub().push_dsh()
        for layer in self.provider_layers():
            self.assertNotIn('openrouter', layer)

    def test_adding_dsh_models_restores_only_selected_models(self):
        provider = self.cfg['providers']['openrouter']
        provider['models'] = []
        self.save()
        engine.Hub().push_dsh()
        provider['agents']['dsh'] = {'models': [{'id': 'vendor/selected'}]}
        self.cfg['modelSync']['overrides'] = {
            'dsh': {'provider': 'openrouter', 'model': 'vendor/selected'}}
        self.save()
        engine.Hub().push_dsh()
        for layer in self.provider_layers():
            self.assertEqual([m['id'] for m in layer['openrouter']['models']],
                             ['vendor/selected'])
        items = engine.parse_yaml(self.patch.read_text(encoding='utf-8'))
        default = next(b for b in items if b['id'] == 'agent-default-model')['config']
        self.assertEqual((default['provider'], default['model']),
                         ('openrouter', 'vendor/selected'))
        before = self.native_bytes()
        engine.Hub().push_dsh()
        self.assertEqual(self.native_bytes(), before)

    def test_all_hub_libraries_empty_keeps_unmanaged_routes_and_credentials(self):
        self.cfg.pop('modelSync')
        for provider in self.cfg['providers'].values():
            provider['models'] = []
        self.save()
        credentials = self.credentials.read_bytes()
        engine.Hub().push_dsh()
        for layer in self.provider_layers():
            self.assertEqual(layer, {'local': {'models': [{'id': 'manual'}]}})
        self.assertEqual(self.credentials.read_bytes(), credentials)
        items = engine.parse_yaml(self.patch.read_text(encoding='utf-8'))
        default = next(b for b in items if b['id'] == 'agent-default-model')['config']
        self.assertEqual(default['provider'], 'deepseek-account')

    def test_empty_selected_route_is_rejected_before_any_native_write(self):
        self.cfg['providers']['openrouter']['models'] = []
        self.cfg['modelSync']['default'] = {
            'provider': 'openrouter', 'model': 'previous-model'}
        self.save()
        before = self.native_bytes()
        with self.assertRaises(ValueError):
            engine.Hub().push_dsh()
        self.assertEqual(self.native_bytes(), before)


if __name__ == '__main__':
    unittest.main()
