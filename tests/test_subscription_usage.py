"""Read-only subscription quota and status evidence."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock,patch
from support import isolated,server
import codex_subscription as codex
import zcode_subscription as zcode
import subscription_usage as usage
from gateway_server import packed
from test_proxy_transport import PROXY_ENV
import os


class SubscriptionUsageTests(unittest.TestCase):
    def test_codex_quota_whitelists_windows_and_omits_identity(self):
        source={'plan_type':'plus','account_id':'do-not-expose','email':'private@example.invalid',
                'rate_limit':{'primary_window':{'used_percent':35,'limit_window_seconds':18000,'reset_at':1700000000},
                              'secondary_window':None}}
        with patch.object(usage,'fetch_json',return_value=source) as request:
            result=usage.codex_quota({'access':'fixture-access','account':'fixture-account'},'0.164.0')
        self.assertEqual(result['windows'][0]['usedPercent'],35)
        self.assertEqual(result['windows'][0]['windowSeconds'],18000)
        self.assertNotIn('do-not-expose',json.dumps(result));self.assertNotIn('fixture-access',json.dumps(result))
        self.assertEqual(request.call_args.args[1]['ChatGPT-Account-Id'],'fixture-account')

    def test_zcode_quota_respects_unknown_and_uncapped_values(self):
        data={'code':200,'data':{'level':'Pro','limits':[{'type':'TOKENS_LIMIT','unit':3,'number':5,
              'usage':1000,'remaining':300,'percentage':99,'nextResetTime':1700000000000},
              {'type':'TIME_LIMIT','unit':5,'number':1,'usage':0,'percentage':100},
              {'type':'TOKENS_LIMIT','unit':6,'number':1,'percentage':22}]}}
        with patch.object(usage,'fetch_json',return_value=data):
            result=usage.zcode_quota({'family':'bigmodel','base':'https://open.bigmodel.cn/api/anthropic','key':'fixture-key'})
        self.assertEqual(result['windows'][0]['usedPercent'],70)
        self.assertEqual(result['windows'][0]['resetsAt'],1700000000)
        self.assertIsNone(result['windows'][1]['usedPercent'])
        self.assertEqual(result['windows'][2]['name'],'每周窗口')
        self.assertNotIn('fixture-key',json.dumps(result))

    def test_start_quota_only_counts_active_matching_unexpired_buckets(self):
        balance={'server_time':1000,'plans':[{'user_plan_id':'p1','status':'active','ends_at':2000}],
                 'balances':[{'user_plan_id':'p1','show_name':'Daily','total_units':'100','remaining_units':'20','expires_at':'1500',
                              'capabilities':['model:GLM-test']},
                             {'show_name':'unmatched','total_units':20,'remaining_units':10,'capabilities':['model:other']}]}
        with patch.object(zcode,'start_balance',return_value=balance):
            result=usage.zcode_quota({'kind':'start-plan'})
        self.assertEqual(len(result['windows']),1);self.assertEqual(result['windows'][0]['usedPercent'],80)
        self.assertEqual(result['availableModels'],['GLM-test'])

    def test_http_failures_never_reveal_vendor_secret(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                wire=packed({'error':'fixture-private-token'});self.send_response(401)
                self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
        vendor=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=vendor.serve_forever,daemon=True).start()
        try:
            with patch.dict(os.environ,{**PROXY_ENV,'NO_PROXY':'127.0.0.1'}):
                with self.assertRaises(usage.UsageError) as failure:usage.fetch_json(f'http://127.0.0.1:{vendor.server_port}/',{})
            self.assertIn('401',str(failure.exception));self.assertNotIn('fixture-private-token',str(failure.exception))
        finally:vendor.shutdown();vendor.server_close()

    def test_quota_account_switch_during_request_is_rejected(self):
        first={'access':'token-a','account':'account-a'};second={'access':'token-b','account':'account-b'}
        with patch.object(codex,'read_auth',side_effect=[first,second]),patch.object(codex,'client_version',return_value='1.2.3'), \
             patch.object(usage,'codex_quota',return_value={'windows':[]}):
            with self.assertRaisesRegex(ValueError,'账号已切换'):server.api_codex_subscription_quota()

    def test_activity_returns_only_indexed_provider_evidence(self):
        with isolated():
            store=server.observations()
            rows=[{'provider':'codex-chatgpt','sourceType':'gateway','status':'succeeded','time':10,'agent':'pi','model':'model-a','httpStatus':200,'error':None},
                  {'provider':'codex-chatgpt','sourceType':'gateway','status':'failed','time':11,'agent':'pi','model':'model-a','httpStatus':429,'error':'quota'},
                  {'provider':'zcode-subscription','sourceType':'gateway','status':'succeeded','time':12,'agent':'codex','model':'model-b','httpStatus':200,'error':None}]
            with store.db() as db:
                for i,row in enumerate(rows):db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(str(i),row['agent'],'fixture',row['time'],json.dumps(row)))
            result=store.subscription_activity('codex-chatgpt')
            self.assertEqual(result['last']['status'],'failed');self.assertEqual(result['lastSuccess']['time'],10)
            self.assertEqual(result['lastError']['httpStatus'],429);self.assertNotIn('model-b',json.dumps(result))
            with self.assertRaises(ValueError):store.subscription_activity('other')


if __name__=='__main__':unittest.main()
