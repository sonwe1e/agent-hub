"""Installation identity, source-specific update checks and verified update jobs.

No shell command supplied by a browser is ever executed. An installation id is a
digest of its agent, kind and absolute entry point. Detection never updates.

除了单实例的 check / start_update，这里还提供「一键检测」check_all 与
「一键更新」update_all。两者的进度都收敛到同一个 bulk 状态对象上，由
GET /api/agents 直接返回给界面，避免前端自己拼装进度。
"""
from __future__ import annotations

import copy
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

AGENTS = ("claude", "codex", "pi", "dsh", "opencode")
# 各客户端读版本号的超时（秒）。OpenCode 是 Bun 单文件，启动要 10 秒以上。
_VERSION_TIMEOUT = {"opencode": 40}
LABELS = {"claude": "Claude Code", "codex": "Codex", "pi": "pi", "dsh": "dsh",
          "opencode": "OpenCode"}
PACKAGES = {"claude": ("@anthropic-ai/claude-code",), "codex": ("@openai/codex",),
            "pi": ("@earendil-works/pi-coding-agent", "@mariozechner/pi-coding-agent"),
            "dsh": ("@deepseek-ai/dsh",), "opencode": ("@opencode/cli",)}
VERSION = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)")
CLAUDE_RELEASES = "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases"

# ---------------------------------------------------------------------------
# 批量检测/更新：常量集中在这里，界面与测试都依赖这几个取值。
# ---------------------------------------------------------------------------

# bulk.state 只有这三个取值，前端据此决定按钮与轮询节奏；不要新增状态。
BULK_IDLE, BULK_CHECKING, BULK_UPDATING = "idle", "checking", "updating"
# 并发检查的上限。发布渠道是 HTTP 查询，4 路已经能把一次全量检测压到 1 秒内，
# 再高只会让 registry 侧限流。
BULK_CHECK_WORKERS = 4
# 检查结果的有效期。超过就必须重查：拿半小时前的结论去装版本等于蒙。
CHECK_TTL = 1800.0
# 只有这两类来源由 Hub 亲自执行更新；其余只能在原应用内更新。
BULK_UPDATE_SOURCES = ("npm", "claude-native")
# 不支持自动更新时的具体原因（按来源）。写清楚「去哪儿更新」而不是笼统失败。
SKIP_REASONS = {
    "msix": "Microsoft Store 安装，需在 Store 或应用内更新",
    "desktop-updater": "桌面应用内检查更新",
    "codex-app-managed": "由 Codex 桌面应用维护",
    "desktop-bundled": "随桌面应用安装，请更新桌面应用",
    "unknown": "安装来源未确认，不能安全选择更新命令",
}


class BulkBusy(RuntimeError):
    """已有批量任务在运行。

    继承 RuntimeError 而不是 ValueError：server.py 把它翻译成 409，
    与「参数不合法」的 400 区分开；界面可据此只提示「正在跑」而不报错。
    """

    def __init__(self, message, bulk=None):
        super().__init__(message)
        self.bulk = dict(bulk or {})


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


def _idle_bulk():
    """bulk 状态对象的空位。字段与 brief 里的形状严格一致。"""
    return {"state": BULK_IDLE, "checked": 0, "total": 0, "step": "",
            "updated": [], "failed": [], "skipped": [],
            "startedAt": 0, "finishedAt": 0}


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def version_of(args, timeout=12):
    """读客户端版本。

    OpenCode（Bun 打包的单文件）`--version` 实测要 10–12 秒，卡在 12 秒上限的
    边缘上，经常被判超时而显示为未知。调用方按 agent 放宽，别改这里的默认值——
    其余客户端 1 秒内就返回，统一提高只会拖慢整次检测。
    """
    try:
        code, out, _ = run([*args, "--version"], timeout=timeout)
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
        # 批量检测/更新的唯一状态对象，随 GET /api/agents 一起返回。
        self.bulk = _idle_bulk()

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
                        launch = None
                        if isinstance(binary, str):
                            target = meta.parent / binary
                            if target.suffix.lower() == ".exe":
                                # OpenCode 这类 npm 包直接分发单文件可执行程序（PE 头），
                                # 交给 node 跑会报 "MZ" 语法错误；直接执行即可。
                                launch = [str(target)]
                            elif node:
                                launch = [node, str(target)]
                        if not launch:
                            continue
                        package = self._entry(agent, p, source="npm", version=version_of(launch, timeout=_VERSION_TIMEOUT.get(agent, 12)), launch=launch,
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
                        entries.append(self._entry(agent, actual, source=source, version=version_of(launch, timeout=_VERSION_TIMEOUT.get(agent, 12)),
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
                    'jobs': list(self.jobs.values()), 'bulk': copy.deepcopy(self.bulk)}

    def bulk_state(self):
        """返回 bulk 状态快照。拷贝出去，调用方改不坏内部状态。"""
        with self.lock:
            return copy.deepcopy(self.bulk)

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
            if time.time() - check.get('checkedAt', 0) > CHECK_TTL:
                raise ValueError('更新检查已过期，请重新检查')
            if self.bulk['state'] != BULK_IDLE:
                # 批量任务正在跑，单个更新会和它抢同一个 npm prefix / lock。
                raise ValueError('正在批量更新，请等待完成后再单独更新')
            if any(j['state'] == 'running' for j in self.jobs.values()):
                raise ValueError('已有更新正在运行')
            job_id = str(time.time_ns())
            job = {'id': job_id, 'instanceId': instance_id, 'agent': e['agent'], 'state': 'running',
                   'stage': '准备更新', 'messages': [], 'expectedVersion': check['latestVersion'], 'startedAt': time.time()}
            self.jobs[job_id] = job
        threading.Thread(target=self._update, args=(e, check, job), daemon=True).start()
        return dict(job)

    # -- 批量检测 -----------------------------------------------------------

    def _groups(self):
        """把实例按「同一份 npm manifest 视为同一个安装」分组去重。

        PATH 里常见的 claude.cmd / claude.exe shim 指向同一个包，逐个检查会
        对同一个 registry 地址重复请求，也让「5 个 Agent」的进度虚高。
        每组第一个元素是代表实例：优先当前 PATH 上生效的那一份，其次按
        路径排序，保证结果不随字典顺序抖动。
        """
        with self.lock:
            entries = [dict(e) for e in self.instances.values()]
        grouped = {}
        for e in entries:
            manifest = str(e.get('manifest') or '').lower()
            key = (e.get('agent'),
                   manifest or f"{e.get('source')}|{e.get('kind')}|{e.get('path')}".lower())
            grouped.setdefault(key, []).append(e)
        for items in grouped.values():
            items.sort(key=lambda i: (not i.get('activePath'), str(i.get('path')).lower()))
        return grouped

    def _claim(self, state, total, step):
        """占用 bulk 状态；已在跑就抛 BulkBusy（server 翻成 409）。"""
        with self.lock:
            if self.bulk['state'] != BULK_IDLE:
                raise BulkBusy('已有批量任务正在运行，请等待完成', self.bulk)
            self.bulk = {**_idle_bulk(), 'state': state, 'total': total, 'step': step,
                         'startedAt': time.time()}
            return self.bulk['startedAt']

    def check_all(self, max_workers=BULK_CHECK_WORKERS, *, claimed=False):
        """并发检查所有实例的发布渠道，同步返回汇总。

        claimed=True 表示调用方（start_check_all）已经占用了 bulk 状态。
        """
        groups = self._groups()
        items = list(groups.values())
        if not claimed:
            self._claim(BULK_CHECKING, len(items), '正在查询发布渠道')
        else:
            with self.lock:
                self.bulk['total'] = len(items)
                self.bulk['checked'] = 0
                self.bulk['step'] = '正在查询发布渠道'
        started = self.bulk['startedAt'] or time.time()
        try:
            states, failures = self._run_checks(items, max_workers)
        except BaseException as exc:  # 占位必须释放，否则之后永远「有任务在跑」
            with self.lock:
                self.bulk.update(state=BULK_IDLE, finishedAt=time.time(),
                                 step=f"检查中断：{type(exc).__name__}")
            raise
        finished = time.time()
        ok = len(items) - len(failures)
        with self.lock:
            self.bulk.update(state=BULK_IDLE, checked=len(items), total=len(items),
                             failed=failures, finishedAt=finished,
                             step=f"检查完成：{ok} 个渠道已确认" + (f"，{len(failures)} 个失败" if failures else ""))
            snapshot = copy.deepcopy(self.bulk)
        return {'ok': True, 'checked': len(items), 'total': len(items),
                'elapsed': round(finished - started, 3), 'results': states,
                'failures': failures, 'bulk': snapshot}

    def start_check_all(self, max_workers=BULK_CHECK_WORKERS):
        """立即返回、后台并发检查。进度写进 bulk，前端轮询 GET /api/agents。"""
        total = len(self._groups())
        self._claim(BULK_CHECKING, total, '正在查询发布渠道')
        threading.Thread(target=self.check_all, args=(max_workers,),
                         kwargs={'claimed': True}, daemon=True).start()
        return self.bulk_state()

    def _run_checks(self, items, max_workers):
        """线程池跑完所有分组，返回 (每个分组的检查结果, 失败清单)。"""
        states, failures = [], []
        if not items:
            return states, failures
        workers = max(1, min(int(max_workers or 1), len(items)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='agent-check') as pool:
            futures = {pool.submit(self._check_group, entries): entries for entries in items}
            for future in as_completed(futures):
                entries = futures[future]
                rep = entries[0]
                try:
                    state = future.result()
                except Exception as exc:  # _check_group 自己吞异常，这里只是兜底
                    state = self._store_check(entries, {
                        'checkedAt': time.time(), 'latestVersion': None,
                        'updateState': 'check-failed',
                        'message': f'发布渠道检查失败：{type(exc).__name__}。请重试。'})
                states.append({'agent': rep.get('agent'), 'instanceId': rep.get('id'),
                               'source': rep.get('source'), 'version': rep.get('version'),
                               'updateState': state.get('updateState'),
                               'latestVersion': state.get('latestVersion'),
                               'message': state.get('message')})
                if state.get('updateState') == 'check-failed':
                    failures.append({'agent': rep.get('agent'),
                                     'message': state.get('message') or '发布渠道检查失败'})
                with self.lock:
                    self.bulk['checked'] = len(states)
                    self.bulk['step'] = f"已检查 {len(states)}/{len(items)}（{LABELS.get(rep.get('agent'), rep.get('agent'))}）"
        return states, failures

    def _check_group(self, entries):
        """检查一个分组的代表实例，并把结论复制给同一 manifest 的其它 shim。"""
        rep = entries[0]
        try:
            state = dict(self.check(rep['id']))
        except Exception as exc:  # check 内部已兜底，这里是二次保险
            state = self._store_check(entries, {
                'checkedAt': time.time(), 'latestVersion': None, 'updateState': 'check-failed',
                'message': f'发布渠道检查失败：{type(exc).__name__}。请重试。'})
            return state
        return self._store_check(entries, state)

    def _store_check(self, entries, state):
        """把一份检查结论写回该分组下所有实例 id。

        不复制的话，同一个包的第二条 shim 在 view() 里会一直停在 not-checked，
        界面就会出现「一行已是最新、一行没检查过」的矛盾。
        """
        with self.lock:
            for e in entries:
                self.checks[e['id']] = dict(state)
        return state

    # -- 批量更新 -----------------------------------------------------------

    def _skip_reason(self, e, check):
        """返回不能批量更新的原因；可以更新时返回 None。"""
        source = e.get('source')
        if source not in BULK_UPDATE_SOURCES:
            return SKIP_REASONS.get(source) or e.get('updateNote') or '该安装来源不支持由 Hub 更新'
        state = check.get('updateState')
        if state == 'available':
            return None
        if state == 'current':
            return '已是最新版本'
        if state == 'check-failed':
            return '更新检查失败，请重试检测'
        if state == 'unverified':
            return '版本顺序无法确认，请在原安装渠道核对'
        return '尚未检查，请先检查更新'

    def _refresh_stale(self, items):
        """检查结果超过 CHECK_TTL 就串行重查，返回重查的数量。"""
        now = time.time()
        stale = []
        for entries in items:
            rep = entries[0]
            with self.lock:
                check = dict(self.checks.get(rep['id']) or {})
            if now - float(check.get('checkedAt') or 0) > CHECK_TTL:
                stale.append(entries)
        for index, entries in enumerate(stale, 1):
            rep = entries[0]
            with self.lock:
                self.bulk['step'] = f"重新检查 {LABELS.get(rep.get('agent'), rep.get('agent'))}（{index}/{len(stale)}）"
            self._check_group(entries)
        return len(stale)

    def _plan_updates(self, items):
        """把分组分成「待更新」与「跳过（带原因）」两堆。"""
        todo, skipped = [], []
        for entries in items:
            rep = entries[0]
            with self.lock:
                check = dict(self.checks.get(rep['id']) or {})
            reason = self._skip_reason(rep, check)
            if reason is None:
                todo.append((entries, check))
            else:
                skipped.append({'agent': rep.get('agent'), 'reason': reason})
        return todo, skipped

    def _bulk_job(self, e, check):
        """给批量更新里的单个实例建一条 job 记录，沿用既有 jobs 的字段。"""
        with self.lock:
            job = {'id': f"bulk-{time.time_ns()}", 'instanceId': e['id'], 'agent': e['agent'],
                   'state': 'running', 'stage': '批量更新', 'messages': [],
                   'expectedVersion': check.get('latestVersion'), 'startedAt': time.time(),
                   'bulk': True}
            self.jobs[job['id']] = job
        return job

    def _prune_jobs(self, keep=40):
        """只留最近的若干条已结束 job，批量更新会攒出很多条。"""
        with self.lock:
            finished = sorted((j for j in self.jobs.values() if j.get('state') != 'running'),
                              key=lambda j: j.get('startedAt') or 0)
            for job in finished[:max(0, len(finished) - keep)]:
                self.jobs.pop(job['id'], None)

    def update_all(self, *, claimed=False):
        """串行把所有已确认可更新的实例升到最新，同步返回汇总。

        串行不是保守而是必须：npm 全局安装共用 prefix 与 lock 文件，
        并发执行会互相打断并留下半装状态。
        """
        groups = self._groups()
        items = list(groups.values())
        if not claimed:
            self._claim(BULK_UPDATING, len(items), '正在确认检查结果')
        else:
            with self.lock:
                self.bulk['total'] = len(items)
                self.bulk['step'] = '正在确认检查结果'
        started = self.bulk['startedAt'] or time.time()
        updated, failed, skipped = [], [], []
        try:
            self._refresh_stale(items)
            todo, skipped = self._plan_updates(items)
            with self.lock:
                self.bulk['total'] = len(todo) + len(skipped)
                self.bulk['checked'] = len(todo) + len(skipped)
                self.bulk['skipped'] = copy.deepcopy(skipped)
            for index, (entries, check) in enumerate(todo, 1):
                rep = entries[0]
                agent = rep.get('agent')
                label = LABELS.get(agent, agent)
                latest = check.get('latestVersion') or '最新版本'
                with self.lock:
                    self.bulk['step'] = f"{label} 正在安装 {latest}（{index}/{len(todo)}）"
                job = self._bulk_job(rep, check)
                # detect=False：批量模式只在最后重新检测一次。detect() 每次都要
                # 跑 PowerShell 查进程，逐个更新各跑一遍会把 5 个包的更新拖成一分钟。
                self._update(rep, check, job, detect=False)
                with self.lock:
                    if job.get('state') == 'succeeded':
                        updated.append(agent)
                        state = {'updateState': 'current',
                                 'latestVersion': job.get('actualVersion') or check.get('latestVersion'),
                                 'checkedAt': time.time(),
                                 'message': '同一安装入口已通过更新后验证'}
                        for e in entries:
                            self.checks[e['id']] = dict(state)
                    else:
                        messages = job.get('messages') or ['更新失败']
                        failed.append({'agent': agent, 'message': messages[-1]})
                    self.bulk['updated'] = list(updated)
                    self.bulk['failed'] = copy.deepcopy(failed)
        finally:
            # 无论成功失败都重新检测一次：批量更新过前缀里的包，PATH 上的
            # 版本、running 标记都必须重算，否则界面继续显示旧版本。
            with self.lock:
                self.bulk['step'] = '正在重新检测安装状态'
            try:
                self.detect()
            except Exception:
                pass
            self._prune_jobs()
            finished = time.time()
            with self.lock:
                summary = f"完成：{len(updated)} 个已更新"
                if failed:
                    summary += f"，{len(failed)} 个失败"
                if skipped:
                    summary += f"，{len(skipped)} 个跳过"
                self.bulk.update(state=BULK_IDLE, updated=list(updated),
                                 failed=copy.deepcopy(failed), skipped=copy.deepcopy(skipped),
                                 finishedAt=finished, step=summary)
                snapshot = copy.deepcopy(self.bulk)
        return {'ok': not failed, 'updated': list(updated), 'failed': copy.deepcopy(failed),
                'skipped': copy.deepcopy(skipped), 'startedAt': started, 'finishedAt': finished,
                'elapsed': round(finished - started, 3), 'bulk': snapshot}

    def start_update_all(self):
        """立即返回、后台串行更新。进度写进 bulk，前端轮询 GET /api/agents。"""
        with self.lock:
            # 先判 bulk：批量自己跑的 job 也是 running，顺序反了会报错原因。
            if self.bulk['state'] != BULK_IDLE:
                raise BulkBusy('已有批量任务正在运行，请等待完成', self.bulk)
            if any(j.get('state') == 'running' for j in self.jobs.values()):
                raise BulkBusy('已有更新正在运行，请等待完成后再试', self.bulk)
        total = len(self._groups())
        self._claim(BULK_UPDATING, total, '正在确认检查结果')
        threading.Thread(target=self.update_all, kwargs={'claimed': True}, daemon=True).start()
        return self.bulk_state()

    def _update(self, e, check, job, detect=True):
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
            if detect:
                # 单实例更新走这里；批量模式传 detect=False，由 update_all
                # 在最后统一检测一次。
                self.detect()
            if job['state'] == 'succeeded':
                with self.lock:
                    self.checks[e['id']] = {'updateState': 'current', 'latestVersion': job['actualVersion'], 'checkedAt': time.time(), 'message': '同一安装入口已通过更新后验证'}
