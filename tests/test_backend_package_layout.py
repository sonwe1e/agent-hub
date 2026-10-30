"""Compatibility and startup contracts for the feature-oriented backend."""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import engine, isolated
import sync_transaction as transaction


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    ("agent_inventory", "agent_hub.agents.inventory"),
    ("capability_library", "agent_hub.capabilities.library"),
    ("observation", "agent_hub.observability.store"),
    ("session_handoff", "agent_hub.sessions.handoff"),
    ("session_titles", "agent_hub.sessions.titles"),
    ("gateway_titles", "agent_hub.sessions.gateway_titles"),
    ("handoff_summary", "agent_hub.sessions.summary"),
    ("quota_monitor", "agent_hub.quota.monitor"),
    ("subscription_usage", "agent_hub.quota.usage"),
    ("gateway_observation", "agent_hub.gateway.observation"),
    ("upstream_transport", "agent_hub.gateway.transport"),
    ("workbuddy_protocol", "agent_hub.gateway.protocol"),
    ("workspace_services", "agent_hub.workspace.services"),
)


class PackageLayoutTests(unittest.TestCase):
    def run_isolated_python(self, code: str, *arguments: str) -> None:
        # -I prevents a developer's PYTHONPATH from hiding missing dependencies.
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(ROOT), *arguments],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_and_feature_imports_are_identical_in_either_order(self):
        code = """
import importlib, sys
sys.path.insert(0, sys.argv[1])
first, second = sys.argv[2:4]
a = importlib.import_module(first)
b = importlib.import_module(second)
assert a is b, (first, second)
assert sys.modules[first] is sys.modules[second]
"""
        for legacy, feature in MODULES:
            for first, second in ((legacy, feature), (feature, legacy)):
                with self.subTest(first=first, second=second):
                    self.run_isolated_python(code, first, second)

    def test_package_initializers_do_not_load_runtime_or_config(self):
        code = """
import importlib, sys
sys.path.insert(0, sys.argv[1])
packages = ('agent_hub', 'agent_hub.agents', 'agent_hub.capabilities',
            'agent_hub.observability', 'agent_hub.sessions', 'agent_hub.quota',
            'agent_hub.gateway', 'agent_hub.workspace')
for name in packages:
    importlib.import_module(name)
assert 'hub' not in sys.modules
assert 'server' not in sys.modules
assert not any(name.startswith('agent_hub.') and name not in packages
               for name in sys.modules)
"""
        self.run_isolated_python(code)

    def test_legacy_patches_reach_private_functions_and_state(self):
        old_library = importlib.import_module("capability_library")
        new_library = importlib.import_module("agent_hub.capabilities.library")
        old_store = importlib.import_module("observation")
        new_store = importlib.import_module("agent_hub.observability.store")
        marker = object()
        with patch.object(old_library, "_swap_native", marker):
            self.assertIs(new_library._swap_native, marker)
        with patch.object(new_store, "CONTENT_MAX_BYTES", 41):
            self.assertEqual(old_store.CONTENT_MAX_BYTES, 41)

    def test_gateway_preview_rejects_changed_inventory_source(self):
        # Use a temporary source tree, never the user's working files.
        with isolated() as home, tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for name in ("hub.py", "codex_config.py", "gateway_runtime.py", "gateway_server.py"):
                (source / name).write_text("# fixture\n", encoding="utf-8")
            inventory = source / "agent_hub" / "agents" / "inventory.py"
            inventory.parent.mkdir(parents=True)
            inventory.write_text("AGENTS = ('pi',)\n", encoding="utf-8")
            config = json.loads(engine.HUB_CONFIG.read_text(encoding="utf-8"))
            config["gateway"] = {"enabled": False}
            engine.HUB_CONFIG.write_text(json.dumps(config), encoding="utf-8")
            transaction._PLANS.clear()
            try:
                # Compiling is covered separately by the transaction suite;
                # here only the source-change decision is under test.
                with patch.object(engine, "__file__", str(source / "hub.py")), \
                     patch.object(transaction, "_compile", return_value=[]):
                    hub = engine.Hub()
                    preview = transaction.preview(hub, ["pi"])
                    inventory.write_text("AGENTS = ('pi', 'codex')\n", encoding="utf-8")
                    with self.assertRaisesRegex(transaction.TransactionError, "stale"):
                        transaction.apply(hub, preview["planId"])
                    self.assertFalse((home / ".agent-hub" / "backups").exists())
            finally:
                transaction._PLANS.clear()


if __name__ == "__main__":
    unittest.main()
