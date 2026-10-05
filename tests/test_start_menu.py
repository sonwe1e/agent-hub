"""在临时开始菜单中验证整理与撤回，不操作真实菜单。"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "Windows + PowerShell required")
class StartMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-hub-menu-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user = self.root / "menu-user"
        self.common = self.root / "menu-common"
        scripts = self.root / "scripts"
        scripts.mkdir()
        (self.root / "backups/start-menu").mkdir(parents=True)
        (self.root / "Launch GUI.vbs").write_text("' test only", encoding="utf-8")
        (self.root / "launch.py").write_text("# test only", encoding="utf-8")
        source = (ROOT / "scripts/organize-start-menu.ps1").read_text(encoding="utf-8")
        original = "$Roots = @{ user = [Environment]::GetFolderPath('Programs'); common = [Environment]::GetFolderPath('CommonPrograms') }"
        assert source.count(original) == 1
        # 仅替换临时副本中的已知文件夹路径，正式脚本仍只接受系统给出的开始菜单路径。
        source = source.replace(original, f"$Roots = @{{ user = '{self.user}'; common = '{self.common}' }}")
        self.script = scripts / "organize-start-menu.ps1"
        self.script.write_text(source, encoding="utf-8")
        shutil.copy2(ROOT / "scripts/start-menu-rules.json", scripts / "start-menu-rules.json")
        setup = f"""
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$ErrorActionPreference='Stop'
$user='{self.user}'; $common='{self.common}'; $shell=New-Object -ComObject WScript.Shell
function Add-Link($root,$relative,$argumentText) {{
  $path=Join-Path $root $relative
  New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
  $link=$shell.CreateShortcut($path)
  $link.TargetPath=Join-Path $env:WINDIR 'System32\\notepad.exe'
  $link.Arguments=$argumentText; $link.WorkingDirectory=$user; $link.Save()
}}
Add-Link $user 'Code.lnk' 'code'
Add-Link $user 'Visual Studio Code\\Visual Studio Code.lnk' 'code'
Add-Link $user 'WorkBuddy AI.lnk' 'workbuddy'
Add-Link $user 'Unknown.lnk' 'unknown'
Add-Link $user 'Startup\\auto.lnk' 'startup'
Add-Link $user 'Clash Verge\\Clash Verge.lnk' 'clash'
Add-Link $common 'Clash Verge.lnk' 'clash'
Add-Link $user '爱奇艺\\爱奇艺PC客户端.lnk' 'video'
Add-Link $user '爱奇艺\\卸载.lnk' 'uninstall-video'
'desktop metadata' | Set-Content -LiteralPath (Join-Path $user 'desktop.ini')
$records=@()
foreach($pair in @(@{{Scope='user';Root=$user}},@{{Scope='common';Root=$common}})){{
  foreach($file in Get-ChildItem -LiteralPath $pair.Root -Recurse -File -Force){{
    $link=if($file.Extension -eq '.lnk'){{$shell.CreateShortcut($file.FullName)}}else{{$null}}
    $records+=@{{Scope=$pair.Scope;Root=$pair.Root;Relative=$file.FullName.Substring($pair.Root.Length+1);FullName=$file.FullName;Extension=$file.Extension;Target=if($link){{$link.TargetPath}}else{{''}};Arguments=if($link){{$link.Arguments}}else{{''}};WorkingDirectory=if($link){{$link.WorkingDirectory}}else{{''}}}}
  }}
}}
@{{Records=$records}} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath '{self.root / 'backups/start-menu/inventory-before.json'}' -Encoding utf8
"""
        result = subprocess.run(["pwsh", "-NoProfile", "-Command", setup], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = self.hashes()

    def hashes(self):
        return {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for directory in (self.user, self.common) for path in directory.rglob("*") if path.is_file()}

    def run_menu(self, *args):
        result = subprocess.run(["pwsh", "-NoProfile", "-File", str(self.script), *args],
                                capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_preview_has_no_mutations(self):
        report = self.run_menu("-Preview")
        self.assertEqual(report["ArchiveDuplicates"], 2)
        self.assertEqual(self.hashes(), self.original)

    def test_apply_and_restore_are_lossless(self):
        report = self.run_menu()
        self.assertEqual(report["Failures"], [])
        self.assertEqual(report["ArchivedDuplicates"], 2)
        self.assertTrue((self.user / "AI 与模型/Agent Hub.lnk").is_file())
        self.assertTrue((self.user / "开发与终端/Visual Studio Code.lnk").is_file())
        self.assertTrue((self.user / "媒体与设计/维护与帮助/爱奇艺/卸载.lnk").is_file())
        self.assertTrue((self.user / "其他应用/Unknown.lnk").is_file())
        after = self.hashes()
        for relative in ("menu-user/Startup/auto.lnk", "menu-user/desktop.ini", "menu-common/Clash Verge.lnk"):
            self.assertEqual(after[relative], self.original[relative])
        restored = self.run_menu("-RestoreManifest", report["Manifest"])
        self.assertEqual(restored["Conflicts"], [])
        self.assertEqual(self.hashes(), self.original)

    def test_rerun_does_not_duplicate_and_empty_restore_is_safe(self):
        self.run_menu()
        expected = self.hashes()
        second = self.run_menu()
        self.assertEqual(second["Added"], 0)
        self.assertEqual(second["Moved"], 0)
        self.assertEqual(second["ArchivedDuplicates"], 0)
        self.assertEqual(self.hashes(), expected)
        report = self.run_menu("-RestoreManifest", second["Manifest"])
        self.assertEqual(report["Restored"], 0)
        self.assertEqual(report["Conflicts"], [])
        self.assertEqual(self.hashes(), expected)

    def test_restore_preserves_subsequent_edits(self):
        report = self.run_menu()
        changed = self.user / "其他应用/Unknown.lnk"
        changed.write_bytes(b"user modified shortcut")
        result = subprocess.run(["pwsh", "-NoProfile", "-File", str(self.script), "-RestoreManifest", report["Manifest"]],
                                capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1)
        conflicts = json.loads(result.stdout)["Conflicts"]
        self.assertTrue(any("Unknown.lnk" in text for text in conflicts))
        self.assertEqual(changed.read_bytes(), b"user modified shortcut")
        self.assertFalse((self.user / "Unknown.lnk").exists())


if __name__ == "__main__":
    unittest.main()
