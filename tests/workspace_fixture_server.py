"""Isolated browser fixture; does not touch real client configs or accounts."""
from http.server import ThreadingHTTPServer
from contextlib import ExitStack
from pathlib import Path
from support import isolated, server
from daily_records import seed
from observation import parse_native
import json
import time

with isolated() as home, ExitStack() as stack:
    source = home / ".agents" / "skills" / "fixture-skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: fixture-skill\n---\nTest skill.\n", encoding="utf-8")
    (source / "references").mkdir()
    (source / "references" / "guide.md").write_text("Fixture guide.\n", encoding="utf-8")
    instructions = home / ".codex" / "AGENTS.md"
    instructions.parent.mkdir(parents=True)
    instructions.write_text("记录 agent 标识为 codex，调用 --agent codex\n", encoding="utf-8")
    inventory = server.Inventory(home)
    inventory.checked_at = time.time()
    stack.enter_context(__import__("unittest.mock", fromlist=["patch"]).patch.object(server, "INVENTORY", inventory))
    store = server.observations()
    for agent, path in seed(home):
        store.ingest(parse_native(agent, path, True, secrets=["fixture-secret-do-not-use"]))
    at = time.time() - 1200
    gateway_session = {'agent': 'pi', 'sessionId': 'fixture-gateway-session', 'cwd': None,
                       'source': str(store.root / 'gateway/config/agent-hub/usage.jsonl')}
    with store.db() as db:
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                   ('gateway-session', 'pi', 'fixture-gateway-session', at, json.dumps(gateway_session)))
        db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',
                   ('gateway-request', 'pi', 'fixture-gateway-session', at,
                    json.dumps({'agent':'pi', 'sessionId':'fixture-gateway-session', 'time':at})))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    print(f"READY {httpd.server_port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
