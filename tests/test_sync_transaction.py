"""Native sync transactions use temporary HOME fixtures and never contact providers."""
from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from support import isolated, engine
from hub import dump_yaml as hub_dump_yaml
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
        cfg["providers"]["deepseek"]["agents"]["codex"] = {
            "api": "openai-responses", "apiKeyEnv": "TEST_CODEX_API_KEY"}
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
        cfg["providers"]["deepseek"]["agents"]["codex"]["api"] = "openai-chat-completions"
        self.save(cfg)
        before = self.native_files()
        with self.assertRaisesRegex(transaction.TransactionError, "Cannot compile codex"):
            transaction.preview(engine.Hub(), ["pi", "codex"])
        self.assertEqual(before, self.native_files())
        self.assertFalse(engine.HUB_BACKUP.exists())

    def test_digit_leading_strings_are_not_treated_as_numbers(self):
        """UUID 形态的密钥（3fa85f64-...）按 YAML 规范就是字符串。

        真实事故：Firework 的 API Key 以数字开头，保守校验把它判成“歧义标量”，
        导致含该密钥的 dsh 同步整组被拒绝。真正必须加引号的是进制、科学计数法
        与六十进制这类会被读成数值的形态。
        """
        subset = transaction._yaml_subset
        self.assertEqual(subset("key: 3fa85f64-5717-4562-b3fc-2c963f66afa6")["key"],
                         "3fa85f64-5717-4562-b3fc-2c963f66afa6")
        self.assertEqual(subset("key: 1")["key"], 1)
        self.assertEqual(subset("key: 1.5")["key"], 1.5)
        for ambiguous in ("007", "0x1F", "1e5", "1:2:3", "-1", "yes"):
            with self.subTest(ambiguous=ambiguous), self.assertRaises(ValueError):
                subset(f"key: {ambiguous}")

    def test_mapping_keys_hub_and_clients_actually_use(self):
        """真实客户端配置里出现过的键形必须能读。

        - 带引号的键（MiniMax 写的 '1000000': hint）；
        - 含 [ ] / 的模型 ID 键（openrouter 的 anthropic/xxx[1m]）；
        - off 这类词作为键（hub 自己写的 reasoningEfforts）。
        两个解析器必须得到同一个字符串键，否则安全校验会误判配置不受支持。
        """
        subset = transaction._yaml_subset
        self.assertEqual(subset("'1000000': higher_usage")["1000000"], "higher_usage")
        self.assertEqual(subset('"1000000": higher_usage')["1000000"], "higher_usage")
        model_key = "anthropic/stealth/space-bunny-alpha[1m]"
        self.assertEqual(subset(f"{model_key}:\n  name: x")[model_key]["name"], "x")
        self.assertEqual(subset("off: null")["off"], None)
        # 数字键加引号写出：两个读取端都读成同一个字符串键，且手写的
        # `'1000000':` / `"1000000":` 两种形态都必须能读
        for key in ("1000000", "-1", "1e5"):
            with self.subTest(key=key):
                dumped = hub_dump_yaml({key: "higher_usage"})
                self.assertEqual(subset(dumped), {key: "higher_usage"})
                self.assertEqual(engine.parse_yaml(dumped), {key: "higher_usage"})
        # 含 ": " 或以指示符开头的键仍然要求引号
        for bad in (r'"a\qb"', '"unterminated', "'it''s'"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                subset(bad)
        # 写入的引号键能被读回
        self.assertEqual(subset(hub_dump_yaml({"a: b": 1})), {"a: b": 1})

    def test_quoted_scalars_round_trip_windows_paths_and_escapes(self):
        """写入端用 json.dumps 写双引号字符串，读取端必须认这些转义。

        真实故障：omp 的 shellPath 是 Windows 路径，hub 写出双引号带反斐极转义的字符串，
        而保守校验拒绝任何含反斐极的引号字符串，整个同步被阾止。
        """
        subset = transaction._yaml_subset
        self.assertEqual(subset(r'k: "C:\\Program Files\\Git\\bash.exe"')["k"],
                         r"C:\Program Files\Git\bash.exe")
        self.assertEqual(subset(r"k: 'C:\Program Files'")["k"], r"C:\Program Files")
        self.assertEqual(subset(r'k: "a\"b"')["k"], 'a"b')
        self.assertEqual(subset(r'k: "line\nbreak"')["k"], "line\nbreak")
        self.assertEqual(subset(r'k: "\u4e2d"')["k"], "中")
        # 两个解析器对引号内容必须得到同一个值
        for raw in (r'"C:\\Program Files"', r"'C:\Program Files'", r'"a\"b"'):
            with self.subTest(raw=raw):
                self.assertEqual(subset(f"k: {raw}")["k"], engine.parse_yaml(f"k: {raw}")["k"])
        # 写出来的内容能被安全读回（否则 omp 这类配置永远同步不了）
        value = {"shellPath": r"C:\Program Files\Git\bin\bash.exe", "note": 'has "quotes"', "flag": "true", "num": "123"}
        rendered = engine.dump_yaml(value)
        self.assertEqual(subset(rendered), value)
        self.assertEqual(engine.parse_yaml(rendered), value)
        # 无法确定的转义依然拒绝
        for bad in ('"a\qb"', '"unterminated', "'it''s'"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                subset(f"k: {bad}")

    def test_flow_indicator_scalars_are_quoted_and_read_back(self):
        """问题 C：模型 ID 里的 [ ] 必须触发引号，否则自己的输出读不回来。

        openrouter 的模型 ID 是 anthropic/stealth/space-bunny-alpha[1m]。
        加强「必须加引号」规则时漏掉 [ ] , 等字符，就会裸写出去，安全校验读回来
        变成一个坏标量（或被 _yaml_subset 直接拒绝），dsh 的 cordis.patch.yml 同步
        因此失败。这里把「写入 → 读回」整条链路钉死。
        """
        subset = transaction._yaml_subset
        for value in ("anthropic/stealth/space-bunny-alpha[1m]", "a,b", "m[1m]",
                      "has #comment", "a: b", "C:\\path\\to\\x", "1000000", "off"):
            with self.subTest(value=value):
                rendered = engine.dump_yaml({"id": value})
                self.assertEqual(subset(rendered)["id"], value)
                self.assertEqual(engine.parse_yaml(rendered)["id"], value)
        # 含流式指示符的值必须真的带引号（不是靠运气读回）
        for value in ("model[1m]", "a,b", 'has "quotes"', "has #comment"):
            with self.subTest(value=value):
                rendered = engine.dump_yaml({"id": value})
                self.assertTrue(rendered.strip().endswith('"') or "'" in rendered,
                                f"{value!r} 被裸写: {rendered!r}")

    def test_hash_inside_quotes_is_content_not_a_comment(self):
        """引号内的 # 是内容：旧实现按「空格+#」一刀切，会把 "has #comment" 截成 "has。"""
        subset = transaction._yaml_subset
        value = "has #comment"
        rendered = engine.dump_yaml({"id": value})
        self.assertEqual(engine.parse_yaml(rendered)["id"], value)
        self.assertEqual(subset(rendered)["id"], value)
        # 真正的注释仍然被剥掉
        self.assertEqual(engine.parse_yaml("k: v  # real comment"), {"k": "v"})
        self.assertEqual(engine.parse_yaml("# whole line comment"), None)
        # 引号键里的冒号/盘符不再被 partition(":") 从中间切断
        self.assertEqual(engine.parse_yaml('"a:b": 1'), {"a:b": 1})
        self.assertEqual(engine.parse_yaml('"C:\\\\path": 1'), {r"C:\path": 1})
        self.assertEqual(engine.parse_yaml('"a:b": 1')["a:b"], subset('"a:b": 1')["a:b"])

    def test_multiline_and_special_scalars_survive_a_round_trip(self):
        """多行字符串必须加引号：裸写会被下一行当成新语句，读回来只剩第一行。"""
        subset = transaction._yaml_subset
        value = "line one\nline two"
        rendered = engine.dump_yaml({"k": value})
        self.assertNotIn("\n", rendered.strip())
        self.assertEqual(engine.parse_yaml(rendered)["k"], value)
        self.assertEqual(subset(rendered)["k"], value)
        # 指示符 / YAML 词开头的值必须加引号，读回来仍是字符串
        for value in ("-dash", "?q", ":c", "#leading", " leading", "trailing ",
                      "true", "false", "null", "~", "12", "1.5", "yes", "on", "off"):
            with self.subTest(value=value):
                rendered = engine.dump_yaml({"k": value})
                self.assertEqual(engine.parse_yaml(rendered)["k"], value)
                self.assertEqual(subset(rendered)["k"], value)
        # 数字本身（不是字符串）必须是数字，包括裸写会被校验器拒绝的负数
        for number in (-3, -3.25, 0, 12, 128000, 1e30, 1e-7):
            with self.subTest(number=number):
                rendered = engine.dump_yaml({"k": number})
                self.assertEqual(engine.parse_yaml(rendered)["k"], number)
                self.assertEqual(subset(rendered)["k"], number)

    def test_parser_and_subset_agree_on_every_target_shaped_document(self):
        """两个解析器必须同进同退：安全校验靠它们比对，分歧就是误判。"""
        subset = transaction._yaml_subset
        documents = [
            "models:\n  - id: \"anthropic/stealth/space-bunny-alpha[1m]\"\n    name: Space Bunny\n",
            "reasoningEfforts:\n  off: null\n  max: null\n",
            "provider:\n  minimax:\n    models:\n      MiniMax-M3:\n        contextWindowOptionHints:\n          '1000000': higher_usage\n",
            'shellPath: "C:\\\\Program Files\\\\Git\\\\bin\\\\bash.exe"\n',
            "items:\n  - a\n  - b\n",
            "empty: {}\nlist: []\n",
        ]
        for text in documents:
            with self.subTest(text=text):
                self.assertEqual(engine.parse_yaml(text), subset(text))
                parsed = subset(text)
                rendered = engine.dump_yaml(parsed)
                self.assertEqual(subset(rendered), parsed)
                self.assertEqual(engine.parse_yaml(rendered), parsed)

    def test_keys_are_quoted_whenever_they_would_read_back_wrong(self):
        """写入端产出的每个键都必须能被两个读取端读回同一个字符串。

        对抗性 fuzz 暴露的三类键，写入端曾经裸写、读取端读出坏结果：
        - 列表项映射里键含冒号：`- a:b: {k: v}` 会被切成 a -> "b: {k: v}"；
        - 键以 `#` 开头：`#lead: x` 整行被当成注释，键直接消失；
        - 键含换行 / 制表：裸写会把一行拆成两行。
        另外半截引号的键必须报错，而不是静默读出一个坏键。
        """
        subset = transaction._yaml_subset
        cases = [
            [{"a:b": {"k": "v"}}],
            {"#lead": "- x"},
            {"a": {"#": "v"}},
            {"a\nb": "v"},
            {"a\tb": "v"},
            {"a:b": [], "#lead": "- x"},
            {"#": {"#": "#"}},
        ]
        for value in cases:
            with self.subTest(value=value):
                rendered = engine.dump_yaml(value)
                self.assertEqual(engine.parse_yaml(rendered), value, rendered)
                self.assertEqual(subset(rendered), value, rendered)
        # 半截引号的键：两个读取端都必须拒绝，不能各自读出一个坏键
        for text in ('"a: b', "a\": 1", '"unterminated: 1'):
            with self.subTest(text=text):
                raised = []
                for reader in (engine.parse_yaml, subset):
                    try:
                        reader(text)
                    except ValueError:
                        raised.append(True)
                self.assertTrue(raised, f"{text!r} 两个读取端都接受了半截引号的键")
        # 键里含引号但不是引号开头的（a"）是合法裸键：必须能原样往返，不能被误拒
        odd = {'a"': 1}
        rendered = engine.dump_yaml(odd)
        self.assertEqual(engine.parse_yaml(rendered), odd)
        self.assertEqual(subset(rendered), odd)


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

    def test_restore_respects_platform_permission_semantics(self):
        """Windows 比较只读位；POSIX 的任何权限位变化仍拒绝恢复。"""
        plan = transaction.preview(engine.Hub(), ["pi"])
        applied = transaction.apply(engine.Hub(), plan["planId"])
        settings = self.settings

        if os.name != "nt":
            original_mode = stat.S_IMODE(settings.stat().st_mode)
            settings.chmod(0o640)
            with self.assertRaisesRegex(transaction.TransactionError, "changed after apply"):
                transaction.restore(engine.Hub(), applied["transactionId"])
            settings.chmod(original_mode)
            transaction.restore(engine.Hub(), applied["transactionId"])
            self.assertEqual(settings.read_bytes(), self.original_settings)
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
