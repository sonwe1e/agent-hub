"""Private test UI: real Hub account APIs, deterministic local OAuth vendor."""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from support import isolated, server
from test_workbuddy import WorkBuddyTests


case=WorkBuddyTests()
case.setUp()
case.authReady=False
try:
    with isolated() as home,ExitStack() as stack:
        inventory=server.Inventory(home)
        for agent in ('claude','codex','pi','dsh'):
            entry=inventory._entry(agent,home/f'Programs/{agent}.exe',source='npm',kind='cli',version='1.2.3',activePath=True)
            inventory.instances[entry['id']]=entry
        inventory.checked_at=1
        stack.enter_context(patch.object(server,'INVENTORY',inventory))
        stack.enter_context(patch.object(server.gateway(),'start',return_value={'state':'running'}))
        class TestHandler(server.Handler):
            def do_POST(self):
                if self.path=='/fixture/authorize':
                    case.authReady=True
                    return self._send_json(200,{'ok':True})
                super().do_POST()
        httpd=ThreadingHTTPServer(('127.0.0.1',0),TestHandler)
        print(f'READY {httpd.server_port}',flush=True)
        try:httpd.serve_forever()
        finally:
            server.workbuddy_login().cancel()
            httpd.server_close()
finally:case.doCleanups()
