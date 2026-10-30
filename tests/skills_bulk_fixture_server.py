"""Isolated fixture for bulk skill publication; no native user files are read."""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import time

from support import isolated, server


with isolated() as home, ExitStack() as stack:
    library = server.capability_library()
    for name, conflict_agent in (("fixture-alpha", "codex"), ("fixture-beta", "pi")):
        source = home / "fixture-sources" / name
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: isolated browser fixture\n---\nnew {name}\n",
            encoding="utf-8")
        library.import_item({"kind": "skill", "name": name, "source": str(source),
                             "targets": ["codex", "pi"], "mode": "manual"})
        destination = (home / (".codex/skills" if conflict_agent == "codex" else ".pi/agent/skills") / name)
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text(f"old {name}\n", encoding="utf-8")
    instructions = home / "fixture-sources" / "instructions.md"
    instructions.write_text("isolated instruction fixture\n", encoding="utf-8")
    library.import_item({"kind": "instruction", "name": "fixture-instructions",
                         "source": str(instructions), "targets": ["claude"], "mode": "manual"})
    inventory = server.Inventory(home)
    inventory.checked_at = time.time()
    stack.enter_context(patch.object(server, "INVENTORY", inventory))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    print(f"READY {httpd.server_port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
