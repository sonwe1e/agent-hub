"""Browser fixture with temporary ZCode credentials and a simulated model list."""
import json
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from support import isolated, server, engine
import zcode_subscription
import subscription_usage


with isolated() as home, ExitStack() as stack:
    stack.enter_context(patch.object(zcode_subscription,'status',return_value={'signedIn':True,'message':'fixture 已登录'}))
    stack.enter_context(patch.object(zcode_subscription,'read_auth',return_value={'family':'bigmodel','kind':'individual-coding-plan','account':'fixture','key':'fixture-ui-secret'}))
    cfg=engine.Hub().cfg;cfg['sync']['targets']['opencode']={'enabled':True}
    engine.save_json(engine.HUB_CONFIG,cfg)
    inventory=server.Inventory(home)
    for agent in ('claude','codex','pi','dsh','opencode'):
        entry=inventory._entry(agent,home/f'Programs/{agent}.exe',source='npm',kind='cli',version='1.2.3',activePath=True)
        inventory.instances[entry['id']]=entry
    inventory.checked_at=1
    stack.enter_context(patch.object(server,'INVENTORY',inventory))
    stack.enter_context(patch.object(server.gateway(),'start',return_value={'state':'running'}))
    stack.enter_context(patch.object(zcode_subscription,'models',return_value=[{'id':'fixture-model','name':'Fixture Model'}]))
    success={'status':'succeeded','time':1700000001,'agent':'pi','model':'fixture-model'}
    failure={'status':'failed','time':1700000000,'agent':'codex','model':'fixture-model','error':'fixture 请求受限'}
    stack.enter_context(patch.object(server,'api_subscription_activity',return_value={'last':success,'lastSuccess':success,
        'lastError':failure,'collectionEnabled':True}))
    stack.enter_context(patch.object(subscription_usage,'zcode_quota',return_value={'source':'fixture 套餐额度',
        'checkedAt':1700000000,'plan':'Pro','windows':[{'name':'5 小时窗口','usedPercent':60,'used':60,'total':100,'resetsAt':1700003600}]}))
    httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    print(f'READY {httpd.server_port}',flush=True)
    try:httpd.serve_forever()
    finally:httpd.server_close()
