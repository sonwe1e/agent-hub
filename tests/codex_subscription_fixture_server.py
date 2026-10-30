"""Browser fixture with temporary Codex credentials and a simulated model list."""
import json
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from support import isolated, server, engine
import codex_subscription
import subscription_usage


with isolated() as home, ExitStack() as stack:
    codex_home = home/'.codex';codex_home.mkdir()
    (codex_home/'auth.json').write_text(json.dumps({'auth_mode':'chatgpt','tokens':{
        'access_token':'fixture-ui-access-secret','refresh_token':'fixture-ui-refresh-secret',
        'account_id':'fixture-ui-account'}}),encoding='utf-8')
    cfg=engine.Hub().cfg;cfg['sync']['targets']['opencode']={'enabled':True}
    engine.save_json(engine.HUB_CONFIG,cfg)
    inventory=server.Inventory(home)
    for agent in ('claude','codex','pi','dsh','opencode'):
        entry=inventory._entry(agent,home/f'Programs/{agent}.exe',source='npm',kind='cli',version='1.2.3',activePath=True)
        inventory.instances[entry['id']]=entry
    inventory.checked_at=1
    stack.enter_context(patch.object(server,'INVENTORY',inventory))
    stack.enter_context(patch.object(server.gateway(),'start',return_value={'state':'running'}))
    stack.enter_context(patch.object(codex_subscription,'models',return_value=[{'id':'fixture-model','name':'Fixture Model'}]))
    stack.enter_context(patch.object(codex_subscription,'client_version',return_value='1.2.3'))
    stack.enter_context(patch.object(subscription_usage,'codex_quota',return_value={'source':'fixture 账号额度',
        'checkedAt':1700000000,'plan':'plus','windows':[{'name':'主窗口','usedPercent':25,'windowSeconds':18000,'resetsAt':1700003600}]}))
    httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    print(f'READY {httpd.server_port}',flush=True)
    try:httpd.serve_forever()
    finally:httpd.server_close()
