"""Native sync transactions use temporary HOME fixtures and never contact providers."""
from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from support import isolated, engine
import sync_transaction as transaction


class SafeSyncTransactionTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated()
        self.home = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        transaction._PLANS.clear()
        self.addCleanup(transaction._PLANS.clear)
        self.settings = self.home / ".pi" / "agent" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text('{"theme":"keep","defaultModel":"official"}\n', encoding="utf-8")
        self.original_settings = self.settings.read_bytes()

    def config(self, targets=None):
        cfg = json.loads(engine.HUB_CONFIG.read_text(encoding="utf-8"))
        if targets is not None:
            for name, entry in cfg["sync"]["targets"].items():
                entry["enabled"] = name in targets
        return cfg

    def save(self, cfg):
        engine.HUB_CONFIG.write_text(json.dumps(cfg), encoding="utf-8")

    def native_files(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in self.home.rglob("*") if path.is_file() and ".agent-hub" not in path.parts}

    def manifest(self, result):
        path = engine.HUB_BACKUP / "transactions" / result["transactionId"] / "manifest.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    def codex_fixture(self):
        cfg = self.config(["pi", "codex"])
        cfg["sync"]["targets"]["codex"].update(mode="third-party")
        cfg["providers"]["deepseek"]["codexWireApi"] = "responses"
        cfg["providers"]["deepseek"]["apiKeyEnv"] = "TEST_CODEX_API_KEY"
        self.save(cfg)
        root = self.home / ".codex"
        root.mkdir(exist_ok=True)
        (root / "config.toml").write_text('model = "official-model"\nmodel_reasoning_effort = "high"\n', encoding="utf-8")
        return root

    def test_preview_is_write_free_and_redacted(self):
        before = self.native_files()
        plan = transaction.preview(engine.Hub(), ["pi"])
        self.assertEqual(before, self.native_files())
        self.assertFalse(engine.HUB_BACKUP.exists())
        self.assertEqual(plan["targets"], ["pi"])
        self.assertEqual({file["action"] for file in plan["files"]}, {"create", "update"})
        self.assertNotIn("test-not-a-real-key", json.dumps(plan))
        self.assertEqual(engine.HOME, self.home)

    def test_missing_targets_are_never_implicitly_enabled(self):
        cfg = self.config(["pi"])
        cfg["sync"]["targets"] = {"pi": cfg["sync"]["targets"]["pi"]}
        self.save(cfg)
        plan = transaction.preview(engine.Hub())
        self.assertEqual(plan["targets"], ["pi"])
        self.assertTrue(all(f"/.pi/".replace("/", os.sep) in file["path"] for file in plan["files"]))

    def test_disabled_selected_target_rejected_without_writes(self):
        before = self.native_files()
        with self.assertRaisesRegex(transaction.TransactionError, "not explicitly enabled"):
            transaction.preview(engine.Hub(), ["codex"])
        self.assertEqual(before, self.native_files())

    def test_unknown_target_and_empty_selection_rejected(self):
        for selection in ([], ["unknown"], "pi"):
            with self.subTest(selection=selection), self.assertRaises(transaction.TransactionError):
                transaction.preview(engine.Hub(), selection)

    def test_alias_requires_enabled_canonical_target(self):
        cfg = self.config(["dsh-desktop"])
        self.save(cfg)
        for selection in (["dsh-desktop"], None):
            with self.subTest(selection=selection), self.assertRaisesRegex(transaction.TransactionError, "canonical target"):
                transaction.preview(engine.Hub(), selection)
        self.assertFalse(engine.HUB_BACKUP.exists())

    def test_arbitrary_alias_cannot_hide_or_redirect_selected_target(self):
        cfg = self.config(["pi"])
        cfg["sync"]["targets"]["pi"]["aliasOf"] = "codex"
        self.save(cfg)
        for selection in (["pi"], None):
            with self.subTest(selection=selection), self.assertRaisesRegex(transaction.TransactionError, "aliasOf"):
                transaction.preview(engine.Hub(), selection)
        self.assertFalse((self.home / ".codex").exists())

    def test_invalid_second_target_prevents_any_first_target_write(self):
        self.codex_fixture()
        path = self.home / ".codex" / "config.toml"
        path.write_text('model = "unterminated', encoding="utf-8")
        before = self.native_files()
        with self.assertRaisesRegex(transaction.TransactionError, "Invalid configuration"):
            transaction.preview(engine.Hub(), ["pi", "codex"])
        self.assertEqual(before, self.native_files())
        self.assertFalse(engine.HUB_BACKUP.exists())

    def test_late_adapter_failure_discards_all_staged_outputs(self):
        self.codex_fixture()
        cfg = self.config()
        cfg["providers"]["deepseek"]["codexWireApi"] = "unsupported"
        self.save(cfg)
        before = self.native_files()
        with self.assertRaisesRegex(transaction.TransactionError, "Cannot compile codex"):
            transaction.preview(engine.Hub(), ["pi", "codex"])
        self.assertEqual(before, self.native_files())
        self.assertFalse(engine.HUB_BACKUP.exists())

    def test_apply_writes_only_selected_allowlist_and_saves_private_backups(self):
        cfg = self.config()
        outsider = self.home / "do-not-read.json"
        outsider.write_text("not even JSON", encoding="utf-8")
        cfg["sync"]["targets"]["pi"]["files"] = [str(outsider)]
        self.save(cfg)
        plan = transaction.preview(engine.Hub(), ["pi"])
        result = transaction.apply(engine.Hub(), plan["planId"])
        self.assertEqual(result["results"], ["  ✓ pi"])
        self.assertEqual(outsider.read_text(), "not even JSON")
        self.assertFalse((self.home / ".claude").exists())
        manifest_path, manifest = self.manifest(result)
        self.assertEqual(manifest["status"], "committed")
        settings = next(item for item in manifest["files"] if item["path"].endswith("settings.json"))
        self.assertEqual((manifest_path.parent / settings["backup"]).read_bytes(), self.original_settings)
        self.assertNotIn("test-not-a-real-key", manifest_path.read_text())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(manifest_path.parent.stat().st_mode), 0o700)

    def test_restore_rejects_a_readonly_flip_but_allows_plain_mode_bits(self):
        """mode 比较：Windows 只有只读位可表达，POSIX 位上浮不算外部改动。

        反向对照：真实改变只读位（Windows 上唯一能表达的权限差异）必须被拒绝，
        否则放宽比较就等于关掉了"外部改动检测"。
        """
        plan = transaction.preview(engine.Hub(), ["pi"])
        applied = transaction.apply(engine.Hub(), plan["planId"])
        settings = self.settings

        if os.name != "nt":
            # 只改权限位（不碰内容、不碰只读位）→ 不构成外部改动
            settings.chmod(0o640)
            transaction.restore(engine.Hub(), applied["transactionId"])
            self.assertEqual(settings.read_text(), self.original_settings or settings.read_text())
            return

        settings.chmod(stat.S_IREAD)
        try:
            with self.assertRaisesRegex(transaction.TransactionError, "changed after apply"):
                transaction.restore(engine.Hub(), applied["transactionId"])
        finally:
            settings.chmod(stat.S_IWRITE | stat.S_IREAD)
        transaction.restore(engine.Hub(), applied["transactionId"])

    def test_partial_failure_rolls_back_prior_files_and_new_files(self):
        plan = transaction.preview(engine.Hub(), ["pi"])
        before = self.native_files()
        original = transaction._atomic_write
        failed = False

        def fail_second_native(path, data, mode=None):
            nonlocal failed
            if path == self.settings.parent / "models.json" and not failed:
                failed = True
                raise OSError("Injected disk error")
            return original(path, data, mode)

        with patch.object(transaction, "_atomic_write", side_effect=fail_second_native):
            with self.assertRaisesRegex(transaction.TransactionError, "rolled back"):
                transaction.apply(engine.Hub(), plan["planId"])
        self.assertTrue(failed)
        self.assertEqual(before, self.native_files())
        manifests = list(engine.HUB_BACKUP.glob("transactions/*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())["status"], "rolled_back")

    def test_failure_after_replace_also_rolls_back(self):
        plan = transaction.preview(engine.Hub(), ["pi"])
        before = self.native_files()
        original = transaction._atomic_write
        failed = False

        def fail_after_replace(path, data, mode=None):
            nonlocal failed
            original(path, data, mode)
            if path == self.settings and not failed:
                failed = True
                raise OSError("Injected fsync failure after replace")

        with patch.object(transaction, "_atomic_write", side_effect=fail_after_replace):
            with self.assertRaisesRegex(transaction.TransactionError, "rolled back"):
                transaction.apply(engine.Hub(), plan["planId"])
        self.assertEqual(before, self.native_files())

    def test_changed_native_file_rejects_stale_preview(self):
        plan = transaction.preview(engine.Hub(), ["pi"])
        self.settings.write_text('{"theme":"external"}')
        before = self.native_files()
        with self.assertRaisesRegex(transaction.TransactionError, "stale"):
            transaction.apply(engine.Hub(), plan["planId"])
        self.assertEqual(before, self.native_files())
        self.assertFalse(engine.HUB_BACKUP.exists())

    def test_changed_hub_config_rejects_stale_preview(self):
        hub = engine.Hub()
        plan = transaction.preview(hub, ["pi"])
        cfg = self.config()
        cfg["providers"]["deepseek"]["name"] = "Changed outside Hub"
        self.save(cfg)
        with self.assertRaisesRegex(transaction.TransactionError, "stale"):
            transaction.apply(hub, plan["planId"])
        self.assertEqual(self.settings.read_bytes(), self.original_settings)

    def test_stale_hub_instance_is_rejected_before_preview(self):
        hub = engine.Hub()
        cfg = self.config()
        cfg["updatedAt"] = "2026-10-05T10:00:00Z"
        self.save(cfg)
        with self.assertRaisesRegex(transaction.TransactionError, "stale"):
            transaction.preview(hub, ["pi"])
        self.assertFalse(engine.HUB_BACKUP.exists())

    def test_jsonc_comments_inside_strings_survive_and_restore_exactly(self):
        original = '// real comment\n{"theme":"keep /* literal text */", "url":"https://example.test/a//b"}\n'
        self.settings.write_text(original)
        plan = transaction.preview(engine.Hub(), ["pi"])
        applied = transaction.apply(engine.Hub(), plan["planId"])
        parsed = json.loads(self.settings.read_text())
        self.assertEqual(parsed["theme"], "keep /* literal text */")
        self.assertEqual(parsed["url"], "https://example.test/a//b")
        transaction.restore(engine.Hub(), applied["transactionId"])
        self.assertEqual(self.settings.read_text(), original)

    def test_failed_backup_does_not_start_native_writes(self):
        plan = transaction.preview(engine.Hub(), ["pi"])
        before = self.native_files()
        original = transaction._atomic_write

        def fail_backup(path, data, mode=None):
            if path.suffix == ".before":
                raise OSError("Backup disk unavailable")
            return original(path, data, mode)

        with patch.object(transaction, "_atomic_write", side_effect=fail_backup):
            with self.assertRaises(OSError):
                transaction.apply(engine.Hub(), plan["planId"])
        self.assertEqual(before, self.native_files())

    def test_restore_partial_failure_rolls_back_to_applied_state(self):
        plan = transaction.preview(engine.Hub(), ["pi"])
        applied = transaction.apply(engine.Hub(), plan["planId"])
        before = self.native_files()
        original = transaction._atomic_write
        failed = False

        def fail_second_native(path, data, mode=None):
            nonlocal failed
            if path == self.settings.parent / "models.json" and not failed:
                failed = True
                raise OSError("Injected restore disk error")
            return original(path, data, mode)

        with patch.object(transaction, "_atomic_write", side_effect=fail_second_native):
            with self.assertRaisesRegex(transaction.TransactionError, "rolled back"):
                transaction.restore(engine.Hub(), applied["transactionId"])
        self.assertEqual(before, self.native_files())

    def test_successful_apply_invalidates_parallel_preview(self):
        first = transaction.preview(engine.Hub(), ["pi"])
        second = transaction.preview(engine.Hub(), ["pi"])
        transaction.apply(engine.Hub(), first["planId"])
        with self.assertRaisesRegex(transaction.TransactionError, "stale"):
            transaction.apply(engine.Hub(), second["planId"])

    def test_expired_or_consumed_plan_cannot_apply(self):
        plan = transaction.preview(engine.Hub(), ["pi"])
        with patch.object(transaction.time, "monotonic", return_value=transaction._PLANS[plan["planId"]].created + transaction.PLAN_TTL + 1):
            with self.assertRaisesRegex(transaction.TransactionError, "expired"):
                transaction.apply(engine.Hub(), plan["planId"])
        plan = transaction.preview(engine.Hub(), ["pi"])
        transaction.apply(engine.Hub(), plan["planId"])
        with self.assertRaisesRegex(transaction.TransactionError, "expired"):
            transaction.apply(engine.Hub(), plan["planId"])

    def test_restore_returns_exact_original_and_removes_created_files(self):
        before = self.native_files()
        plan = transaction.preview(engine.Hub(), ["pi"])
        applied = transaction.apply(engine.Hub(), plan["planId"])
        restored = transaction.restore(engine.Hub(), applied["transactionId"])
        self.assertEqual(restored["restored"], applied["transactionId"])
        self.assertNotEqual(restored["transactionId"], applied["transactionId"])
        self.assertEqual(before, self.native_files())
        self.assertEqual(self.manifest(restored)[1]["restoredFrom"], applied["transactionId"])

    def test_restore_rejects_external_changes_without_overwrite(self):
        plan = transaction.preview(engine.Hub(), ["pi"])
        applied = transaction.apply(engine.Hub(), plan["planId"])
        self.settings.write_text('{"theme":"new-external-change"}')
        before = self.native_files()
        with self.assertRaisesRegex(transaction.TransactionError, "changed after apply"):
            transaction.restore(engine.Hub(), applied["transactionId"])
        self.assertEqual(before, self.native_files())

    def test_restore_rejects_tampered_backup(self):
        plan = transaction.preview(engine.Hub(), ["pi"])
        applied = transaction.apply(engine.Hub(), plan["planId"])
        path, manifest = self.manifest(applied)
        entry = next(file for file in manifest["files"] if file["backup"])
        (path.parent / entry["backup"]).write_text("tampered")
        before = self.native_files()
        with self.assertRaisesRegex(transaction.TransactionError, "integrity"):
            transaction.restore(engine.Hub(), applied["transactionId"])
        self.assertEqual(before, self.native_files())

    def test_codex_preview_never_opens_native_auth_or_keyring(self):
        root = self.codex_fixture()
        (root / "auth.json").write_text("unparseable fake auth sentinel")
        (root / "keyring").mkdir()
        (root / "keyring" / "secret").write_text("fake sentinel")
        original = transaction._snapshot
        read = []

        def trace(path):
            read.append(path)
            self.assertNotEqual(path, root / "auth.json")
            self.assertFalse(path.is_relative_to(root / "keyring"))
            return original(path)

        with patch.object(transaction, "_snapshot", side_effect=trace):
            plan = transaction.preview(engine.Hub(), ["codex"])
            applied = transaction.apply(engine.Hub(), plan["planId"])
            transaction.restore(engine.Hub(), applied["transactionId"])
        self.assertTrue(read)
        self.assertEqual((root / "auth.json").read_text(), "unparseable fake auth sentinel")
        self.assertEqual({Path(file["path"]).name for file in plan["files"]}, {"config.toml", ".agent-hub-state.json"})

    def test_symlink_target_rejected_before_reads_or_writes(self):
        self.settings.unlink()
        elsewhere = self.home / "other.json"
        elsewhere.write_text('{"do":"not modify"}')
        try:
            self.settings.symlink_to(elsewhere)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaisesRegex(transaction.TransactionError, "symlink"):
            transaction.preview(engine.Hub(), ["pi"])
        self.assertEqual(elsewhere.read_text(), '{"do":"not modify"}')

    def test_cc_switch_rejected_without_opening_database(self):
        database = self.home / ".cc-switch" / "cc-switch.db"
        database.parent.mkdir()
        database.write_bytes(b"invalid database sentinel")
        with self.assertRaisesRegex(transaction.TransactionError, "SQLite"):
            transaction.preview(engine.Hub(), ["pi", "cc-switch"])
        plan = transaction.preview(engine.Hub(), ["claude"])
        transaction.apply(engine.Hub(), plan["planId"])
        self.assertEqual(database.read_bytes(), b"invalid database sentinel")
        self.assertTrue(plan["warnings"])

    def test_dsh_supported_yaml_subset_compiles_transactionally(self):
        from test_modelsync import dsh_fixture
        dsh_fixture(self.home)
        before = self.native_files()
        plan = transaction.preview(engine.Hub(), ["dsh", "dsh-desktop"])
        self.assertEqual(plan["targets"], ["dsh"])
        self.assertEqual(before, self.native_files())
        applied = transaction.apply(engine.Hub(), plan["planId"])
        self.assertTrue((self.home / ".dsh" / "settings.yaml").exists())
        transaction.restore(engine.Hub(), applied["transactionId"])
        self.assertEqual(before, self.native_files())

    def test_unsupported_yaml_is_rejected_instead_of_silently_dropped(self):
        path = self.home / ".dsh" / "settings.yaml"
        path.parent.mkdir()
        for text in ("first: 1\nfirst: 2\n", "prompt: |\n  important text\n", "base: &base\n  key: value\n", "invalid:\n   wrong: indentation\n"):
            with self.subTest(text=text):
                path.write_text(text)
                before = self.native_files()
                with self.assertRaises(transaction.TransactionError):
                    transaction.preview(engine.Hub(), ["pi", "dsh"])
                self.assertEqual(before, self.native_files())

    def test_bounded_plan_cache(self):
        with patch.object(transaction, "MAX_PLANS", 2):
            ids = [transaction.preview(engine.Hub(), ["pi"])["planId"] for _ in range(3)]
        self.assertEqual(len(transaction._PLANS), 2)
        self.assertNotIn(ids[0], transaction._PLANS)


if __name__ == "__main__":
    unittest.main()
