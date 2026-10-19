"""Reviewable real UI with isolated native files and deterministic installations.

No live client configuration, updater or vendor endpoint is used by this server.
"""
import threading
import json
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from support import isolated, engine, server
from fixture_server_helpers import preview, apply, restore
from daily_records import seed, LONG_MODEL
from observation import parse_native


with isolated() as home, ExitStack() as stack:
    cfg=json.loads(engine.HUB_CONFIG.read_text(encoding='utf-8'))
    cfg['providers']['deepseek']['models'].append({'id':LONG_MODEL,'name':LONG_MODEL,'contextWindow':128000})
    engine.HUB_CONFIG.write_text(json.dumps(cfg),encoding='utf-8')
    for name,fn in [('preview_push',preview),('apply_preview',apply),('restore_transaction',restore)]:
        stack.enter_context(patch.object(engine.Hub,name,fn))
    inventory=server.Inventory(home)
    for agent,source,kind in [('claude','claude-native','cli'),('codex','npm','cli'),('pi','npm','cli'),('dsh','desktop-bundled','cli'),('codex','msix','desktop'),('dsh','desktop-updater','desktop')]:
        e=inventory._entry(agent,home/f'Programs/{agent}-{kind}.exe',source=source,kind=kind,version='1.2.3',activePath=kind=='cli',running=agent=='dsh')
        inventory.instances[e['id']]=e
    inventory.checked_at=1
    stack.enter_context(patch.object(server,'INVENTORY',inventory))
    store=server.observations()
    store.settings_path.write_text(json.dumps({'enabled':False,'content':True,'retentionDays':30,'clearedBefore':0}))
    for agent,path in seed(home):store.ingest(parse_native(agent,path,True,secrets=['fixture-secret-do-not-use']))
    store.progress.update(state='complete',finishedAt=__import__('time').time(),sources=[{'agent':a,'exists':True,'files':1} for a in ('claude','codex','pi','dsh')])
    httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    print(f'READY {httpd.server_port}',flush=True)
    try:httpd.serve_forever()
    finally:httpd.server_close()
