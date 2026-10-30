"""All native-client paths here are temporary test fixtures, never the real home."""
from __future__ import annotations

import tempfile
import unittest
import subprocess
import sys
import os
import ctypes
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from capability_library import CapabilityLibrary, LibraryError


@contextmanager
def windows_writer(path):
    """Real share-delete write handle; no time-based synchronization."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle; close.argtypes = [wintypes.HANDLE]; close.restype = wintypes.BOOL
    handle = create(str(path), 0x40000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try: yield handle
    finally: close(handle)


class CapabilityLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.root = self.base / "data"
        self.lib = CapabilityLibrary(self.root, self.home)

    def skill(self, name="work", folder=None):
        path = folder or self.base / "sources" / name
        (path / "references").mkdir(parents=True)
        (path / "scripts").mkdir()
        (path / "SKILL.md").write_text("---\nname: work\ndescription: Work\n---\nBody\n", encoding="utf-8")
        (path / "references" / "guide.md").write_text("v1", encoding="utf-8")
        (path / "scripts" / "run.py").write_text("print('ok')", encoding="utf-8")
        return path

    def add_skill(self, path, targets=("codex", "pi"), **kwargs):
        return self.lib.import_item({"kind": "skill", "name": "work", "source": str(path),
                                     "targets": list(targets), **kwargs})

    def publish(self, item, body=None):
        plan = self.lib.preview(item["id"], body)
        return self.lib.apply(plan["planId"])

    def test_explicit_import_preview_then_entire_skill_publish_and_restore(self):
        source = self.skill()
        item = self.add_skill(source)
        target = self.home / ".codex/skills/work"
        self.assertFalse(target.exists())
        plan = self.lib.preview(item["id"])
        self.assertFalse(target.exists())
        self.assertEqual({r["status"] for r in plan["results"]}, {"create"})
        result = self.lib.apply(plan["planId"])
        self.assertEqual((target / "references/guide.md").read_text(), "v1")
        self.assertTrue((self.home / ".pi/agent/skills/work/scripts/run.py").is_file())
        fresh = CapabilityLibrary(self.root, self.home)
        self.assertEqual(fresh.history(item["id"])["transactions"][0]["id"], result["transactionId"])
        fresh.restore(result["transactionId"])
        self.assertFalse(target.exists())
        self.assertTrue(source.exists())

    def test_external_target_conflict_requires_explicit_preview(self):
        source = self.skill()
        target = self.home / ".codex/skills/work"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("unmanaged", encoding="utf-8")
        item = self.add_skill(source, ("codex",))
        plan = self.lib.preview(item["id"])
        self.assertTrue(plan["hasConflicts"])
        with self.assertRaises(LibraryError): self.lib.apply(plan["planId"])
        self.assertEqual((target / "SKILL.md").read_text(), "unmanaged")
        forced = self.lib.preview(item["id"], {"forceTargets": ["codex"]})
        self.assertTrue(forced["results"][0]["forced"])
        result = self.lib.apply(forced["planId"])
        self.lib.restore(result["transactionId"])
        self.assertEqual((target / "SKILL.md").read_text(), "unmanaged")

    def test_source_and_destination_changes_after_preview_stop_write(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        plan = self.lib.preview(item["id"])
        (source / "references/guide.md").write_text("v2")
        with self.assertRaisesRegex(LibraryError, "主来源已变更"): self.lib.apply(plan["planId"])
        plan = self.lib.preview(item["id"])
        target = self.home / ".codex/skills/work"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("someone else")
        with self.assertRaisesRegex(LibraryError, "目标在预览后发生变化"): self.lib.apply(plan["planId"])
        self.assertEqual((target / "SKILL.md").read_text(), "someone else")

    def test_same_source_is_never_overwritten(self):
        source = self.skill(folder=self.home / ".codex/skills/work")
        item = self.add_skill(source, ("codex", "pi"))
        before = (source / "SKILL.md").read_bytes()
        plan = self.lib.preview(item["id"])
        self.assertEqual(plan["results"][0]["status"], "source")
        self.lib.apply(plan["planId"])
        self.assertEqual((source / "SKILL.md").read_bytes(), before)

    def test_manual_published_snapshot_is_available_for_new_target(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self.publish(item)
        (source / "references/guide.md").write_text("unpublished edit")
        self.lib.update_item(item["id"], {"targets": ["codex", "pi"]})
        self.publish(item, {"published": True})
        self.assertEqual((self.home / ".pi/agent/skills/work/references/guide.md").read_text(), "v1")
        self.assertEqual(self.lib.list_items()["items"][0]["sourceStatus"], "changed")

    def test_instruction_preserves_unmanaged_text_and_detects_edits(self):
        source = self.base / "source.md"; source.write_text("Shared instruction\n", encoding="utf-8")
        target = self.home / ".claude/CLAUDE.md"; target.parent.mkdir(parents=True)
        target.write_text("User rule\n", encoding="utf-8")
        item = self.lib.import_item({"kind": "instruction", "name": "my-rule", "source": str(source), "targets": ["claude"]})
        self.publish(item)
        original = target.read_text(encoding="utf-8")
        self.assertTrue(original.startswith("User rule\n"))
        self.assertIn("Shared instruction", original)
        target.write_text(original.replace("User rule", "User updated rule"), encoding="utf-8")
        source.write_text("Updated shared instruction", encoding="utf-8")
        plan = self.lib.preview(item["id"])
        self.assertFalse(plan["hasConflicts"])
        result = self.lib.apply(plan["planId"])
        self.assertIn("User updated rule", target.read_text(encoding="utf-8"))
        target.write_text(target.read_text(encoding="utf-8").replace("Updated shared", "Changed locally"), encoding="utf-8")
        self.assertTrue(self.lib.preview(item["id"])["hasConflicts"])
        with self.assertRaisesRegex(LibraryError, "目标在发布后被修改"): self.lib.restore(result["transactionId"])

    def test_auto_sync_skips_conflicted_target_but_updates_other(self):
        source = self.skill(); item = self.add_skill(source, mode="auto")
        self.publish(item)
        codex = self.home / ".codex/skills/work/references/guide.md"
        pi = self.home / ".pi/agent/skills/work/references/guide.md"
        codex.write_text("local edit")
        (source / "references/guide.md").write_text("v2")
        result = self.lib.sync_auto()["results"][0]
        self.assertTrue(result["partial"])
        self.assertEqual(codex.read_text(), "local edit")
        self.assertEqual(pi.read_text(), "v2")
        self.assertEqual(self.lib.list_items()["items"][0]["targetStatus"]["codex"], "conflict")
        again = self.lib.sync_auto()["results"][0]
        self.assertFalse(again["ok"])

    def test_stop_managing_does_not_remove_any_user_files(self):
        source = self.skill(); item = self.add_skill(source, ("codex",), mode="auto")
        self.publish(item)
        target = self.home / ".codex/skills/work/SKILL.md"
        self.lib.remove_item(item["id"])
        self.assertTrue(target.exists())
        self.assertTrue((source / "SKILL.md").exists())
        self.assertEqual(self.lib.list_items()["items"], [])
        self.assertEqual(self.lib.sync_auto()["results"], [])

    def test_only_latest_transaction_can_be_restored(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        first = self.publish(item)
        self.lib.update_item(item["id"], {"targets": ["codex", "pi"]})
        second = self.publish(item, {"published": True})
        with self.assertRaisesRegex(LibraryError, "后续发布"): self.lib.restore(first["transactionId"])
        self.lib.restore(second["transactionId"])
        self.lib.restore(first["transactionId"])

    def test_mid_transaction_failure_rolls_back_prior_target(self):
        source = self.skill(); item = self.add_skill(source)
        plan = self.lib.preview(item["id"])
        import capability_library
        real = capability_library._swap_native
        def fail_pi(op, kind):
            if op["agent"] == "pi":
                raise OSError("fixture failure")
            return real(op, kind)
        with patch.object(capability_library, "_swap_native", side_effect=fail_pi):
            with self.assertRaises(LibraryError): self.lib.apply(plan["planId"])
        self.assertFalse((self.home / ".codex/skills/work").exists())
        self.assertFalse((self.home / ".pi/agent/skills/work").exists())
        self.assertIsNone(self.lib.list_items()["items"][0]["published"])

    def test_discover_is_read_only_and_lists_shared_roots(self):
        source = self.skill(folder=self.home / ".agents/skills/work")
        rows = self.lib.discover()["candidates"]
        match = next(row for row in rows if row["source"] == str(source))
        self.assertEqual(set(match["foundBy"]), {"codex", "pi", "dsh", "opencode"})
        self.assertFalse(self.root.exists())

    def test_nested_source_and_target_are_rejected(self):
        source = self.skill(folder=self.home)
        item = self.add_skill(source, ("codex",))
        with self.assertRaisesRegex(LibraryError, "互相包含"):
            self.lib.preview(item["id"])
        self.assertFalse((self.home / ".codex/skills/work").exists())

    def test_auto_sync_can_be_restricted_to_stable_items(self):
        source = self.skill(); item = self.add_skill(source, ("codex",), mode="auto")
        state = self.lib.list_items()["items"][0]
        self.assertEqual(len(state["sourceHash"]), 64)
        self.assertEqual(self.lib.sync_auto([])["results"], [])
        self.assertFalse((self.home / ".codex/skills/work").exists())
        pending = self.lib.sync_auto([item["id"]], {item["id"]: state["sourceHash"]})["results"][0]
        self.assertFalse(pending["ok"])
        self.assertIn("首次发布", pending["error"])
        self.assertFalse((self.home / ".codex/skills/work").exists())
        self.publish(item)
        (source / "references/guide.md").write_text("changed during observation")
        stale = self.lib.sync_auto([item["id"]], {item["id"]: state["sourceHash"]})["results"][0]
        self.assertFalse(stale["ok"])
        target = self.home / ".codex/skills/work/references/guide.md"
        self.assertEqual(target.read_text(), "v1")
        self.assertTrue(self.lib.sync_auto([item["id"]])["results"][0]["ok"])
        self.assertEqual(target.read_text(), "changed during observation")

    def test_source_global_file_does_not_recursively_copy_managed_blocks(self):
        one = self.base / "one.md"; one.write_text("first shared", encoding="utf-8")
        codex = self.home / ".codex/AGENTS.md"; codex.parent.mkdir(parents=True)
        codex.write_text("my original text", encoding="utf-8")
        first = self.lib.import_item({"kind": "instruction", "name": "one", "source": str(one), "targets": ["codex"]})
        self.publish(first)
        second = self.lib.import_item({"kind": "instruction", "name": "two", "source": str(codex), "targets": ["codex", "pi"]})
        self.publish(second)
        result = (self.home / ".pi/agent/AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("my original text", result)
        self.assertNotIn("first shared", result)
        self.assertEqual(result.count("agent-hub:instruction:"), 2)

    def test_rejected_reserved_windows_and_claude_names(self):
        source = self.skill()
        for name in ("con", "synced", "anthropic-skills"):
            with self.assertRaises(LibraryError):
                self.lib.import_item({"kind": "skill", "name": name, "source": str(source), "targets": ["claude"]})

    def test_state_write_failure_reverts_files(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        plan = self.lib.preview(item["id"])
        with patch.object(self.lib, "_save", side_effect=OSError("fixture state failure")):
            with self.assertRaises(LibraryError): self.lib.apply(plan["planId"])
        self.assertFalse((self.home / ".codex/skills/work").exists())
        self.assertIsNone(self.lib.list_items()["items"][0]["published"])

    def test_instruction_replacements_are_explicit_and_preview_actual_variants(self):
        source = self.base / "shared.md"
        source.write_text("当前 agent 标识为 codex\nrecorder --agent codex\n", encoding="utf-8")
        item = self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source),
                                    "targets": ["codex", "pi", "dsh"],
                                    "overrides": {"pi": [{"from": "codex", "to": "pi"}]}})
        plan = self.lib.preview(item["id"])
        rows = {row["agent"]: row for row in plan["results"]}
        self.assertIn("--agent pi", rows["pi"]["diff"]["text"])
        self.assertIn("--agent codex", rows["dsh"]["diff"]["text"])
        self.assertTrue(any("dsh" in warning and "codex" in warning for warning in plan["warnings"]))
        self.assertFalse(any("pi 的指令包含" in warning for warning in plan["warnings"]))
        self.lib.apply(plan["planId"])
        self.assertIn("--agent pi", (self.home / ".pi/agent/AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("--agent codex", (self.home / ".dsh/AGENTS.md").read_text(encoding="utf-8"))

    def test_manual_published_version_includes_original_per_agent_replacements(self):
        source = self.base / "shared.md"; source.write_text("identity=codex", encoding="utf-8")
        original = {"pi": [{"from": "codex", "to": "pi-old"}],
                    "claude": [{"from": "codex", "to": "claude-old"}]}
        item = self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source),
                                    "targets": ["pi"], "overrides": original})
        self.publish(item)
        changed = {"pi": [{"from": "codex", "to": "pi-new"}],
                   "claude": [{"from": "codex", "to": "claude-new"}]}
        self.lib.update_item(item["id"], {"targets": ["pi", "claude"], "overrides": changed})
        self.assertEqual(self.lib.list_items()["items"][0]["sourceStatus"], "changed")
        self.publish(item, {"published": True})
        pi = self.home / ".pi/agent/AGENTS.md"
        claude = self.home / ".claude/CLAUDE.md"
        self.assertIn("identity=pi-old", pi.read_text(encoding="utf-8"))
        self.assertIn("identity=claude-old", claude.read_text(encoding="utf-8"))
        self.assertNotIn("identity=pi-new", pi.read_text(encoding="utf-8"))
        self.publish(item)
        self.assertIn("identity=pi-new", pi.read_text(encoding="utf-8"))
        self.assertIn("identity=claude-new", claude.read_text(encoding="utf-8"))

    def test_override_change_is_in_auto_fingerprint(self):
        source = self.base / "shared.md"; source.write_text("identity=codex", encoding="utf-8")
        item = self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source),
                                    "targets": ["pi"], "mode": "auto",
                                    "overrides": {"pi": [{"from": "codex", "to": "pi-v1"}]}})
        self.publish(item)
        old_hash = self.lib.list_items()["items"][0]["sourceHash"]
        self.lib.update_item(item["id"], {"overrides": {"pi": [{"from": "codex", "to": "pi-v2"}]}})
        new_hash = self.lib.list_items()["items"][0]["sourceHash"]
        self.assertNotEqual(new_hash, old_hash)
        self.assertFalse(self.lib.sync_auto([item["id"]], {item["id"]: old_hash})["results"][0]["ok"])
        self.assertTrue(self.lib.sync_auto([item["id"]], {item["id"]: new_hash})["results"][0]["ok"])
        self.assertIn("identity=pi-v2", (self.home / ".pi/agent/AGENTS.md").read_text(encoding="utf-8"))

    def test_restore_pauses_auto_sync_until_explicitly_reenabled(self):
        source = self.skill(); item = self.add_skill(source, ("codex",), mode="auto")
        self.publish(item)
        (source / "references/guide.md").write_text("v2")
        result = self.lib.sync_auto([item["id"]])["results"][0]
        target = self.home / ".codex/skills/work/references/guide.md"
        self.assertEqual(target.read_text(), "v2")
        restored = self.lib.restore(result["transactionId"])
        self.assertTrue(restored["autoSyncPaused"])
        self.assertEqual(self.lib.list_items()["items"][0]["mode"], "manual")
        self.assertEqual(target.read_text(), "v1")
        self.assertEqual(self.lib.sync_auto([item["id"]])["results"], [])
        self.assertEqual(target.read_text(), "v1")
        self.lib.update_item(item["id"], {"mode": "auto"})
        self.lib.sync_auto([item["id"]])
        self.assertEqual(target.read_text(), "v2")

    def test_invalid_replacement_rejected(self):
        source = self.base / "shared.md"; source.write_text("shared", encoding="utf-8")
        for rules in ({"pi": [{"from": "", "to": "x"}]},
                      {"pi": [{"from": "s", "to": "x"}, {"from": "s", "to": "y"}]},
                      {"unrecognized": []}):
            with self.assertRaises(LibraryError):
                self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source),
                                      "targets": ["pi"], "overrides": rules})

    def test_edit_during_final_skill_swap_is_not_overwritten(self):
        import capability_library
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self.publish(item)
        (source / "references/guide.md").write_text("v2")
        plan = self.lib.preview(item["id"])
        target = self.home / ".codex/skills/work"
        real = capability_library._rename_noreplace
        def concurrent_edit(src, dst):
            if Path(src) == target and ".agent-hub-escrow-" in Path(dst).name:
                (target / "references/guide.md").write_text("user edit during swap")
            return real(src, dst)
        with patch.object(capability_library, "_rename_noreplace", side_effect=concurrent_edit):
            with self.assertRaises(LibraryError): self.lib.apply(plan["planId"])
        self.assertEqual((target / "references/guide.md").read_text(), "user edit during swap")

    def test_two_independent_roots_cannot_overwrite_concurrent_creation(self):
        import capability_library
        source_a = self.skill(); source_b = self.base / "other" / "work"
        self.skill(folder=source_b)
        (source_b / "references/guide.md").write_text("from B")
        item_a = self.add_skill(source_a, ("codex",))
        other = CapabilityLibrary(self.base / "other-data", self.home)
        item_b = other.import_item({"kind": "skill", "name": "work", "source": str(source_b), "targets": ["codex"]})
        plan_a = self.lib.preview(item_a["id"]); plan_b = other.preview(item_b["id"])
        real = capability_library._rename_noreplace
        fired = [False]; rejected = []
        def race(src, dst):
            if not fired[0] and Path(src).name.startswith(".agent-hub-stage-"):
                fired[0] = True
                try: other.apply(plan_b["planId"])
                except LibraryError as exc: rejected.append(str(exc))
            return real(src, dst)
        with patch.object(capability_library, "_rename_noreplace", side_effect=race):
            self.lib.apply(plan_a["planId"])
        self.assertTrue(rejected)
        self.assertEqual((self.home / ".codex/skills/work/references/guide.md").read_text(), "v1")
        self.assertTrue(other.preview(item_b["id"])["hasConflicts"])

    def _crash_publish(self, item, point):
        program = r'''
import os, sys
import capability_library as c
lib=c.CapabilityLibrary(sys.argv[1],sys.argv[2])
plan=lib.preview(sys.argv[3])
real=c._rename_noreplace
def stop(src,dst):
    real(src,dst)
    from pathlib import Path
    if sys.argv[4]=='installed' and Path(src).name.startswith('.agent-hub-stage-'):
        os._exit(23)
    if sys.argv[4]=='escrow' and Path(dst).name.startswith('.agent-hub-escrow-'):
        os._exit(23)
c._rename_noreplace=stop
lib.apply(plan['planId'])
'''
        result = subprocess.run([sys.executable, "-c", program, str(self.root), str(self.home), item["id"], point],
                                cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 23, result.stderr)
        fresh = CapabilityLibrary(self.root, self.home)
        entry = fresh.history(item["id"])["transactions"][0]
        self.assertTrue(entry["interrupted"])
        self.assertTrue(entry["canRestore"])
        return fresh, entry["id"]

    def test_interrupted_first_publish_can_be_recovered_after_restart(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        fresh, tx = self._crash_publish(item, "installed")
        target = self.home / ".codex/skills/work"
        self.assertTrue(target.exists())
        result = fresh.restore(tx)
        self.assertTrue(result["interrupted"])
        self.assertFalse(target.exists())
        self.assertIsNone(fresh.list_items()["items"][0]["published"])

    def test_interrupted_swap_with_missing_target_restores_escrow(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self.publish(item)
        (source / "references/guide.md").write_text("v2")
        fresh, tx = self._crash_publish(item, "escrow")
        target = self.home / ".codex/skills/work"
        self.assertFalse(target.exists())
        fresh.restore(tx)
        self.assertEqual((target / "references/guide.md").read_text(), "v1")

    def test_interrupted_after_applied_journal_before_state_is_recoverable(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        program = r'''
import os,sys
import capability_library as c
lib=c.CapabilityLibrary(sys.argv[1],sys.argv[2])
plan=lib.preview(sys.argv[3])
lib._save=lambda state: os._exit(24)
lib.apply(plan['planId'])
'''
        child = subprocess.run([sys.executable, "-c", program, str(self.root), str(self.home), item["id"]],
                               cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 24, child.stderr)
        fresh = CapabilityLibrary(self.root, self.home)
        row = fresh.history(item["id"])["transactions"][0]
        self.assertEqual(row["status"], "applied")
        self.assertTrue(row["interrupted"])
        fresh.restore(row["id"])
        self.assertFalse((self.home / ".codex/skills/work").exists())

    def test_interrupted_restore_rolls_back_partial_restore_before_retry(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self.publish(item)
        (source / "references/guide.md").write_text("v2")
        transaction = self.publish(item)["transactionId"]
        program = r'''
import os,sys
from pathlib import Path
import capability_library as c
lib=c.CapabilityLibrary(sys.argv[1],sys.argv[2])
real=c._rename_noreplace
def stop(src,dst):
    real(src,dst)
    if Path(src).name.startswith('.agent-hub-stage-'):
        os._exit(25)
c._rename_noreplace=stop
lib.restore(sys.argv[3])
'''
        child = subprocess.run([sys.executable, "-c", program, str(self.root), str(self.home), transaction],
                               cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 25, child.stderr)
        fresh = CapabilityLibrary(self.root, self.home)
        row = fresh.history(item["id"])["transactions"][0]
        self.assertTrue(row["interrupted"])
        self.assertEqual(row["status"], "restoring")
        result = fresh.restore(transaction)
        self.assertIn("撤回中断的恢复", result["message"])
        target = self.home / ".codex/skills/work/references/guide.md"
        self.assertEqual(target.read_text(), "v2")
        fresh.restore(transaction)
        self.assertEqual(target.read_text(), "v1")

    def test_restore_state_commit_before_journal_finalization_is_recoverable(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self.publish(item)
        (source / "references/guide.md").write_text("v2")
        transaction = self.publish(item)["transactionId"]
        program = r'''
import os,sys
import capability_library as c
lib=c.CapabilityLibrary(sys.argv[1],sys.argv[2])
real=c._atomic_json
def stop(path,value):
    if value.get('status') == 'restored': os._exit(26)
    real(path,value)
c._atomic_json=stop
lib.restore(sys.argv[3])
'''
        child = subprocess.run([sys.executable, "-c", program, str(self.root), str(self.home), transaction],
                               cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 26, child.stderr)
        fresh = CapabilityLibrary(self.root, self.home)
        self.assertTrue(fresh.history(item["id"])["transactions"][0]["interrupted"])
        result = fresh.restore(transaction)
        self.assertTrue(result["interrupted"])
        self.assertEqual(fresh.history(item["id"])["transactions"][0]["status"], "restored")
        self.assertEqual((self.home / ".codex/skills/work/references/guide.md").read_text(), "v1")

    def test_unfinished_transaction_blocks_publish_update_remove_and_auto(self):
        source = self.skill(); item = self.add_skill(source, ("codex",), mode="auto")
        self.publish(item)
        (source / "references/guide.md").write_text("v2")
        fresh, tx = self._crash_publish(item, "installed")
        row = fresh.list_items()["items"][0]
        self.assertEqual(row["pendingTransaction"], tx)
        with self.assertRaisesRegex(LibraryError, "未完成的事务"): fresh.preview(item["id"])
        with self.assertRaisesRegex(LibraryError, "未完成的事务"): fresh.update_item(item["id"], {"targets": []})
        with self.assertRaisesRegex(LibraryError, "未完成的事务"): fresh.update_item(item["id"], {"mode": "manual"})
        with self.assertRaisesRegex(LibraryError, "未完成的事务"): fresh.remove_item(item["id"])
        result = fresh.sync_auto([item["id"]])["results"][0]
        self.assertFalse(result["ok"])
        self.assertIn("未完成的事务", result["error"])
        self.assertTrue(fresh.history(item["id"])["transactions"][0]["canRestore"])
        fresh.restore(tx)
        self.assertIsNone(fresh.list_items()["items"][0]["pendingTransaction"])
        self.assertEqual(fresh.list_items()["items"][0]["mode"], "manual")
        self.assertEqual((self.home / ".codex/skills/work/references/guide.md").read_text(), "v1")

    def test_crash_reservation_blocks_a_different_hub_data_root(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self.publish(item)
        other_source = self.base / "other" / "work"; self.skill(folder=other_source)
        other = CapabilityLibrary(self.base / "other-data", self.home)
        other_item = other.import_item({"kind": "skill", "name": "work", "source": str(other_source), "targets": ["codex"]})
        (source / "references/guide.md").write_text("v2")
        fresh, tx = self._crash_publish(item, "escrow")
        self.assertFalse((self.home / ".codex/skills/work").exists())
        row = other.list_items()["items"][0]
        self.assertEqual(row["targetStatus"]["codex"], "conflict")
        self.assertEqual(row["targetPending"]["codex"]["transactionId"], tx)
        plan = other.preview(other_item["id"], {"forceTargets": ["codex"]})
        self.assertTrue(plan["hasConflicts"])
        self.assertTrue(plan["results"][0]["blockedByRecovery"])
        with self.assertRaises(LibraryError): other.apply(plan["planId"], skip_conflicts=True)
        self.assertFalse((self.home / ".codex/skills/work").exists())
        fresh.restore(tx)
        self.assertEqual((self.home / ".codex/skills/work/references/guide.md").read_text(), "v1")
        self.assertNotIn("codex", other.list_items()["items"][0]["targetPending"])

    def test_completed_stale_reservation_remains_resolvable_after_item_removal(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        with patch.object(self.lib, "_release_targets", side_effect=OSError("fixture cleanup failure")):
            self.publish(item)
        self.lib.remove_item(item["id"])
        other = CapabilityLibrary(self.base / "other-data", self.home)
        other_item = other.import_item({"kind": "skill", "name": "work", "source": str(source), "targets": ["codex"]})
        row = other.list_items()["items"][0]
        self.assertNotIn("codex", row["targetPending"])
        plan = other.preview(other_item["id"], {"forceTargets": ["codex"]})
        self.assertFalse(plan["hasConflicts"])
        other.apply(plan["planId"])

    def _crash_during_marker_write(self, item):
        program = r'''
import os,sys
from pathlib import Path
import capability_library as c
lib=c.CapabilityLibrary(sys.argv[1],sys.argv[2])
plan=lib.preview(sys.argv[3])
real=c._rename_noreplace
def stop(src,dst):
    if Path(src).name.startswith('.agent-hub-reservation-'): os._exit(27)
    real(src,dst)
c._rename_noreplace=stop
lib.apply(plan['planId'])
'''
        child = subprocess.run([sys.executable, "-c", program, str(self.root), str(self.home), item["id"]],
                               cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 27, child.stderr)
        fresh = CapabilityLibrary(self.root, self.home)
        row = fresh.history(item["id"])["transactions"][0]
        self.assertTrue(row["interrupted"])
        fresh.restore(row["id"])
        self.assertIsNone(fresh.list_items()["items"][0]["pendingTransaction"])
        return fresh

    def test_crash_before_atomic_marker_on_first_publish_is_recoverable(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self._crash_during_marker_write(item)
        self.assertFalse((self.home / ".codex/skills/work").exists())

    def test_crash_before_atomic_marker_for_existing_target_is_recoverable(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self.publish(item)
        (source / "references/guide.md").write_text("v2")
        self._crash_during_marker_write(item)
        self.assertEqual((self.home / ".codex/skills/work/references/guide.md").read_text(), "v1")

    def test_publish_crash_before_reservation_preserves_other_root_change(self):
        a_source = self.base / "alpha.md"; a_source.write_text("alpha-1", encoding="utf-8")
        b_source = self.base / "beta.md"; b_source.write_text("beta-1", encoding="utf-8")
        a = self.lib.import_item({"kind": "instruction", "name": "alpha", "source": str(a_source), "targets": ["codex"]})
        other = CapabilityLibrary(self.base / "other-data", self.home)
        b = other.import_item({"kind": "instruction", "name": "beta", "source": str(b_source), "targets": ["codex"]})
        self.publish(a)
        other.apply(other.preview(b["id"])["planId"])
        a_source.write_text("alpha-2", encoding="utf-8")
        program = r'''
import os,sys
import capability_library as c
lib=c.CapabilityLibrary(sys.argv[1],sys.argv[2]); plan=lib.preview(sys.argv[3])
lib._reserve_targets=lambda *a,**kw: os._exit(79)
lib.apply(plan['planId'])
'''
        child = subprocess.run([sys.executable, "-c", program, str(self.root), str(self.home), a["id"]],
                               cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 79, child.stderr)
        b_source.write_text("beta-2", encoding="utf-8")
        other.apply(other.preview(b["id"])["planId"])
        fresh = CapabilityLibrary(self.root, self.home)
        tx = fresh.history(a["id"])["transactions"][0]["id"]
        self.assertTrue(fresh.restore(tx)["interrupted"])
        content = (self.home / ".codex/AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("alpha-1", content)
        self.assertIn("beta-2", content)
        self.assertNotIn("alpha-2", content)
        self.assertIsNone(fresh.list_items()["items"][0]["pendingTransaction"])

    def test_restore_crash_before_reservation_preserves_other_root_change(self):
        a_source = self.base / "alpha.md"; a_source.write_text("alpha-1", encoding="utf-8")
        b_source = self.base / "beta.md"; b_source.write_text("beta-1", encoding="utf-8")
        a = self.lib.import_item({"kind": "instruction", "name": "alpha", "source": str(a_source), "targets": ["codex"]})
        other = CapabilityLibrary(self.base / "other-data", self.home)
        b = other.import_item({"kind": "instruction", "name": "beta", "source": str(b_source), "targets": ["codex"]})
        self.publish(a)
        other.apply(other.preview(b["id"])["planId"])
        a_source.write_text("alpha-2", encoding="utf-8")
        transaction = self.publish(a)["transactionId"]
        program = r'''
import os,sys
import capability_library as c
lib=c.CapabilityLibrary(sys.argv[1],sys.argv[2])
lib._reserve_targets=lambda *a,**kw: os._exit(79)
lib.restore(sys.argv[3])
'''
        child = subprocess.run([sys.executable, "-c", program, str(self.root), str(self.home), transaction],
                               cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 79, child.stderr)
        b_source.write_text("beta-2", encoding="utf-8")
        other.apply(other.preview(b["id"])["planId"])
        fresh = CapabilityLibrary(self.root, self.home)
        self.assertTrue(fresh.restore(transaction)["interrupted"])
        content = (self.home / ".codex/AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("alpha-2", content)
        self.assertIn("beta-2", content)
        fresh.restore(transaction)
        content = (self.home / ".codex/AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("alpha-1", content)
        self.assertIn("beta-2", content)

    def test_all_conflicted_targets_do_not_create_a_published_revision(self):
        source = self.skill(); target = self.home / ".codex/skills/work"
        target.mkdir(parents=True); (target / "SKILL.md").write_text("unmanaged")
        item = self.add_skill(source, ("codex",))
        plan = self.lib.preview(item["id"])
        with self.assertRaisesRegex(LibraryError, "没有可安全发布的目标"):
            self.lib.apply(plan["planId"], skip_conflicts=True)
        self.assertIsNone(self.lib.list_items()["items"][0]["published"])
        self.assertEqual((target / "SKILL.md").read_text(), "unmanaged")

    def test_primary_source_as_only_target_can_be_published_as_snapshot(self):
        source = self.skill(folder=self.home / ".codex/skills/work")
        item = self.add_skill(source, ("codex",))
        self.publish(item)
        self.assertEqual(self.lib.list_items()["items"][0]["sourceStatus"], "current")
        self.assertEqual((source / "references/guide.md").read_text(), "v1")

    @unittest.skipUnless(os.name == "nt", "Windows share-mode behavior")
    def test_win32_open_writer_blocks_instruction_publish_and_restore(self):
        source = self.base / "shared.md"; source.write_text("old", encoding="utf-8")
        item = self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source), "targets": ["codex"]})
        self.publish(item)
        target = self.home / ".codex/AGENTS.md"
        old = target.read_bytes()
        source.write_text("new", encoding="utf-8")
        plan = self.lib.preview(item["id"])
        with windows_writer(target):
            with self.assertRaises(LibraryError): self.lib.apply(plan["planId"])
            self.assertEqual(target.read_bytes(), old)
        result = self.publish(item)
        new = target.read_bytes()
        self.assertNotEqual(new, old)
        with windows_writer(target):
            with self.assertRaises(LibraryError): self.lib.restore(result["transactionId"])
            self.assertEqual(target.read_bytes(), new)
        self.lib.restore(result["transactionId"])
        self.assertEqual(target.read_bytes(), old)

    @unittest.skipUnless(os.name == "nt", "Windows share-mode behavior")
    def test_win32_preexisting_writer_is_not_detached_by_publish(self):
        from ctypes import wintypes
        source = self.base / "shared.md"; source.write_text("old", encoding="utf-8")
        item = self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source), "targets": ["codex"]})
        self.publish(item)
        target = self.home / ".codex/AGENTS.md"
        source.write_text("new", encoding="utf-8")
        plan = self.lib.preview(item["id"])
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetFilePointerEx.restype = wintypes.BOOL
        kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.WriteFile.restype = wintypes.BOOL
        kernel.SetEndOfFile.argtypes = [wintypes.HANDLE]; kernel.SetEndOfFile.restype = wintypes.BOOL
        kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]; kernel.FlushFileBuffers.restype = wintypes.BOOL
        with windows_writer(target) as handle:
            with self.assertRaises(LibraryError): self.lib.apply(plan["planId"])
            self.assertTrue(kernel.SetFilePointerEx(handle, 0, None, 0))
            raw = b"late-user-edit"; written = wintypes.DWORD()
            self.assertTrue(kernel.WriteFile(handle, raw, len(raw), ctypes.byref(written), None))
            self.assertTrue(kernel.SetEndOfFile(handle))
            self.assertTrue(kernel.FlushFileBuffers(handle))
        self.assertEqual(target.read_bytes(), b"late-user-edit")

    @unittest.skipUnless(os.name == "nt", "Windows share-mode behavior")
    def test_win32_new_writer_is_blocked_during_instruction_swap(self):
        import capability_library
        source = self.base / "shared.md"; source.write_text("old", encoding="utf-8")
        item = self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source), "targets": ["codex"]})
        self.publish(item)
        target = self.home / ".codex/AGENTS.md"
        source.write_text("new", encoding="utf-8")
        plan = self.lib.preview(item["id"])
        real = capability_library._rename_noreplace; blocked = []
        def race(src, dst):
            if Path(src) == target:
                try:
                    with windows_writer(target): pass
                except OSError: blocked.append(True)
            return real(src, dst)
        with patch.object(capability_library, "_rename_noreplace", side_effect=race):
            self.lib.apply(plan["planId"])
        self.assertEqual(blocked, [True])
        self.assertIn("new", target.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows directory sharing behavior")
    def test_win32_open_child_writer_blocks_skill_directory_swap(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self.publish(item)
        target = self.home / ".codex/skills/work/references/guide.md"
        (source / "references/guide.md").write_text("v2")
        plan = self.lib.preview(item["id"])
        with windows_writer(target):
            with self.assertRaises(LibraryError): self.lib.apply(plan["planId"])
            self.assertEqual(target.read_text(), "v1")
        self.publish(item)
        self.assertEqual(target.read_text(), "v2")

    def test_recovery_does_not_overwrite_new_user_changes(self):
        source = self.skill(); item = self.add_skill(source, ("codex",))
        self.publish(item)
        (source / "references/guide.md").write_text("v2")
        fresh, tx = self._crash_publish(item, "installed")
        target = self.home / ".codex/skills/work/references/guide.md"
        target.write_text("user post-crash edit")
        with self.assertRaisesRegex(LibraryError, "冲突"):
            fresh.restore(tx)
        self.assertEqual(target.read_text(), "user post-crash edit")
        backups = list((self.home / ".codex/.agent-hub-transactions").glob(".agent-hub-escrow-*"))
        self.assertTrue(backups)
        self.assertTrue(any((p / "references/guide.md").read_text() == "v1" for p in backups if p.is_dir()))

    def test_pause_between_auto_scan_and_preview_prevents_write(self):
        source = self.skill(); item = self.add_skill(source, ("codex",), mode="auto")
        self.publish(item)
        (source / "references/guide.md").write_text("v2")
        real = self.lib.preview
        def pause_then_preview(*args, **kwargs):
            self.lib.update_item(item["id"], {"mode": "manual"})
            return real(*args, **kwargs)
        with patch.object(self.lib, "preview", side_effect=pause_then_preview):
            result = self.lib.sync_auto([item["id"]])["results"][0]
        self.assertFalse(result["ok"])
        self.assertEqual((self.home / ".codex/skills/work/references/guide.md").read_text(), "v1")

    def test_auto_pauses_only_target_with_unmatched_agent_replacement(self):
        source = self.base / "shared.md"
        source.write_text("agent 标识为 codex", encoding="utf-8")
        item = self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source),
                                    "targets": ["codex", "pi"], "mode": "auto",
                                    "overrides": {"pi": [{"from": "agent 标识为 codex", "to": "agent 标识为 pi"}]}})
        self.publish(item)
        pi = self.home / ".pi/agent/AGENTS.md"; codex = self.home / ".codex/AGENTS.md"
        before = pi.read_bytes()
        source.write_text("agent 标识为 `codex`", encoding="utf-8")
        result = self.lib.sync_auto([item["id"]])["results"][0]
        self.assertTrue(result["partial"])
        self.assertEqual(pi.read_bytes(), before)
        self.assertIn("`codex`", codex.read_text(encoding="utf-8"))
        row = self.lib.list_items()["items"][0]
        self.assertEqual(row["targetStatus"]["pi"], "conflict")
        self.assertTrue(row["autoWarnings"]["pi"])

    def test_restore_one_instruction_preserves_other_item_and_unmanaged_edits(self):
        first = self.base / "first.md"; first.write_text("first", encoding="utf-8")
        second = self.base / "second.md"; second.write_text("second", encoding="utf-8")
        a = self.lib.import_item({"kind": "instruction", "name": "first", "source": str(first), "targets": ["codex"]})
        b = self.lib.import_item({"kind": "instruction", "name": "second", "source": str(second), "targets": ["codex"]})
        tx = self.publish(a)["transactionId"]
        self.publish(b)
        target = self.home / ".codex/AGENTS.md"
        target.write_bytes(b"user added\n" + target.read_bytes())
        self.assertTrue(self.lib.history(a["id"])["transactions"][0]["canRestore"])
        self.lib.restore(tx)
        final = target.read_text(encoding="utf-8")
        self.assertIn("user added", final)
        self.assertIn("second", final)
        self.assertNotIn("first", final)

    def test_reimported_instruction_requires_explicit_old_block_adoption(self):
        source = self.base / "shared.md"; source.write_text("IDENTITY=old", encoding="utf-8")
        original = self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source), "targets": ["codex"]})
        self.publish(original)
        self.lib.remove_item(original["id"])
        source.write_text("IDENTITY=new", encoding="utf-8")
        replacement = self.lib.import_item({"kind": "instruction", "name": "shared", "source": str(source), "targets": ["codex"]})
        plan = self.lib.preview(replacement["id"])
        self.assertTrue(plan["hasConflicts"])
        with self.assertRaises(LibraryError): self.lib.apply(plan["planId"])
        target = self.home / ".codex/AGENTS.md"
        self.assertIn("IDENTITY=old", target.read_text(encoding="utf-8"))
        plan = self.lib.preview(replacement["id"], {"forceTargets": ["codex"]})
        transaction = self.lib.apply(plan["planId"])["transactionId"]
        result = target.read_text(encoding="utf-8")
        self.assertIn("IDENTITY=new", result)
        self.assertNotIn("IDENTITY=old", result)
        target.write_bytes(b"user added\n" + target.read_bytes())
        self.lib.restore(transaction)
        restored = target.read_text(encoding="utf-8")
        self.assertIn("user added", restored)
        self.assertIn("IDENTITY=old", restored)
        self.assertNotIn("IDENTITY=new", restored)


if __name__ == "__main__":
    unittest.main()
