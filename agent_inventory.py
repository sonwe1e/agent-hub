"""Installation identity, source-specific update checks and verified update jobs.

No shell command supplied by a browser is ever executed. An installation id is a
digest of its agent, kind and absolute entry point. Detection never updates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

AGENTS = ("claude", "codex", "pi", "dsh")
LABELS = {"claude": "Claude Code", "codex": "Codex", "pi": "pi", "dsh": "dsh"}
PACKAGES = {"claude": ("@anthropic-ai/claude-code",), "codex": ("@openai/codex",),
            "pi": ("@earendil-works/pi-coding-agent", "@mariozechner/pi-coding-agent"),
            "dsh": ("@deepseek-ai/dsh",)}
VERSION = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)")
CLAUDE_RELEASES = "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases"


def run(args, timeout=20, **kwargs):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    p = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags, **kwargs)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def powershell(script):
    code, out, _ = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], 25)
    if code or not out:
        return []
    value = json.loads(out)
    return value if isinstance(value, list) else [value]


def ident(agent, kind, path):
    return hashlib.sha256(f"{agent}|{kind}|{str(path).lower()}".encode()).hexdigest()[:20]


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def version_of(args):
    try:
        code, out, _ = run([*args, "--version"], timeout=12)
        match = VERSION.search(out)
        return match[1] if code == 0 and match else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def asar_app_version(path):
    """Read Electron application's own package, never Electron's `version` file."""
    try:
        with path.open('rb') as f:
            _, header_size, _, json_size = struct.unpack('<IIII', f.read(16))
            if not 0 < json_size <= header_size <= 16 * 1024 * 1024:
                return None
            header = json.loads(f.read(json_size))
            entry = header['files']['package.json']
            if not 0 < entry['size'] < 1024 * 1024:
                return None
            f.seek(8 + header_size + int(entry['offset']))
            package = json.loads(f.read(entry['size']))
            version = package.get('version')
            return version if isinstance(version, str) and VERSION.fullmatch(version) else None
    except (OSError, ValueError, KeyError, struct.error):
        return None


def newer(latest, current):
    """Return None rather than guessing an ordering between prerelease channels."""
    if latest == current:
        return False
    if not latest or not current:
        return None
    a, b = VERSION.fullmatch(latest), VERSION.fullmatch(current)
    if not a or not b:
        return None
    aa, bb = tuple(map(int, latest.split('-')[0].split('+')[0].split('.'))), tuple(map(int, current.split('-')[0].split('+')[0].split('.')))
    if aa != bb:
        return aa > bb
    if '-' not in latest and '-' in current:
        return True
    return None


class Inventory:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.lock = threading.RLock()
        self.instances = {}
        self.checks = {}
        self.jobs = {}
        self.checked_at = None
        self.diagnostics = []

    def _entry(self, agent, path, *, kind="cli", source="unknown", version=None, launch=None, **extra):
        path = Path(path).absolute()
        return {"id": ident(agent, kind, path), "agent": agent, "kind": kind, "path": str(path),
                "source": source, "version": version, "launch": launch or [str(path)],
                "activePath": False, "running": False, "updateState": "not-checked", **extra}

    def detect(self):
        found = {}
        diagnostics = []
        path_dirs = list(dict.fromkeys(os.environ.get("PATH", "").split(os.pathsep)))
        for agent in AGENTS:
            entries = []
            for directory in path_dirs:
                # PowerShell npm shims and cmd shims launch the same package. Prefer
                # the cmd/exe entry; no execution-policy changes are necessary.
                for suffix in ((".exe", ".cmd", "") if os.name == "nt" else ("",)):
                    p = Path(directory) / (agent + suffix)
                    if not p.is_file():
                        continue
                    package = None
                    for pkg in PACKAGES[agent]:
                        meta = p.parent / "node_modules" / pkg / "package.json"
                        data = read_json(meta)
                        if data.get("name") != pkg:
                            continue
                        # Confirm this wrapper actually names this package.
                        text = p.read_text(encoding="utf-8", errors="ignore") if p.suffix != '.exe' else ''
                        if pkg.replace('/', '\\').lower() not in text.replace('/', '\\').lower():
                            continue
                        bins = data.get("bin")
                        binary = bins.get(agent) if isinstance(bins, dict) else bins
                        node = p.parent / "node.exe"
                        node = str(node) if node.is_file() else shutil.which("node")
                        launch = [node, str(meta.parent / binary)] if node and isinstance(binary, str) else None
                        if not launch:
                            continue
                        package = self._entry(agent, p, source="npm", version=version_of(launch), launch=launch,
                                              package=pkg, prefix=str(p.parent), manifest=str(meta), channel="latest")
                        break
                    if package:
                        if not any(e.get("manifest") == package["manifest"] for e in entries):
                            entries.append(package)
                        continue
                    # Small forwarding cmd files, such as WindowsApps/claude.cmd.
                    actual = p
                    if p.suffix.lower() == '.cmd' and p.stat().st_size < 2048:
                        wrapper = p.read_text(encoding="utf-8", errors="ignore")
                        match = re.search(r'"([A-Za-z]:[^"\r\n]+\.exe)"\s+%\*', wrapper)
                        if match and Path(match[1]).is_file():
                            actual = Path(match[1])
                    source = "unknown"
                    normalized_path = str(actual).replace('\\', '/').lower()
                    if agent == 'codex' and any(part in normalized_path for part in ('/openai/codex/bin/', '/programs/openai/codex/bin/')):
                        source = 'codex-app-managed'
                    if agent == "claude" and actual.parent == self.home / '.local/bin':
                        source = "claude-native"
                    bundled_dsh = agent == "dsh" and 'deepseek harness' in str(p).lower() and 'runtime' in str(p).lower()
                    if bundled_dsh:
                        source = "desktop-bundled"
                    if not any(e["path"].lower() == str(actual).lower() for e in entries):
                        launch = [str(actual)]
                        if actual.suffix.lower() == '.cmd' and os.name == 'nt':
                            # Internal, discovered absolute path, no browser arguments.
                            launch = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', str(actual)]
                        channel = read_json(self.home / '.claude/settings.json').get('autoUpdatesChannel', 'latest') if agent == 'claude' else None
                        entries.append(self._entry(agent, actual, source=source, version=version_of(launch),
                                                   launch=launch, entryPath=str(p), channel=channel))
            if entries:
                entries[0]["activePath"] = True
            for e in entries:
                found[e['id']] = e
        if os.name == "nt":
            try:
                procs = powershell("Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(claude|codex|DeepSeek Harness|Claude)\\.exe$' } | Select-Object ExecutablePath,ProcessId | ConvertTo-Json -Compress")
                for e in found.values():
                    e['running'] = any(str(p.get('ExecutablePath') or '').lower() == e['path'].lower() for p in procs)
                apps = powershell("Get-AppxPackage | Where-Object { $_.Name -match '^(OpenAI.Codex|Claude|Anthropic.Claude)$' } | Select-Object Name,Version,InstallLocation | ConvertTo-Json -Compress")
                # npm Codex launches a platform package's vendor binary. It is
                # only running when that exact package directory owns the image.
                for e in found.values():
                    if e['source'] == 'npm':
                        package_root = str(Path(e['manifest']).parent).lower() + '\\'
                        e['running'] = any(str(p.get('ExecutablePath') or '').lower().startswith(package_root) for p in procs)
                for app in apps:
                    agent = 'codex' if 'Codex' in app['Name'] else 'claude'
                    e = self._entry(agent, app['InstallLocation'], kind='desktop', source='msix', version=app['Version'],
                                    launch=[], package=app['Name'], updateNote='Microsoft Store 安装：请在 Store 或应用内更新；Hub 不以 CLI 版本代替桌面版。')
                    e['running'] = any(str(p.get('ExecutablePath') or '').lower().startswith(e['path'].lower() + '\\') for p in procs)
                    found[e['id']] = e
                local = Path(os.environ.get('LOCALAPPDATA', self.home / 'AppData/Local'))
                for agent, rel, exe in [('dsh', 'DeepSeek Harness', 'DeepSeek Harness.exe'), ('claude', 'Claude', 'Claude.exe')]:
                    base = local / 'Programs' / rel
                    binary = base / exe
                    if not binary.is_file():
                        continue
                    escaped = str(binary).replace("'", "''")
                    versions = powershell(f"(Get-Item -LiteralPath '{escaped}').VersionInfo | Select-Object ProductVersion | ConvertTo-Json -Compress")
                    desktop_version = asar_app_version(base / 'resources/app.asar')
                    e = self._entry(agent, binary, kind='desktop', source='desktop-updater', launch=[],
                                    version=desktop_version or (versions[0].get('ProductVersion') if versions else None),
                                    updateNote='使用桌面应用内的检查更新与重启。独立安装器更新尚未自动执行。')
                    e['running'] = any(str(p.get('ExecutablePath') or '').lower() == str(binary).lower() for p in procs)
                    if agent == 'dsh':
                        feed = base / 'resources/app-update.yml'
                        if feed.exists():
                            text = feed.read_text(encoding='utf-8')
                            url = re.search(r'^url:\s*(https://download\.deepseek\.com/[^\s]+)', text, re.M)
                            channel = re.search(r'^channel:\s*([\w-]+)', text, re.M)
                            if url:
                                e['feed'] = url[1] + (channel[1] if channel else 'latest') + '.yml'
                    found[e['id']] = e
            except (OSError, ValueError, subprocess.TimeoutExpired):
                diagnostics.append('Windows 进程或桌面包查询失败：运行实例/桌面发现不完整。')
        with self.lock:
            self.instances = found
            self.diagnostics = diagnostics
            self.checked_at = time.time()
        return self.view()

    def view(self):
        with self.lock:
            rows = []
            for raw in self.instances.values():
                e = {k: v for k, v in raw.items() if k != 'launch'}
                e.update(self.checks.get(e['id'], {}))
                e['canUpdate'] = e.get('updateState') == 'available' and e['source'] in ('npm', 'claude-native')
                rows.append(e)
            return {'instances': rows, 'detectedAt': self.checked_at, 'diagnostics': self.diagnostics,
                    'jobs': list(self.jobs.values())}

    def check(self, instance_id):
        with self.lock:
            e = dict(self.instances.get(instance_id) or {})
        if not e:
            raise ValueError('安装实例已失效，请重新检测')
        state = {'checkedAt': time.time(), 'latestVersion': None, 'updateState': 'source-unknown'}
        try:
            latest = None
            if e['source'] == 'npm':
                url = 'https://registry.npmjs.org/' + e['package'].replace('/', '%2f') + '/latest'
                with urllib.request.urlopen(url, timeout=12) as response:
                    latest = json.load(response)['version']
                state['releaseSource'] = url
            elif e['source'] == 'claude-native':
                channel = e.get('channel')
                if channel not in ('stable', 'latest'):
                    raise ValueError('Claude 发布频道无法确认')
                url = CLAUDE_RELEASES + '/' + channel
                with urllib.request.urlopen(url, timeout=12) as response:
                    latest = response.read(128).decode().strip()
                state['releaseSource'] = url
            elif e.get('feed'):
                with urllib.request.urlopen(e['feed'], timeout=12) as response:
                    text = response.read(65536).decode()
                m = re.search(r'^version:\s*[\'\"]?([^\s\'\"]+)', text, re.M)
                latest = m[1] if m else None
                if not latest:
                    raise ValueError('发布 feed 未包含版本')
                state['releaseSource'] = e['feed']
            else:
                state['updateState'] = 'manual' if e['source'] != 'unknown' else 'source-unknown'
                state['message'] = e.get('updateNote') or ('由 Codex 桌面应用维护此运行时，请在桌面应用中检查更新。' if e['source']=='codex-app-managed' else '安装来源未确认，不能安全选择更新命令。')
            if latest:
                if not VERSION.fullmatch(latest):
                    raise ValueError('发布源返回无效版本')
                compare = newer(latest, e.get('version'))
                state.update(latestVersion=latest, updateState='available' if compare else 'current' if compare is False else 'unverified',
                             message='发现可用更新' if compare else '对应频道没有更新版本' if compare is False else '版本顺序无法确认，请在原安装渠道核对')
                if compare and e['source'] not in ('npm', 'claude-native'):
                    state['message'] = '发现更新；需要在桌面应用内执行更新并重启。'
        except Exception as exc:
            state.update(updateState='check-failed', message=f'发布渠道检查失败：{type(exc).__name__}。请重试。')
        with self.lock:
            self.checks[instance_id] = state
        return state

    def start_update(self, instance_id):
        with self.lock:
            e = dict(self.instances.get(instance_id) or {})
            check = dict(self.checks.get(instance_id) or {})
            if not e or check.get('updateState') != 'available' or e['source'] not in ('npm', 'claude-native'):
                raise ValueError('先检查该安装实例；只有已确认的更新与来源才允许执行')
            if time.time() - check.get('checkedAt', 0) > 1800:
                raise ValueError('更新检查已过期，请重新检查')
            if any(j['state'] == 'running' for j in self.jobs.values()):
                raise ValueError('已有更新正在运行')
            job_id = str(time.time_ns())
            job = {'id': job_id, 'instanceId': instance_id, 'agent': e['agent'], 'state': 'running',
                   'stage': '准备更新', 'messages': [], 'expectedVersion': check['latestVersion'], 'startedAt': time.time()}
            self.jobs[job_id] = job
        threading.Thread(target=self._update, args=(e, check, job), daemon=True).start()
        return dict(job)

    def _update(self, e, check, job):
        try:
            # Re-detect before mutation: PATH drift must never redirect an update.
            if e['source'] == 'npm':
                meta = read_json(Path(e['manifest']))
                if meta.get('name') != e['package'] or meta.get('version') != e['version']:
                    raise ValueError('安装实例已改变，请重新检测')
                npm_cli = Path(shutil.which('node') or '').parent / 'node_modules/npm/bin/npm-cli.js'
                if not npm_cli.is_file():
                    raise ValueError('无法确认 npm 执行入口；请用原安装渠道更新')
                args = [shutil.which('node'), str(npm_cli), 'install', '--global', '--prefix', e['prefix'],
                        e['package'] + '@' + check['latestVersion'], '--no-audit', '--no-fund']
            else:
                if version_of(e['launch']) != e['version']:
                    raise ValueError('原生安装实例已改变，请重新检测')
                args = [*e['launch'], 'update']
            job['stage'] = '安装中（由原渠道更新器执行）'
            code, out, err = run(args, timeout=300)
            # Avoid persisting npm output (may contain registry credentials).
            job['messages'].append(f'更新器退出码：{code}')
            if code:
                reason = '文件占用或需要权限' if any(x in (out + err).lower() for x in ('eperm', 'eacces', 'access is denied', 'being used')) else '更新器失败'
                raise ValueError(reason + '；请关闭对应客户端后重试或使用原渠道。')
            job['stage'] = '核验同一入口的启动与版本'
            actual = version_of(e['launch'])
            job['actualVersion'] = actual
            if actual != check['latestVersion']:
                raise ValueError('更新命令成功，但同一入口版本未达到检查结果；更新未确认，请重新检测。')
            if e['source'] == 'npm' and read_json(Path(e['manifest'])).get('version') != actual:
                raise ValueError('启动版本与安装包版本不一致，更新未确认。')
            job.update(state='succeeded', stage='已核验更新', restartRequired=True)
            job['messages'].append('对应入口可启动且版本符合预期；已有会话请重启客户端。')
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else f'{type(exc).__name__}：更新失败或超时，请重新检测。'
            job.update(state='failed', stage='更新未完成', messages=[*job['messages'], message])
        finally:
            job['finishedAt'] = time.time()
            self.detect()
            if job['state'] == 'succeeded':
                self.checks[e['id']] = {'updateState': 'current', 'latestVersion': job['actualVersion'], 'checkedAt': time.time(), 'message': '同一安装入口已通过更新后验证'}
