"""Replay a real-call observation index in an isolated Hub for browser QA."""
import json
import os
import sqlite3
import socket
from pathlib import Path
from http.server import ThreadingHTTPServer
from support import isolated, engine, server

report = Path(os.environ['LIVE_OBSERVATION_REPORT']).resolve()
evidence = json.loads((report/'observation.json').read_text(encoding='utf-8'))
with isolated() as home:
    if any(r.get('sourceType','').startswith('gateway') for r in evidence.get('requests',[])):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        h=server.hub();h.cfg['gateway']={'enabled':False,'port':port};engine.save_json(engine.HUB_CONFIG,h.cfg)
        # Exercise the real runtime and transactions without sending fixture
        # credentials to upstream providers; this instance exports no providers.
        server.gateway().config=lambda:{**server.hub().cfg,'providers':{}}
    store = server.observations()
    with sqlite3.connect(report/'data/observability.sqlite3') as source, store.db() as dest:
        source.backup(dest)
    store.settings_path.write_text(json.dumps({'enabled':False,'content':True,'retentionDays':30,'clearedBefore':0}), encoding="utf-8")
    store.progress.update(evidence['status']['collector'])
    # The observation pages use the recorded real data; no model sync, vendor
    # updater, or native client is invoked by this read-only review server.
    server.INVENTORY = server.Inventory(home)
    server.INVENTORY.checked_at = 1
    httpd = ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    print(f'READY {httpd.server_port}',flush=True)
    try:
        httpd.serve_forever()
    finally:
        for runtime in server._GATEWAYS.values():runtime.close()
        store.close()
        httpd.server_close()
