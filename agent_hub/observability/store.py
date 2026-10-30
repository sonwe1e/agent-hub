"""Read native telemetry and maintain the local observation index.

Normalized records share one filter/query path for all four observation pages.
Configuration is never evidence of a call. Hub gateway and supported legacy
records supplement native logs; only explicit identities and exact response IDs
link records, without time-based guessing.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import platform
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_hub.agents.inventory import AGENTS

# Match Magpie's local-first defaults: usage is available automatically;
# recording a second copy of conversation content is separate and opt-in.
DEFAULTS = {'enabled': True, 'content': False, 'retentionDays': 0, 'clearedBefore': 0}
CONTENT_DAYS = 7
CONTENT_MAX_BYTES = 256 * 1024 * 1024
# Native clients embed image/tool payloads in single JSONL records; the former
# 8 MiB gateway-style bound rejected a valid 14 MiB Codex native record.
MAX_LINE = 16 * 1024 * 1024
MAX_DECODED = 128 * 1024 * 1024
PARSER_REVISION = 5
# 原生客户端是被动写入方：刚写了一半的文件不该被记成"读取失败"。
INFLIGHT_SECONDS = 3.0
# 占用/权限错误大多是瞬时状态，一次短退避重试就能消化掉。
RETRY_BACKOFF = 0.15
MAX_ERRORS = 100
MAX_DEGRADED = 100
ERROR_KINDS = ('in-flight', 'locked', 'too-large', 'unreadable', 'parse')


def retention_cutoff(settings, now=None):
    days = settings.get('retentionDays', 0)
    return max((now or time.time()) - days * 86400 if days else 0, settings.get('clearedBefore', 0))


def stamp(value):
    if isinstance(value, (float, int)):
        return value / 1000 if value > 10**11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def fresh_input(u):
    if u.get('inputIncludesCache'):
        return max(0,u['input']-u['cacheRead']) if u.get('input') is not None and u.get('cacheRead') is not None else None
    return u.get('input')


def key(*values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def redact(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 6:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(?:bearer\s+)[\w.\-+/=]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|authorization|password|secret)["\s]*[:=]["\s]*)[^\s,"\}]+', r'\1[REDACTED]', text)
    text = re.sub(r'(https?://[^\s"<>?\\]+)\?[^\s"<>\\]*', r'\1?[REDACTED-QUERY]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[REDACTED]',text)
    return re.sub(r'\b(?:sk-[\w-]{8,}|gh[pousr]_[\w]{15,})', '[REDACTED]', text)[:16000]


def usage(raw, kind):
    raw = raw or {}
    mapping = {
        'claude': ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'),
        'codex': ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_input_tokens'),
        'pi': ('input', 'output', 'cacheRead', 'cacheWrite'),
        'dsh': ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens'),
        'opencode': ('input', 'output', 'cacheRead', 'cacheWrite'),
    }
    a, b, c, d = mapping[kind]
    result = dict(zip(('input', 'output', 'cacheRead', 'cacheWrite'), (number(raw.get(x)) for x in (a, b, c, d))))
    result['reasoning'] = number(raw.get('reasoning_output_tokens')) if kind == 'codex' else number(raw.get('reasoning'))
    if kind == 'claude':
        result['reasoning'] = number((raw.get('output_tokens_details') or {}).get('thinking_tokens'))
    # Codex input already includes cached input; the other clients report fresh input.
    result['context'] = result['input'] if kind == 'codex' else (sum(result[x] or 0 for x in ('input', 'cacheRead', 'cacheWrite')) if result['input'] is not None else None)
    result['inputIncludesCache'] = kind == 'codex'
    result['contextPrecision'] = ('measured' if kind == 'codex' or all(result[x] is not None for x in ('input','cacheRead','cacheWrite'))
                                  else 'lower-bound' if result['input'] is not None else 'unknown')
    return result


_ZSTD: bool | None = None


def zstd_available():
    """Whether this interpreter can decode dsh's compressed session logs.

    Probed once per process: the check costs an import, and `native_files`
    calls it per session directory. A missing optional dependency must never
    be fatal — `native_files` degrades to the previous uncompressed generation
    instead of losing the source entirely.
    """
    global _ZSTD
    if _ZSTD is None:
        try:
            import zstandard  # noqa: F401
            _ZSTD = True
        except Exception:
            _ZSTD = False
    return _ZSTD


def open_lines(path, report=None):
    if path.suffix in ('.zstd', '.zst'):
        try:
            import zstandard
        except ImportError as exc:
            raise ValueError(
                '压缩日志需要 zstandard 才能读取；桌面版已内置该依赖，'
                f'以源码运行时需要给当前解释器安装：{sys.executable}'
            ) from exc
        raw = path.open('rb')
        try:
            with zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) as reader:
                yield from _lines(io.BufferedReader(reader), report)
        finally:
            raw.close()
    else:
        with path.open('rb') as reader:
            yield from _lines(reader, report)


def _lines(reader, report=None):
    """Yield JSON objects; safety limits degrade instead of discarding the file.

    `report` (optional dict) collects how a source was narrowed down:
    `oversized` (chunks dropped for exceeding MAX_LINE), `truncated` (stopped
    after MAX_DECODED), `invalid` (undecodable rows), `partialTail`, and `bytes`
    (how much was actually decoded). Keeping the decoded prefix is strictly
    better than dropping a 100 MiB log because one embedded image was too large.
    """
    total = 0
    try:
        while True:
            line = reader.readline(MAX_LINE + 1)
            if not line:
                return
            total += len(line)
            if len(line) > MAX_LINE:
                # One over-long record cannot be decoded; the rest of the file still can.
                if report is not None:
                    report['oversized'] = report.get('oversized', 0) + 1
                continue
            if total > MAX_DECODED:
                if report is not None:
                    report['truncated'] = True
                return
            # A partially written tail is never treated as a completed event.
            if not line.endswith(b'\n'):
                if report is not None:
                    report['partialTail'] = True
                return
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if report is not None:
                    report['invalid'] = report.get('invalid', 0) + 1
                continue
            if isinstance(row, dict):
                yield row
            elif report is not None:
                report['invalid'] = report.get('invalid', 0) + 1
    finally:
        # How much actually came out of the source; a compressed frame that was
        # still being written decodes to zero bytes without raising, and that
        # must not look like "this log has no events".
        if report is not None:
            report['bytes'] = total


def opencode_database(home):
    """OpenCode's explicitly configured database or its standard data location."""
    configured = os.environ.get('OPENCODE_DB')
    if configured:
        return Path(configured).expanduser().absolute()
    data = Path(os.environ.get('XDG_DATA_HOME') or Path(home) / '.local' / 'share').expanduser()
    return (data / 'opencode' / 'opencode.db').absolute()


def native_roots(home):
    """Allowed native log roots per agent; the only paths Hub will ever open."""
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude') / 'projects'
    codex = Path(os.environ.get('CODEX_HOME') or home / '.codex')
    pi = Path(os.environ.get('PI_CODING_AGENT_DIR') or home / '.pi/agent') / 'sessions'
    dsh = Path(os.environ.get('DSH_HOME') or home / '.dsh') / 'sessions'
    return {'claude': [claude], 'codex': [codex / 'sessions', codex / 'archived_sessions'],
            'pi': [pi], 'dsh': [dsh]}


def source_agent(home, path):
    """Return the agent whose native root contains `path`, else None.

    Deliberately path-containment based and cheap: a retry request must never
    turn an HTTP-supplied path into an arbitrary file read.
    """
    path = Path(path)
    try:
        if path.resolve() == opencode_database(home).resolve():
            return 'opencode'
    except OSError:
        return None
    if path.suffix not in ('.jsonl', '.zstd'):
        return None
    for agent, roots in native_roots(home).items():
        for root in roots:
            try:
                if path.resolve().is_relative_to(root.resolve()):
                    return agent
            except OSError:
                continue
    return None


def native_files(home):
    roots = {'claude': Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude') / 'projects',
             'codex': Path(os.environ.get('CODEX_HOME') or home / '.codex') / 'sessions',
             'pi': Path(os.environ.get('PI_CODING_AGENT_DIR') or home / '.pi/agent') / 'sessions',
             'dsh': Path(os.environ.get('DSH_HOME') or home / '.dsh') / 'sessions'}
    sources, info = [], []
    for agent, root in roots.items():
        files = list(root.rglob('*.jsonl')) if root.exists() else []
        if agent == 'codex':
            archived = root.parent / 'archived_sessions'
            if archived.exists():
                files += list(archived.rglob('*.jsonl'))
        degraded = None
        if agent == 'dsh' and root.exists():
            candidates = {}
            for f in root.rglob('session*.jsonl*'):
                m = re.fullmatch(r'session(?:\.v(\d+))?\.jsonl(?:\.zstd)?', f.name)
                if not m or not f.is_file():
                    continue
                rank = (int(m[1] or 0), f.stat().st_mtime_ns)
                candidates.setdefault(f.parent, []).append((rank, f))
            readable = zstd_available()
            files, fell_back, unreadable = [], 0, 0
            for items in candidates.values():
                items.sort(key=lambda x: x[0], reverse=True)
                chosen = next((f for _, f in items
                               if readable or f.suffix not in ('.zstd', '.zst')), None)
                if chosen is None:
                    # Nothing readable at all: keep the newest so the round can
                    # report a real, path-level failure instead of hiding it.
                    chosen = items[0][1]
                    unreadable += 1
                elif chosen is not items[0][1]:
                    fell_back += 1
                files.append(chosen)
            if fell_back or unreadable:
                degraded = {'fallback': fell_back, 'unreadable': unreadable}
        files = [f for f in files if f.is_file()]
        sources.extend((agent, f) for f in files)
        entry = {'agent': agent, 'root': str(root), 'exists': root.exists(), 'files': len(files),
                 'coverage': 'native-partial', 'note': '只读原生记录；未记录的网络失败、重试、TTFT 无法补推。'}
        if degraded:
            entry['degraded'] = degraded
        info.append(entry)
    database = opencode_database(home)
    present = database.is_file()
    if present:
        sources.append(('opencode', database))
    info.append({'agent': 'opencode', 'root': str(database), 'exists': present,
                 'files': int(present), 'coverage': 'native-partial',
                 'note': '只读 OpenCode v2 会话数据库；未记录的网络失败、重试、TTFT 无法补推。'})
    return sorted(sources, key=lambda x: x[1].stat().st_mtime, reverse=True), info


def error_kind(exc):
    """Map an exception to one of ERROR_KINDS so a failure is explainable."""
    if isinstance(exc, PermissionError):
        return 'locked'
    if isinstance(exc, sqlite3.OperationalError) and any(x in str(exc).lower() for x in ('locked', 'busy')):
        return 'locked'
    if isinstance(exc, OSError):
        # WinError 32/33: the file is held by another process.
        return 'locked' if getattr(exc, 'winerror', None) in (32, 33) else 'unreadable'
    if isinstance(exc, ValueError):
        text = str(exc)
        if '安全上限' in text:
            return 'too-large'
        if 'zstandard' in text:
            return 'unreadable'
        return 'parse'
    return 'unreadable'


def error_message(exc):
    text = str(exc).strip()
    return f'{type(exc).__name__}: {text}' if text else type(exc).__name__


def human_bytes(value):
    """Readable bound for error messages ('16 MiB' / '512 KiB')."""
    if value >= 1024 * 1024:
        amount = value / (1024 * 1024)
        return f'{amount:g} MiB'
    return f'{max(1, value // 1024)} KiB'


def fresh(st):
    """True when the file was touched within the in-flight window."""
    return INFLIGHT_SECONDS > 0 and time.time() - st.st_mtime < INFLIGHT_SECONDS


def in_flight(path, st):
    """True while a client is probably still writing this file.

    mtime alone is not enough: a client that finishes and closes a file
    immediately leaves a *complete* file with a fresh mtime, and dropping it
    would lose a whole round of usage. So a fresh file is only treated as
    in-flight when it is still visibly incomplete here; compressed frames are
    judged by whether they decode (see `Store._collect_one`).
    """
    if st.st_size == 0:
        return fresh(st)  # nothing to index yet; an old empty file is just empty
    if not fresh(st):
        return False
    if path.suffix in ('.zstd', '.zst'):
        return False  # decode result decides, not the tail byte
    try:
        with path.open('rb') as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b'\n'
    except OSError:
        return True


class Parsed:
    def __init__(self, agent, path, content=False, secrets=()):
        self.agent, self.path, self.content, self.secrets = agent, path, content, secrets
        self.sid = None
        self.parent = None
        self.cwd = None
        self.title = None
        self.started = None
        self.requests = {}
        self.messages = []
        self.compactions = []
        self.quotas = []
        self.model = None
        self.provider = None
        self.window = None

    def message(self, row_id, at, role, body):
        if not at:
            return
        if self.content:
            text = redact(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False), self.secrets)
        else:
            text = None
        self.messages.append({'id': key(self.agent, self.sid, row_id), 'time': at, 'role': role, 'body': text})
        if len(self.messages) > 50000:
            raise ValueError('会话事件超过 50000 条，需缩短原生日志或分段索引')

    def request(self, native_id, at, raw_usage, *, model=None, served=None, provider=None,
                status='unknown', error=None, duration=None, kind='response', evidence=None):
        if not native_id or not at:
            return
        rid = key(self.agent, native_id)
        u = usage(raw_usage, self.agent)
        row = {'id': rid, 'nativeId': native_id, 'agent': self.agent, 'sessionId': self.sid,
               'time': at, 'model': model, 'servedModel': served, 'provider': provider,
               'providerEvidence': 'native-client' if provider else 'unknown',
               'status': status, 'error': redact(error, self.secrets) if error else None,
               'durationMs': number(duration), 'ttftMs': None, 'outputSpeed': None,
               'usage': u, 'window': self.window, 'windowEvidence': 'native-event' if self.window else None,
               'kind': kind, 'attempts': None, 'retried': None, 'cost': None, 'costEvidence': None,
               'source': str(self.path), 'sourceType': 'native', 'evidence': evidence,
               'gaps': ['原生日志不是完整网络流水；上游尝试、备用线路与首字等待未采集。',
                        '系统指令、工具定义、文件/记忆注入无完整请求快照；不提供推测的分项 Token。']}
        previous = self.requests.get(rid)
        if previous:
            # Claude splits a single response over multiple assistant blocks.
            for k in ('input', 'output', 'cacheRead', 'cacheWrite', 'reasoning', 'context'):
                vals = [x for x in (previous['usage'][k], u[k]) if x is not None]
                u[k] = max(vals) if vals else None
            row['time'] = max(at, previous['time'])
            if status == 'unknown':
                row['status'] = previous['status']
        if self.agent == 'pi':
            cost = (raw_usage or {}).get('cost')
            if isinstance(cost, dict) and number(cost.get('total')) is not None and cost['total'] > 0:
                row.update(cost=cost['total'], costEvidence='native-reference', costNote='客户端记录的 USD 参考成本；非供应商实际扣费')
        self.requests[rid] = row


def parse_opencode_database(path, content=False, secrets=(), report=None, session_id=None,
                            content_cutoff=None):
    """Read OpenCode v2's session/message projections without opening auth tables.

    Each assistant row is a client-recorded model step. An explicitly recorded,
    unique provider response ID can associate it with the same gateway response.
    SQLite is opened read-only so an active WAL remains visible without
    modifying the client's database. Older schemas are reported explicitly.
    """
    path = Path(path).absolute()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')  # a consistent view of sessions and messages, including WAL
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'session_v2', 'session_message'} <= tables:
            raise ValueError('OpenCode 会话数据库不是受支持的 v2 格式')
        sessions = {}
        where = ' WHERE id=?' if session_id is not None else ''
        args = (session_id,) if session_id is not None else ()
        title_column = 'title' if content else 'NULL'
        for sid, parent, directory, title, created in db.execute(
                f'SELECT id,parent_id,directory,{title_column},time_created FROM session_v2' + where, args):
            if not isinstance(sid, str) or not sid:
                continue
            p = Parsed('opencode', path, content, secrets)
            p.sid, p.parent, p.cwd, p.started = sid, parent, directory, stamp(created)
            p.title = redact(title, secrets)[:120] if content and title else None
            sessions[sid] = p
        selection = ' AND session_id=?' if session_id is not None else ''
        if report is not None:
            report['bytes'] = path.stat().st_size
            report['invalid'] = db.execute(
                "SELECT count(*) FROM session_message WHERE type='assistant' AND NOT json_valid(data)" + selection,
                args).fetchone()[0]
        query = '''SELECT id,session_id,time_created,
                   json_extract(data,'$.model.id'),json_extract(data,'$.model.providerID'),
                   json_extract(data,'$.finish'),json_extract(data,'$.time.created'),
                   json_extract(data,'$.time.completed'),json_extract(data,'$.tokens.input'),
                   json_extract(data,'$.tokens.output'),json_extract(data,'$.tokens.reasoning'),
                   json_extract(data,'$.tokens.cache.read'),json_extract(data,'$.tokens.cache.write'),
                   json_extract(data,'$.cost'),json_extract(data,'$.error'),
                   json_extract(data,'$.providerState.responseId')
                   FROM session_message WHERE type='assistant' AND json_valid(data)''' + selection
        steps = list(db.execute(query, args))
        response_counts = Counter(row[-1] for row in steps if isinstance(row[-1], str) and row[-1])
        for mid, sid, created, model, provider, finish, start, end, inp, out, reasoning, cached, written, cost, error, response_id in steps:
            p = sessions.get(sid)
            if p is None or not isinstance(mid, str):
                continue
            output, thinking = number(out), number(reasoning)
            total_output = output + thinking if output is not None and thinking is not None else None
            raw = {'input': inp, 'output': total_output, 'reasoning': thinking,
                   'cacheRead': cached, 'cacheWrite': written}
            finished = stamp(end)
            started = stamp(start) or stamp(created)
            status = ('failed' if finish == 'error' else
                      'interrupted' if finish in ('aborted', 'cancelled') else
                      'succeeded' if finished and finish in ('stop', 'length', 'tool-calls', 'tool_calls') else 'unknown')
            if status in ('failed', 'interrupted') and all(raw.get(k) == 0 for k in ('input', 'output', 'cacheRead', 'cacheWrite')):
                raw = {}
            exact_response = isinstance(response_id, str) and response_counts[response_id] == 1
            native_id = response_id if exact_response else 'opencode-message:' + sid + ':' + mid
            p.request(native_id, started, raw, model=model, provider=provider, status=status,
                      error=error, duration=(finished-started)*1000 if finished and started and finished >= started else None,
                      evidence=('OpenCode v2 session_message：providerState.responseId / 用量' if exact_response else
                                'OpenCode v2 session_message：客户端模型步骤和用量；没有可唯一确认的响应 ID'))
            row = p.requests.get(key('opencode', native_id))
            if row and number(cost) is not None and cost > 0:
                row.update(cost=cost, costEvidence='native-reference',
                           costNote='OpenCode 记录的参考成本；非供应商实际扣费')
            if row:
                row['gaps'].append('OpenCode 的 output 与 reasoning 分开记录；输出总量为两者之和。')
                if not exact_response:
                    row['gaps'].append('缺少唯一响应 ID；若同一调用另经 Hub 网关，可能出现未关联的重复记录。')
        compact_query = "SELECT session_id,time_created FROM session_message WHERE type='compaction'" + selection
        for sid, created in db.execute(compact_query, args):
            if sid in sessions and stamp(created):
                sessions[sid].compactions.append({'time': stamp(created), 'source': 'OpenCode compaction'})
        if content:
            # Only fetch bodies when explicitly requested; large inline attachments
            # are skipped, while their metadata and usage can still be indexed.
            lower = int(content_cutoff * 1000) if content_cutoff is not None else 0
            content_query = '''SELECT id,session_id,type,time_created,
                               CASE WHEN length(CAST(data AS BLOB)) <= ? THEN data ELSE NULL END,
                               length(CAST(data AS BLOB)) FROM session_message
                               WHERE type IN ('user','assistant','synthetic','system','compaction')
                               AND time_created>=?''' + selection + ' ORDER BY time_created,seq'
            for mid, sid, kind, created, data, size in db.execute(content_query, (MAX_LINE, lower, *args)):
                p = sessions.get(sid)
                if p is None:
                    continue
                if size > MAX_LINE:
                    if report is not None:
                        report['oversized'] = report.get('oversized', 0) + 1
                    continue
                try:
                    message = json.loads(data)
                except (TypeError, ValueError):
                    if report is not None and kind != 'assistant':
                        report['invalid'] = report.get('invalid', 0) + 1
                    continue
                if not isinstance(message, dict):
                    continue
                body = message.get('content') if kind == 'assistant' else message.get('text', message.get('summary', ''))
                p.message(mid, stamp(created), kind, body or '')
        return list(sessions.values())
    finally:
        db.close()


def parse_native(agent, path, content=False, secrets=(), report=None):
    p = Parsed(agent, path, content, secrets)
    rows = list(open_lines(path, report))
    if agent == 'claude':
        # Subagents often repeat their parent's sessionId. Their file's explicit
        # agent id establishes a distinct child; parent relationship is structural.
        is_sub = path.parent.name == 'subagents' and path.stem.startswith('agent-')
        for i, r in enumerate(rows):
            native_sid = r.get('sessionId') or r.get('session_id')
            if native_sid and not p.sid:
                p.sid = native_sid + '/' + path.stem if is_sub else native_sid
                p.parent = native_sid if is_sub else None
            at = stamp(r.get('timestamp'))
            p.cwd = r.get('cwd') or p.cwd
            if at and not p.started:
                p.started = at
            if r.get('type') == 'ai-title' and content:
                p.title = redact(r.get('aiTitle', ''), secrets)[:120]
            m = r.get('message') or {}
            if r.get('type') in ('assistant', 'user'):
                p.message(r.get('uuid', i), at, r['type'], m.get('content', ''))
            if r.get('type') == 'assistant' and m.get('id') and m.get('model') != '<synthetic>':
                failed = bool(r.get('isApiErrorMessage')) or m.get('stop_reason') in ('error', 'refusal')
                status = 'failed' if failed else 'succeeded' if m.get('stop_reason') in ('end_turn', 'tool_use', 'max_tokens', 'stop_sequence') else 'unknown'
                p.request(m['id'], at, m.get('usage'), model=r.get('requestedModel'), served=m.get('model'),
                          status=status, error=r.get('error'), evidence='assistant.message.id / usage / stop_reason')
            if r.get('subtype') == 'compact_boundary':
                p.compactions.append({'time': at, 'source': 'system.compact_boundary'})
    elif agent == 'codex':
        modern = any(r.get('type') == 'token_usage_record' for r in rows)
        for i, r in enumerate(rows):
            data = r.get('payload') or {}
            at = stamp(r.get('timestamp'))
            typ = r.get('type')
            if typ == 'session_meta':
                p.sid = data.get('id') or data.get('session_id')
                p.cwd, p.provider = data.get('cwd'), data.get('model_provider')
                p.started = at
                p.window = number(data.get('context_window'))
                sub = ((data.get('source') or {}).get('subagent') or {}) if isinstance(data.get('source'), dict) else {}
                if isinstance(sub, dict):
                    spawn = sub.get('thread_spawn') or {}
                    p.parent = spawn.get('parent_thread_id') if isinstance(spawn, dict) else None
            if typ == 'turn_context':
                p.model = data.get('model') or p.model
                p.cwd = data.get('cwd') or p.cwd
            if typ == 'event_msg' and data.get('type') == 'token_count':
                info = data.get('info') or {}
                p.window = number(info.get('model_context_window')) or p.window
                quota = data.get('rate_limits')
                if isinstance(quota, dict):
                    # Whitelist quota fields; no account ids, login tokens, headers.
                    p.quotas.append({'agent': 'codex', 'time': at, 'plan': quota.get('plan_type'),
                                     'primary': quota.get('primary'), 'secondary': quota.get('secondary'),
                                     'credits': {k: v for k, v in (quota.get('credits') or {}).items() if k in ('has_credits', 'unlimited', 'balance')},
                                     'source': 'native-rate-limit-snapshot'})
                if not modern and isinstance(info.get('last_token_usage'), dict):
                    total = info.get('total_token_usage')
                    if isinstance(total, dict):
                        nid = key('legacy-token-count', p.sid, total)
                        if key('codex', nid) not in p.requests:
                            p.request(nid, at, info['last_token_usage'], model=p.model, provider=p.provider,
                                      evidence='event_msg.token_count.last_token_usage；按累计计数首次出现去重')
            if typ == 'token_usage_record' and data.get('response_id'):
                p.sid = p.sid or data.get('session_id') or data.get('thread_id')
                if data.get('thread_id') and data['thread_id'] != p.sid:
                    continue # copied parent usage in a fork belongs to its original thread
                p.request(data['response_id'], at, data.get('usage'), model=p.model, provider=p.provider,
                          evidence='token_usage_record.response_id / usage；用量事件不证明流式完整结束')
            if typ == 'response_item':
                role = data.get('role') or data.get('type')
                if role in ('user', 'assistant', 'function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'):
                    body = data.get('content', data.get('arguments', data.get('input', data.get('output', ''))))
                    if role.endswith(('_call', '_call_output')):
                        body = {k: data[k] for k in ('call_id', 'name', 'status') if k in data} | {'content': body}
                    p.message(data.get('id') or r.get('ordinal', i), at, role, body)
            if typ == 'event_msg' and data.get('type') in ('task_complete', 'task_failed', 'turn_aborted'):
                # A native turn completion can contain several model responses.
                # Preserve its scope instead of assigning turn timings/status to
                # an arbitrary individual response_id.
                p.message(key('turn-outcome', data.get('turn_id'), at), at, data['type'],
                          {k: data[k] for k in ('turn_id','duration_ms','time_to_first_token_ms','reason','error') if k in data})
            if typ == 'compacted':
                p.compactions.append({'time': at, 'source': 'compacted'})
    elif agent == 'pi':
        for i, r in enumerate(rows):
            typ, at = r.get('type'), stamp(r.get('timestamp'))
            if typ == 'session':
                p.sid, p.cwd, p.started = r.get('id'), r.get('cwd'), at
                p.parent = r.get('parentSession') # May be a native file reference, do not guess its id.
                if content and r.get('name'):
                    p.title = redact(r['name'],secrets)[:120]
            if typ == 'session_info' and content and r.get('name'):
                p.title = redact(r['name'],secrets)[:120]
            if typ == 'model_change':
                p.model, p.provider = r.get('modelId'), r.get('provider')
            if p.parent and at and p.started and at < p.started:
                continue # fork copied the parent entries
            m = r.get('message') or {}
            if typ == 'message':
                body = m.get('content', '')
                if m.get('role') == 'toolResult':
                    body = {k: m[k] for k in ('toolCallId','toolName','isError','durationMs') if k in m} | {'content': body}
                elif m.get('role') == 'assistant' and m.get('errorMessage'):
                    body = {'content':body, 'stopReason':m.get('stopReason'), 'errorMessage':m['errorMessage']}
                p.message(r.get('id', i), at, m.get('role', 'unknown'), body)
            if typ == 'message' and m.get('role') == 'assistant':
                stop = m.get('stopReason')
                status = 'interrupted' if stop == 'aborted' else 'failed' if stop == 'error' else 'succeeded' if stop in ('stop', 'toolUse', 'length') else 'unknown'
                raw = m.get('usage') or {}
                default_zero = status in ('failed','interrupted') and all(raw.get(k) == 0 for k in ('input','output','cacheRead','cacheWrite'))
                nid = m.get('responseId') or key(p.sid, r.get('id'), at)
                p.request(nid, at, {} if default_zero else raw,
                          model=m.get('model'), provider=m.get('provider'), status=status,
                          error=m.get('errorMessage'), duration=m.get('durationMs'), evidence='message.responseId / usage / stopReason')
                if default_zero and key(p.agent,nid) in p.requests:
                    p.requests[key(p.agent,nid)]['gaps'].insert(0,'失败/中断消息中的全零用量可能是客户端初始化值；上游实际用量未确认，不能据此断言零 Token 或免费。')
            if typ in ('usage', 'compaction', 'branch_summary') and isinstance(r.get('usage'), dict):
                p.request(key(p.sid, r.get('id'), at), at, r['usage'], model=r.get('model') or p.model,
                          provider=r.get('provider') or p.provider, kind=typ, evidence='native auxiliary usage')
            if typ == 'compaction':
                p.compactions.append({'time': at, 'source': 'compaction', 'tokensBefore': number(r.get('tokensBefore'))})
    else:
        if not rows or rows[0].get('type') != 'session':
            raise ValueError('无法识别 dsh 会话头')
        header = rows[0]
        p.sid, p.cwd = header.get('id'), header.get('cwd')
        p.started = stamp(header.get('createdAt'))
        p.parent = header.get('parentSession') if header.get('delegationDepth', 0) > 0 else None
        cut = -1
        if header.get('version', 0) >= 2 and header.get('isSeeded'):
            marks = [i for i, r in enumerate(rows) if r.get('type') == 'session/end-seed' and (r.get('data') or {}).get('inherited')]
            if not marks:
                raise ValueError('dsh seed 尚未写完，等待下次采集')
            cut = max(marks)
        for i, r in enumerate(rows[1:], 1):
            if i <= cut or header.get('version', 0) < 2 and r.get('seq', 10**18) < header.get('seedLength', 0):
                continue
            typ, at, data = r.get('type'), stamp(r.get('time')), r.get('data') or {}
            if typ == 'request/context':
                p.window = number(data.get('contextWindow'))
            if typ == 'session/title' and content:
                p.title = redact(data.get('title', ''), secrets)[:120]
            if typ in ('assistant/message', 'user/message', 'tool/result', 'tool/call'):
                body = data.get('content', (data.get('message') or {}).get('content', ''))
                if typ == 'tool/call':
                    body = {k: data[k] for k in ('callId','name','arguments') if k in data}
                if typ == 'tool/result':
                    message = data.get('message') or {}
                    body = {k: message[k] for k in ('callId','toolCallId','name','toolName','isError') if k in message} | {'content': body}
                p.message(r.get('seq', i), at, typ, body)
            if typ == 'assistant/message' and isinstance(data.get('usage'), dict):
                src = (data.get('message') or {}).get('source') or {}
                response = (src.get('replayState') or {}).get('response') or {}
                stop = response.get('stopReason')
                status = 'interrupted' if stop == 'aborted' else 'failed' if stop == 'error' else 'succeeded' if stop in ('stop', 'toolUse', 'length') else 'unknown'
                finishes = [x.get('chunk') for x in (data.get('stream') or []) if isinstance(x, dict)
                            and isinstance(x.get('chunk'), dict) and x['chunk'].get('type') == 'finish']
                finish = (finishes[-1].get('reason') or {}) if finishes else {}
                finish = finish if isinstance(finish, dict) else {}
                if status == 'unknown':
                    status = {'stop':'succeeded', 'tool-calls':'succeeded', 'length':'succeeded',
                              'error':'failed', 'aborted':'interrupted', 'cancelled':'interrupted'}.get(finish.get('kind'), 'unknown')
                p.request(response.get('responseId') or key(p.sid, r.get('seq', i)), at, data['usage'], model=src.get('model'),
                          served=response.get('responseModel') or response.get('model'),
                          provider=src.get('provider') or response.get('provider'), status=status,
                          error=(finish.get('error') or {}).get('message') if isinstance(finish.get('error'), dict) else None,
                          evidence='assistant/message.seq / usage / replayState.response.stopReason / stream.finish.reason（如有）')
            if typ in ('session/compact', 'context/compaction'):
                p.compactions.append({'time': at, 'source': typ})
    return p


class Store:
    def __init__(self, root, home=None, config=None):
        self.root = Path(root)
        self.home = Path(home or Path.home())
        self.config = config or (lambda: {})
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'observability.sqlite3'
        self.settings_path = self.root / 'observation-settings.json'
        self.lock = threading.RLock()
        self.worker = None
        self.collecting = False
        self.stop_event = threading.Event()
        self.monitor = None
        self.progress = {'state': 'idle', 'mode': 'full', 'processed': 0, 'total': 0,
                         'errors': [], 'degraded': [], 'unchanged': 0, 'inFlight': 0,
                         'empty': 0, 'skipped': 0, 'failed': 0, 'sources': []}
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, agent TEXT, sid TEXT, at REAL, data TEXT);
                CREATE INDEX IF NOT EXISTS request_time ON requests(at);
                CREATE INDEX IF NOT EXISTS request_session ON requests(agent,sid);
                CREATE INDEX IF NOT EXISTS request_source ON requests(json_extract(data,'$.source'));
                CREATE INDEX IF NOT EXISTS request_provider_time ON requests(json_extract(data,'$.provider'),at);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, agent TEXT, sid TEXT, at REAL, data TEXT);
                CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, agent TEXT, sid TEXT, at REAL, data TEXT);
                CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, fingerprint TEXT);
                CREATE TABLE IF NOT EXISTS quotas(id TEXT PRIMARY KEY, at REAL, data TEXT);
                CREATE TABLE IF NOT EXISTS source_failures(path TEXT PRIMARY KEY, agent TEXT, kind TEXT, message TEXT, at REAL);
                CREATE TABLE IF NOT EXISTS session_title_metadata(
                    agent TEXT NOT NULL, sid TEXT NOT NULL, title TEXT NOT NULL,
                    source TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(agent,sid));
                CREATE TABLE IF NOT EXISTS session_manual_titles(
                    agent TEXT NOT NULL, sid TEXT NOT NULL, title TEXT NOT NULL,
                    at REAL NOT NULL, PRIMARY KEY(agent,sid));
                CREATE TABLE IF NOT EXISTS session_title_control(
                    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
                    epoch INTEGER NOT NULL, cleared_before REAL NOT NULL);
            ''')
            settings = self.settings()
            db.execute('''INSERT INTO session_title_control(id,enabled,epoch,cleared_before) VALUES(1,?,1,?)
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, epoch=session_title_control.epoch+1,
                cleared_before=MAX(session_title_control.cleared_before,excluded.cleared_before)''',
                (int(bool(settings.get('enabled'))), float(settings.get('clearedBefore') or 0)))
            # Kept in memory so a successful source does not cost a DB round-trip.
            self.failed_paths = {row[0] for row in db.execute('SELECT path FROM source_failures')}

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA secure_delete=ON')
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        try:
            value = json.loads(self.settings_path.read_text(encoding='utf-8'))
            return {**DEFAULTS, **value}
        except (ValueError, OSError):
            return dict(DEFAULTS)

    def set_settings(self, body):
        with self.lock:
            value = self.settings()
            for field in ('enabled', 'content'):
                if field in body:
                    if not isinstance(body[field], bool):
                        raise ValueError(field + ' 必须是布尔值')
                    value[field] = body[field]
            if 'retentionDays' in body:
                if type(body['retentionDays']) is not int or not 0 <= body['retentionDays'] <= 365:
                    raise ValueError('用量索引保留期限应为 0（不自动过期）或 1–365 天')
                value['retentionDays'] = body['retentionDays']
            temp = self.settings_path.with_suffix('.tmp')
            temp.write_text(json.dumps(value), encoding='utf-8')
            temp.replace(self.settings_path)
            with self.db() as db:
                db.execute('''UPDATE session_title_control SET enabled=?,epoch=epoch+1,
                    cleared_before=MAX(cleared_before,?) WHERE id=1''',
                    (int(value['enabled']), float(value.get('clearedBefore') or 0)))
                if not value['content']:
                    db.execute('DELETE FROM messages')
                    for sid, data in db.execute('SELECT id,data FROM sessions').fetchall():
                        data = json.loads(data)
                        data['title'] = None
                        db.execute('UPDATE sessions SET data=? WHERE id=?', (json.dumps(data), sid))
            self.cleanup()
        if value['enabled']:
            self.refresh()
        return self.status()

    def snapshot(self):
        """Defensive copy of the collector state; HTTP callers cannot mutate it."""
        with self.lock:
            p = self.progress
            return {**p, 'errors': [dict(e) for e in p.get('errors', [])],
                    'degraded': [dict(e) for e in p.get('degraded', [])],
                    'sources': [dict(s) for s in p.get('sources', [])]}

    def capabilities(self):
        """What this interpreter is actually able to read.

        Surfaced in `status()` so a missing optional dependency is a stated,
        actionable fact instead of an unexplained gap in the source list.
        """
        return {'zstd': zstd_available(), 'interpreter': sys.executable,
                'python': platform.python_version()}

    def status(self):
        with self.db() as db:
            count = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            ncontent = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            content_bytes = db.execute('SELECT COALESCE(SUM(length(CAST(data AS BLOB))),0) FROM messages').fetchone()[0]
        return {'settings': self.settings(), 'collector': self.snapshot(), 'records': count,
                'contentRecords': ncontent, 'contentBytes': content_bytes,
                'capabilities': self.capabilities(),
                'contentRetentionDays': CONTENT_DAYS, 'contentMaxBytes': CONTENT_MAX_BYTES, 'path': str(self.path),
                'coverage': '原生日志的局部视图；没有记录不等于零请求。真实客户端验证范围与版本见 docs/validation.md；不代表完整网络流水。'}

    def refresh(self):
        if not self.settings()['enabled']:
            return self.status()
        self.refresh_thread(self.collect)
        return self.status()

    def start(self):
        if self.monitor and self.monitor.is_alive():
            return
        self.stop_event.clear()
        def loop():
            while not self.stop_event.is_set():
                if self.settings()['enabled']:
                    self.refresh()
                else:
                    self.cleanup()
                self.stop_event.wait(30)
        self.monitor = threading.Thread(target=loop,daemon=True)
        self.monitor.start()

    def close(self):
        self.stop_event.set()
        current = threading.current_thread()
        if self.monitor and self.monitor is not current:
            self.monitor.join(timeout=2)
        if self.worker and self.worker is not current:
            self.worker.join(timeout=2)

    @staticmethod
    def in_flight(path, st):
        """True while a client is probably still writing this file."""
        return in_flight(path, st)

    @staticmethod
    def error_kind(exc):
        """Map an exception to one of ERROR_KINDS so failures are explainable."""
        return error_kind(exc)

    def _record(self, bucket, agent, path, kind, message, persist=True):
        """Record one failure/degradation, deduped by path within its bucket."""
        entry = {'agent': agent, 'path': str(path), 'kind': kind,
                 'message': redact(message, self.secrets())[:400], 'at': time.time()}
        limit = MAX_ERRORS if bucket == 'errors' else MAX_DEGRADED
        with self.lock:
            p = self.progress
            current = [e for e in p.get(bucket, []) if e.get('path') != entry['path']]
            p[bucket] = (current + [entry])[-limit:]
            if bucket == 'errors':
                p['failed'] = len(p['errors'])
        if persist and bucket == 'errors' and entry['path']:
            self.failed_paths.add(entry['path'])
            try:
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO source_failures VALUES(?,?,?,?,?)',
                               (entry['path'], entry['agent'], entry['kind'], entry['message'], entry['at']))
            except sqlite3.Error:
                # Bookkeeping must never abort a round; the in-memory list still shows it.
                pass
        return entry

    def _fail(self, agent, path, exc, persist=True):
        return self._record('errors', agent, path, error_kind(exc), error_message(exc), persist)

    def _clear_failure(self, path):
        """A later success removes the persisted failure for that exact path."""
        path = str(path)
        with self.lock:
            p = self.progress
            p['errors'] = [e for e in p.get('errors', []) if e.get('path') != path]
            p['failed'] = len(p['errors'])
        if path not in self.failed_paths:
            return  # nothing was recorded for this source: no DB traffic on the happy path
        self.failed_paths.discard(path)
        try:
            with self.db() as db:
                db.execute('DELETE FROM source_failures WHERE path=?', (path,))
        except sqlite3.Error:
            pass

    def _prune_failures(self, keep):
        """Drop failures for sources that no longer exist in this enumeration."""
        if not self.failed_paths:
            return
        try:
            with self.db() as db:
                for (path,) in db.execute('SELECT path FROM source_failures').fetchall():
                    if path not in keep:
                        db.execute('DELETE FROM source_failures WHERE path=?', (path,))
                        self.failed_paths.discard(path)
        except sqlite3.Error:
            pass

    def _skip_in_flight(self):
        """Count a skipped in-flight source without calling it a failure."""
        with self.lock:
            self.progress['inFlight'] += 1
            self.progress['skipped'] += 1

    def _tick(self, done):
        """Publish progress; processed is monotonic and can never exceed total."""
        with self.lock:
            p = self.progress
            total = p.get('total') or 0
            p['processed'] = max(p.get('processed') or 0, min(done, total))

    @staticmethod
    def _parse(agent, path, content, secrets):
        """Parse one source, retrying transient lock/permission errors once."""
        last = None
        for attempt in range(2):
            report = {}
            try:
                if agent == 'opencode':
                    return parse_opencode_database(path, content, secrets, report,
                                                   content_cutoff=time.time()-CONTENT_DAYS*86400), report
                return parse_native(agent, path, content, secrets, report), report
            except (OSError, sqlite3.OperationalError) as exc:
                if isinstance(exc, sqlite3.OperationalError) and error_kind(exc) != 'locked':
                    raise
                last = exc
                if attempt == 0:
                    time.sleep(RETRY_BACKOFF)
        raise last

    def failed_sources(self):
        """Current failure list: durable rows merged with the live round."""
        with self.lock:
            live = {e['path']: dict(e) for e in self.progress.get('errors', []) if e.get('path')}
        merged = {}
        try:
            with self.db() as db:
                for path, agent, kind, message, at in db.execute(
                        'SELECT path,agent,kind,message,at FROM source_failures').fetchall():
                    merged[path] = {'agent': agent, 'path': path, 'kind': kind, 'message': message, 'at': at}
        except sqlite3.Error:
            merged = {}
        merged.update(live)
        return sorted(merged.values(), key=lambda e: e.get('at') or 0, reverse=True)

    def retry(self, paths=None, wait=False):
        """Re-scan failed sources, bypassing the fingerprint short-circuit.

        `paths=None` retries every current failure. Paths are validated by
        native-root containment, so a caller can never turn this into an
        arbitrary file read. Re-ingest is idempotent: every table is written
        with INSERT OR REPLACE keyed by a stable id, so a re-scan of an
        already-indexed file replaces identical rows instead of duplicating.
        """
        current = self.failed_sources()
        if isinstance(paths, str):
            paths = [paths]
        elif paths is not None and not isinstance(paths, (list, tuple, set)):
            raise ValueError('paths 必须是来源路径数组')
        wanted = list(dict.fromkeys(str(p) for p in paths)) if paths is not None else [e['path'] for e in current]
        targets, rejected, gone = [], [], []
        for path in wanted:
            agent = source_agent(self.home, path)
            if agent:
                targets.append((agent, Path(path)))
            elif Path(path).exists():
                rejected.append(path)  # outside every native root: refuse
            else:
                gone.append(path)
        for path in gone:
            self._clear_failure(path)
        result = {'ok': True, 'requested': len(wanted), 'retried': len(targets),
                  'rejected': rejected, 'gone': gone}
        if not targets:
            result.update(remaining=len(self.failed_sources()))
            return result
        if wait:
            self.collect(sources=targets, force=True)
            result.update(remaining=len(self.failed_sources()), collector=self.snapshot())
            return result
        if not self.refresh_thread(self.collect, sources=targets, force=True):
            result.update(ok=False, reason='采集正在进行，下一轮会自动重读这些来源', queued=0,
                          remaining=len(current))
            return result
        result['queued'] = len(targets)
        return result

    def refresh_thread(self, target, **kwargs):
        """Start a collector thread unless one is already running."""
        with self.lock:
            if self.collecting:
                return False
            worker = threading.Thread(target=target, kwargs=kwargs, daemon=True)
            self.worker = worker
            worker.start()
        return True

    def collect(self, sources=None, force=False):
        """Run one collection round. At most one round may own `progress`.

        `sources` (list of `(agent, Path)`) restricts the round to those files
        (used by `retry`); None enumerates every native source. `force` ignores
        the stored fingerprint so an unchanged file is re-parsed.
        """
        retry_mode = sources is not None
        with self.lock:
            if self.collecting:
                # A second concurrent round would double-count `processed`
                # against a shared dict (the 1985/993 symptom); refuse it.
                return self.status()
            self.collecting = True
            self.worker = threading.current_thread()
            previous = self.progress
        self.progress = {'state': 'reading', 'mode': 'retry' if retry_mode else 'full',
                         'processed': 0, 'total': 0,
                         'errors': [dict(e) for e in previous.get('errors', [])] if retry_mode else [],
                         'degraded': [dict(e) for e in previous.get('degraded', [])] if retry_mode else [],
                         'unchanged': 0, 'inFlight': 0, 'empty': 0, 'skipped': 0, 'failed': 0,
                         'sources': [dict(s) for s in previous.get('sources', [])] if retry_mode else [],
                         'startedAt': time.time()}
        try:
            try:
                if sources is None:
                    sources, info = native_files(self.home)
                    self.progress['sources'] = info
                    # One aggregated line, not one per session: a missing optional
                    # dependency is one fact about the interpreter, and nine
                    # repetitions of it read as nine broken sources.
                    for entry in info:
                        gap = entry.get('degraded')
                        if not gap:
                            continue
                        parts = []
                        if gap.get('fallback'):
                            parts.append(f'{gap["fallback"]} 个会话的最新一代是压缩日志，'
                                         '已回退到同目录上一代未压缩日志')
                        if gap.get('unreadable'):
                            parts.append(f'{gap["unreadable"]} 个会话只有压缩日志可读，本轮跳过')
                        self._record('degraded', entry['agent'], '', 'missing-dependency',
                                     '当前解释器不支持 zstandard：' + '；'.join(parts)
                                     + '。桌面版已内置该依赖；源码模式需要给运行 Agent Hub 的解释器安装 zstandard。', False)
                else:
                    sources = [(agent, Path(path)) for agent, path in sources]
                with self.lock:
                    self.progress['total'] = len(sources)
                secrets = self.secrets()
                done = 0
                for agent, path in sources:
                    with self.lock:
                        settings = self.settings()
                        if not settings['enabled'] or self.stop_event.is_set():
                            self.progress['state'] = 'stopped'
                            return
                    done = self._collect_one(agent, path, settings, secrets, force, done)
                    if done < 0:
                        if self.progress.get('state') == 'stopped':
                            return
                        self.progress['settingsChanged'] = True
                        break
                self.cleanup()
                # Keep legacy records readable without running the former engine.
                for gateway_path in (self.root/'gateway/config/magpie/usage.jsonl',self.root/'gateway/config/agent-hub/usage.jsonl'):
                    if gateway_path.exists() and self.settings()['enabled']:
                        try:
                            self.ingest_gateway(gateway_path)
                            self.progress['sources'].append({'agent':'gateway','path':str(gateway_path),'state':'available'})
                        except (OSError,ValueError) as exc:
                            self._record('errors','gateway',gateway_path,error_kind(exc),error_message(exc),False)
                if not retry_mode:
                    self._prune_failures({str(path) for _, path in sources})
                self.progress['state'] = 'complete'
            except Exception as exc:
                self.progress['state'] = 'failed'
                self._fail('collector', '', exc, persist=False)
            finally:
                self.progress['finishedAt'] = time.time()
                self.progress['failed'] = len(self.progress.get('errors') or [])
        finally:
            with self.lock:
                self.collecting = False

    def _collect_one(self, agent, path, settings, secrets, force, done):
        """Index a single source; a failure is recorded and never aborts the round.

        Returns the new `done` count, or -1 when the round must stop because
        settings changed underneath it.
        """
        try:
            st = path.stat()
        except OSError as exc:
            self._fail(agent, path, exc)
            self._tick(done + 1)
            return done + 1
        if st.st_size == 0:
            # An empty native log has nothing to index. Fresh means "not written
            # yet" (skip, retry next round); old just means empty, never a failure.
            if fresh(st):
                self._skip_in_flight()
            else:
                with self.lock:
                    self.progress['empty'] += 1
                    self.progress['skipped'] += 1
            self._tick(done + 1)
            return done + 1
        if agent != 'opencode' and in_flight(path, st):
            self._skip_in_flight()
            self._tick(done + 1)
            return done + 1
        if path.suffix in ('.zstd', '.zst') and not zstd_available():
            # One fact about this interpreter, already reported once by the
            # aggregate `missing-dependency` entry. Repeating it per session
            # turns a single missing optional dependency into hundreds of
            # "broken sources" and hides the real gaps.
            with self.lock:
                self.progress['skipped'] += 1
            self._tick(done + 1)
            return done + 1
        fingerprint = f'{PARSER_REVISION}:{st.st_size}:{st.st_mtime_ns}:{settings["content"]}:{settings["retentionDays"]}'
        if agent == 'opencode':
            # SQLite may update only the WAL until the next checkpoint.
            wal = Path(str(path) + '-wal')
            try:
                wal_stat = wal.stat()
                fingerprint += f':wal:{wal_stat.st_size}:{wal_stat.st_mtime_ns}'
            except FileNotFoundError:
                fingerprint += ':no-wal'
        if not force:
            with self.db() as db:
                old = db.execute('SELECT fingerprint FROM sources WHERE path=?', (str(path),)).fetchone()
            if old and old[0] == fingerprint:
                with self.lock:
                    self.progress['unchanged'] += 1
                    self.progress['skipped'] += 1
                self._tick(done + 1)
                return done + 1
        try:
            parsed, report = self._parse(agent, path, settings['content'], secrets)
        except Exception as exc:
            if agent != 'opencode' and fresh(st) and error_kind(exc) in ('locked', 'unreadable', 'parse'):
                # Still being written (e.g. a dsh header not flushed yet): not a
                # failure, the next round reads it naturally.
                self._skip_in_flight()
                self._tick(done + 1)
                return done + 1
            self._fail(agent, path, exc)
            self._tick(done + 1)
            return done + 1
        items = parsed if isinstance(parsed, list) else [parsed]
        if report.get('oversized') or report.get('truncated'):
            self._note_limits(agent, path, items, report)
        elif st.st_size > 128 and not report.get('bytes') and not any(p.requests or p.messages for p in items):
            # A compressed frame that is still open decodes to nothing without
            # raising; saying "no events" would be a silent data loss.
            if fresh(st):
                self._skip_in_flight()
                self._tick(done + 1)
                return done + 1
            self._record('degraded', agent, path, 'unreadable',
                         f'解压后为空（源文件 {st.st_size} 字节），可能是尚未写完或被截断的压缩帧', False)
        with self.lock:
            current = self.settings()
            if not current['enabled'] or self.stop_event.is_set():
                self.progress['state'] = 'stopped'
                return -1
            if current != settings:
                return -1
            try:
                for item in items:
                    self.ingest(item, settings)
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO sources VALUES(?,?)', (str(path), fingerprint))
            except Exception as exc:
                self._fail(agent, path, exc)
                self._tick(done + 1)
                return done + 1
        self._clear_failure(path)
        if report.get('invalid'):
            self._record('degraded', agent, path, 'parse',
                         f'跳过 {report["invalid"]} 条无法解析的 OpenCode 消息；其他记录已保留。', False)
        self._tick(done + 1)
        return done + 1

    def _note_limits(self, agent, path, parsed, report):
        """A source narrowed by MAX_LINE/MAX_DECODED is explained, not silently cut."""
        limits = f'单行 {human_bytes(MAX_LINE)} / 单文件解压 {human_bytes(MAX_DECODED)}'
        parts = []
        if report.get('oversized'):
            parts.append(f'跳过 {report["oversized"]} 段超过单行上限的内容')
        if report.get('truncated'):
            parts.append('解压量超过上限后停止读取，已保留之前的内容')
        message = f'超过安全上限（{limits}）：' + '；'.join(parts)
        items = parsed if isinstance(parsed, list) else [parsed]
        if any(item.requests or item.messages for item in items):
            self._record('degraded', agent, path, 'too-large', message, False)
        else:
            self._record('errors', agent, path, 'too-large', message + '；本文件无可索引内容')

    def ingest(self, parsed, settings=None):
        settings = settings or self.settings()
        price_config=self.config()
        cutoff = retention_cutoff(settings)
        content_cutoff = max(cutoff, time.time() - CONTENT_DAYS * 86400)
        with self.db() as db:
            # Parser migrations may replace a synthetic native key with an exact
            # response ID. Remove only superseded rows from this same source.
            query = "SELECT id,data FROM requests WHERE json_extract(data,'$.source')=?"
            args = [str(parsed.path)]
            if parsed.agent == 'opencode':
                # One SQLite source contains many independent sessions.
                query += ' AND agent=? AND sid=?'
                args.extend([parsed.agent, parsed.sid])
            for old_id, data in db.execute(query, args).fetchall():
                previous=json.loads(data)
                if previous.get('sourceType')=='native' and previous.get('source')==str(parsed.path) and old_id not in parsed.requests:
                    db.execute('DELETE FROM requests WHERE id=?',(old_id,))
            for row in parsed.requests.values():
                if row['time'] < cutoff:
                    continue
                existing = db.execute('SELECT data FROM requests WHERE id=?',(row['id'],)).fetchone()
                if existing:
                    previous = json.loads(existing[0])
                    if previous.get('sourceType') in ('gateway','gateway+native'):
                        row = self.merge_gateway(previous,row)
                    if previous.get('cost') is not None:
                        for field in ('cost','costEvidence','costNote','priceAsOf'):
                            if field in previous:
                                row[field] = previous[field]
                self.price(row,price_config)
                if row['cost'] is not None:
                    row.setdefault('priceAsOf',time.time())
                db.execute('INSERT OR REPLACE INTO requests VALUES(?,?,?,?,?)', (row['id'], row['agent'], row['sessionId'], row['time'], json.dumps(row)))
            for row in parsed.messages if settings['content'] else []:
                if row['time'] < content_cutoff:
                    continue
                db.execute('INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?)', (row['id'], parsed.agent, parsed.sid, row['time'], json.dumps(row)))
            if parsed.sid:
                times = [r['time'] for r in parsed.requests.values() if r['time'] >= cutoff]
                if times:
                    row = {'agent': parsed.agent, 'sessionId': parsed.sid, 'parentSessionId': parsed.parent,
                           'cwd': parsed.cwd, 'title': parsed.title if settings['content'] else None,
                           'startedAt': parsed.started, 'lastAt': max(times), 'source': str(parsed.path),
                           'compactions': [r for r in parsed.compactions if r.get('time') and r['time'] >= cutoff]}
                    old=db.execute('SELECT data FROM sessions WHERE id=?',(key(parsed.agent,parsed.sid),)).fetchone()
                    if old and json.loads(old[0]).get('gatewayAgent'):
                        row['gatewayAgent']=json.loads(old[0])['gatewayAgent']
                    db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)', (key(parsed.agent, parsed.sid), parsed.agent, parsed.sid, max(times), json.dumps(row)))
            for row in parsed.quotas:
                if row.get('time') and row['time'] >= cutoff:
                    db.execute('INSERT OR REPLACE INTO quotas VALUES(?,?,?)', (key(row), row['time'], json.dumps(row)))

    @staticmethod
    def merge_gateway(gateway, native):
        result=dict(gateway)
        result.update(sourceType='gateway+native',nativeSource=native.get('nativeSource') or native['source'],
                      nativeEvidence=native.get('nativeEvidence') or native.get('evidence'))
        # A response ID proves identity even if a client omitted its session header.
        if native.get('sessionId'):
            result['sessionId']=native['sessionId']
        if native.get('window'):
            result.update(window=native['window'],windowEvidence=native.get('windowEvidence'))
        if result.get('cost') is None and native.get('cost') is not None:
            for field in ('cost','costEvidence','costNote','priceAsOf'):
                if field in native:result[field]=native[field]
        return result

    def ingest_gateway(self,path,mapping=None):
        from agent_hub.gateway.observation import read_requests
        if not self.settings()['enabled']:
            return
        cutoff=retention_cutoff(self.settings())
        price_config=self.config()
        with self.lock, self.db() as db:
            identities={}
            for agent,data in db.execute('SELECT agent,data FROM requests'):
                native=json.loads(data)
                identities.setdefault(native['nativeId'],set()).add(agent)
            report=[]
            for row in read_requests(path,mapping,identities,self.secrets(),report):
                if row['time']<cutoff:
                    continue
                previous=db.execute('SELECT data FROM requests WHERE id=?',(row['id'],)).fetchone()
                if previous:
                    native=json.loads(previous[0])
                    if native.get('sourceType') in ('native','gateway+native'):
                        row=self.merge_gateway(row,native)
                    if native.get('cost') is not None:
                        for field in ('cost','costEvidence','costNote','priceAsOf'):
                            if field in native:row[field]=native[field]
                self.price(row,price_config)
                # Count billed usage of handed-on attempts, but keep final prompt
                # occupancy separate. A final success is one successful request.
                dimensions=('input','output','cacheRead','cacheWrite')
                row['billableUsage']={k:sum(a['usage'][k] or 0 for a in row['attemptDetails'])
                    if all(a['usage'][k] is not None for a in row['attemptDetails']) else None for k in dimensions}
                row['billableUsage']['reasoning']=row['usage']['reasoning'] if row['attempts']==1 else None
                if row['attempts']==1 and row['usage']['input'] is None and not any(row['billableUsage'].values()):
                    row['billableUsage']={k:None for k in row['billableUsage']}
                if row['attempts']>1:
                    costs=[]
                    for a in row['attemptDetails']:
                        attempt={**row,'provider':a['provider'],'model':a['model'],'servedModel':None,
                                 'usage':{**a['usage'],'inputIncludesCache':False},'cost':None}
                        self.price(attempt,price_config)
                        costs.append(attempt['cost'])
                    row['cost']=sum(costs) if all(c is not None for c in costs) and len(costs)==row['attempts'] else None
                    row['costEvidence']='configured-model-price' if row['cost'] is not None else None
                db.execute('INSERT OR REPLACE INTO requests VALUES(?,?,?,?,?)',
                    (row['id'],row['agent'],row['sessionId'],row['time'],json.dumps(row)))
                if row['sessionId']:
                    skey=key(row['agent'],row['sessionId'])
                    old=db.execute('SELECT data FROM sessions WHERE id=?',(skey,)).fetchone()
                    session=json.loads(old[0]) if old else {'agent':row['agent'],'sessionId':row['sessionId'],
                        'parentSessionId':row.get('parentSessionId'),'cwd':None,'title':None,'compactions':[],
                        'source':str(path),'startedAt':row['time'],'lastAt':row['time']}
                    session.update(lastAt=max(session.get('lastAt') or 0,row['time']),gatewayAgent=row.get('sourceAgent') or row['agent'])
                    db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)',
                        (skey,row['agent'],row['sessionId'],session['lastAt'],json.dumps(session)))
        # A partially readable gateway ledger is evidence, not a silent no-op.
        # Persisted only in memory: the gateway file is optional enrichment and
        # must not turn into a blocking "failed source" the user cannot fix.
        for entry in report:
            bucket='degraded' if entry['kind'] in ('too-large','parse') else 'errors'
            self._record(bucket,'gateway',entry['path'],entry['kind'],entry['message'],False)

    def price(self, row, config=None):
        if row['cost'] is not None or not row['provider'] or not row['model']:
            return
        from hub import agent_connection
        provider = ((self.config() if config is None else config).get('providers') or {}).get(row['provider'])
        if not provider:
            return
        conn = agent_connection(provider, row['agent'])
        priced_model = row['servedModel'] or row['model']
        models = [m for m in conn['models'] if m.get('id') == priced_model]
        if len(models) != 1:
            return
        costs = models[0].get('cost') or {}
        u = row['usage']
        fresh = max(0, u['input'] - (u['cacheRead'] or 0)) if u['inputIncludesCache'] and u['input'] is not None else u['input']
        parts = {'input': fresh, 'output': u['output'], 'cacheRead': u['cacheRead'], 'cacheWrite': u['cacheWrite']}
        # Missing token dimensions or missing applicable prices => unknown, not free.
        if any(n is None or n > 0 and number(costs.get(k)) is None for k, n in parts.items()):
            return
        row.update(cost=sum(n * (costs.get(k) or 0) / 1e6 for k, n in parts.items()), costEvidence='configured-model-price',
                   costNote='按采集时该 Agent 连接中模型单价估算 USD；非实际扣费；不能推断订阅额度消耗')

    def cleanup(self, clear=False):
        with self.lock:
            settings = self.settings()
            cutoff = retention_cutoff(settings)
            content_cutoff = max(cutoff, time.time() - CONTENT_DAYS * 86400)
            if clear:
                settings.update(clearedBefore=time.time(), enabled=False)
                temp = self.settings_path.with_suffix('.tmp')
                temp.write_text(json.dumps(settings), encoding='utf-8')
                temp.replace(self.settings_path)
            with self.db() as db:
                if clear:
                    db.execute('''UPDATE session_title_control SET enabled=0,epoch=epoch+1,
                        cleared_before=MAX(cleared_before,?) WHERE id=1''', (settings['clearedBefore'],))
                for table in ('session_title_metadata', 'session_manual_titles'):
                    db.execute(f'DELETE FROM {table}' + ('' if clear else ' WHERE at < ?'),
                               () if clear else (cutoff,))
                for table in ('requests', 'sessions', 'messages', 'quotas'):
                    db.execute(f'DELETE FROM {table}' + ('' if clear else ' WHERE at < ?'), () if clear else (content_cutoff if table == 'messages' else cutoff,))
                if not clear:
                    # Bound retained text, independently from usage history.
                    db.execute('''DELETE FROM messages WHERE id IN (
                        SELECT id FROM (SELECT id, SUM(length(CAST(data AS BLOB))) OVER
                        (ORDER BY at DESC,id DESC) AS kept FROM messages) WHERE kept > ?
                    )''',(CONTENT_MAX_BYTES,))
                    for sid, raw in db.execute('SELECT id,data FROM sessions WHERE at < ?',(content_cutoff,)).fetchall():
                        row = json.loads(raw)
                        if row.get('title'):
                            row['title'] = None
                            db.execute('UPDATE sessions SET data=? WHERE id=?',(json.dumps(row),sid))
                if clear:
                    db.execute('DELETE FROM sources')
                    db.execute('DELETE FROM source_failures')
                    self.failed_paths.clear()
            # DELETE is also applied to WAL; secure_delete scrubs old cell payloads.
            with self.db() as db:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if clear:
                with self.db() as db:
                    db.execute('VACUUM')
        return {'ok': True, 'cleared': clear}

    def subscription_activity(self, provider):
        """Latest indexed evidence for a particular Hub subscription route."""
        if provider not in ('codex-chatgpt', 'zcode-subscription'):
            raise ValueError('不支持的订阅来源')
        with self.db() as db:
            query = ("SELECT data FROM requests WHERE json_extract(data,'$.provider')=? "
                     "AND json_extract(data,'$.sourceType') IN ('gateway','gateway+native')")
            latest = db.execute(query + ' ORDER BY at DESC LIMIT 1', (provider,)).fetchone()
            success = db.execute(query + " AND json_extract(data,'$.status')='succeeded' ORDER BY at DESC LIMIT 1", (provider,)).fetchone()
            failure = db.execute(query + " AND json_extract(data,'$.status') IN ('failed','interrupted') ORDER BY at DESC LIMIT 1", (provider,)).fetchone()
        def public(hit):
            if not hit: return None
            row=json.loads(hit[0])
            return {k:row.get(k) for k in ('time','status','agent','model','httpStatus','error')}
        return {'last':public(latest),'lastSuccess':public(success),'lastError':public(failure),
                'collectionEnabled':bool(self.settings().get('enabled')),
                'source':'Hub 本机观测索引；可能有采集延迟'}

    def query(self, filters=None):
        self.cleanup()
        f = filters or {}
        now = time.time()
        requested_start = stamp(f.get('start'))
        start = requested_start if requested_start is not None else (0 if f.get('days') == 'all' else now - min(365, max(1, int(f.get('days') or 7))) * 86400)
        end = stamp(f.get('end')) or now
        if start > end:
            raise ValueError('开始时间不能晚于结束时间')
        agent = f.get('agent')
        if agent and agent not in AGENTS:
            raise ValueError('不支持的 Agent')
        with self.db() as db:
            requests = [json.loads(x[0]) for x in db.execute('SELECT data FROM requests WHERE at>=? AND at<=? ORDER BY at DESC,id', (start, end))]
            session_data = {key(r['agent'], r['sessionId']): r for (data,) in db.execute('SELECT data FROM sessions') for r in [json.loads(data)]}
        q = str(f.get('q') or '').lower()
        requests = [r for r in requests if (not agent or r['agent'] == agent)
                    and (not f.get('model') or f['model'] in (r['model'], r['servedModel']))
                    and (not f.get('provider') or r['provider'] == f['provider'])
                    and (f.get('providerUnknown') != '1' or not r['provider'])
                    and (f.get('modelUnknown') != '1' or not r['model'] and not r['servedModel'])
                    and (not f.get('status') or r['status'] == f['status'])
                    and (not f.get('session') or r['sessionId'] == f['session'])
                    and (not f.get('request') or r['id'] == f['request'])
                    and (not q or q in json.dumps({k: r[k] for k in ('model', 'servedModel', 'provider', 'nativeId', 'error', 'sessionId')}, ensure_ascii=False).lower()
                         or q in json.dumps(session_data.get(key(r['agent'], r['sessionId']), {}), ensure_ascii=False).lower())]
        sessions = {}
        for r in requests:
            if not r['sessionId']:
                continue
            sid = key(r['agent'], r['sessionId'])
            s = sessions.setdefault(sid, {**session_data.get(sid, {}), 'id': sid, 'agent': r['agent'],
                                          'sessionId': r['sessionId'], 'requests': [], 'models': []})
            s['requests'].append(r)
            if (r['servedModel'] or r['model']) not in s['models']:
                s['models'].append(r['servedModel'] or r['model'])
        for s in sessions.values():
            s['summary'] = summarize(s['requests'])
            s['points'] = [{'id': r['id'], 'time': r['time'], 'input': r['usage']['context'], 'cacheRead': r['usage']['cacheRead'],
                            'window': r['window'], 'precision': r['usage'].get('contextPrecision','unknown'),
                            'fill': r['usage']['context'] / r['window'] if r['window'] and r['usage']['context'] is not None else None}
                           for r in reversed(s['requests'])]
            vals = [p['input'] for p in s['points'] if p['input'] is not None]
            s['peakInput'] = max(vals) if vals else None
            s['pointCount'] = len(s['points'])
            s['points'] = s['points'][-160:]
            s['rangeFirst'] = min(r['time'] for r in s['requests'])
            s['rangeLast'] = max(r['time'] for r in s['requests'])
            s['requests'] = [{k: r[k] for k in ('id','time','model','servedModel','status')} for r in s['requests']]
        page = max(1, int(f.get('page') or 1))
        page_size = min(100, max(1, int(f.get('pageSize') or 30)))
        pages = max(1, math.ceil(len(requests) / page_size))
        page = min(page, pages)
        summary = summarize(requests)
        distribution = {}
        for dim in ('agent', 'provider', 'model'):
            groups = {}
            for r in requests:
                value = r['servedModel'] or r['model'] if dim == 'model' else r[dim]
                groups.setdefault(value or '未知', []).append(r)
            distribution[dim] = [{'name': k, **summarize(v)} for k, v in groups.items()]
        trend = {}
        for r in requests:
            day = datetime.fromtimestamp(r['time'], timezone.utc).strftime('%Y-%m-%d')
            trend.setdefault(day, []).append(r)
        # 长范围降采样（性能审计：一年逐日 365 列会让概览页每次轮询重建两千多节点）。
        # 范围超过 120 天时按 ISO 周聚合到周首（周一），同一周的请求合并后 summarize 汇总；
        # granularity 随响应如实标注，前端轴标同步写「按周」，不得让周聚合冒充逐日。
        trend_granularity = 'week' if (end - start) > 120 * 86400 else 'day'
        if trend_granularity == 'week':
            weekly = {}
            for day, rows in trend.items():
                d = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                weekly.setdefault((d - timedelta(days=d.weekday())).strftime('%Y-%m-%d'), []).extend(rows)
            trend = weekly
        with self.db() as db:
            quotas = [json.loads(x[0]) for x in db.execute('SELECT data FROM quotas WHERE at>=? AND at<=? ORDER BY at DESC LIMIT 1', (start,end))]
        if agent and agent != 'codex':
            quotas = []
        ordered_sessions = sorted(sessions.values(), key=lambda s: s['rangeLast'], reverse=True)
        session_pages = max(1, math.ceil(len(ordered_sessions) / 30))
        session_page = min(session_pages, max(1, int(f.get('sessionPage') or 1)))
        return {'range': {'start': start, 'end': end, 'timezone': 'UTC'}, 'filters': f, 'quotas': quotas,
                'summary': summary, 'distribution': distribution,
                'trend': [{'date': k, **summarize(v)} for k, v in sorted(trend.items())],
                'trendGranularity': trend_granularity,
                'requests': requests[(page-1)*page_size:page*page_size], 'total': len(requests), 'page': page, 'pages': pages,
                'sessions': ordered_sessions[(session_page-1)*30:session_page*30], 'sessionTotal': len(ordered_sessions),
                'sessionPage': session_page, 'sessionPages': session_pages,
                'unassociated': sum(not r['sessionId'] for r in requests),
                'models': sorted(set(x for r in requests for x in (r['model'], r['servedModel']) if x)),
                'status': self.status()}

    def detail(self, request_id, filters=None):
        with self.db() as db:
            hit = db.execute('SELECT data FROM requests WHERE id=?', (request_id,)).fetchone()
            if not hit:
                raise ValueError('记录不存在，可能已超过保留期或被清理')
            row = json.loads(hit[0])
        return row

    def session_detail(self, agent, sid, filters=None):
        f = {**(filters or {}), 'agent': agent, 'session': sid, 'pageSize': 100}
        data = self.query(f)
        with self.db() as db:
            messages = [json.loads(x[0]) for x in db.execute('SELECT data FROM messages WHERE agent=? AND sid=? AND at>=? AND at<=? ORDER BY at',
                                                          (agent, sid, data['range']['start'], data['range']['end']))]
        session = data['sessions'][0] if data['sessions'] else None
        source, error = 'retained' if messages else None, None
        # Native transcripts already belong to the client. Opening one does not
        # require or enable a second copy in Hub, just as in Magpie. Never accept
        # a path from the HTTP caller, and verify both its root and native id.
        if session and session.get('source'):
            path = Path(session['source'])
            roots = {'claude': Path(os.environ.get('CLAUDE_CONFIG_DIR') or self.home/'.claude')/'projects',
                     'codex': Path(os.environ.get('CODEX_HOME') or self.home/'.codex'),
                     'pi': Path(os.environ.get('PI_CODING_AGENT_DIR') or self.home/'.pi/agent')/'sessions',
                     'dsh': Path(os.environ.get('DSH_HOME') or self.home/'.dsh')/'sessions'}
            allowed = ([roots[agent]/'sessions', roots[agent]/'archived_sessions'] if agent == 'codex'
                       else [roots[agent]] if agent in roots else [])
            safe = (path.resolve() == opencode_database(self.home).resolve() if agent == 'opencode'
                    else any(path.resolve().is_relative_to(root.resolve()) for root in allowed))
            if safe:
                try:
                    if agent == 'opencode':
                        candidates = parse_opencode_database(path,True,self.secrets(),session_id=sid)
                        if not candidates:
                            raise ValueError('OpenCode 原生会话已不存在')
                        parsed = candidates[0]
                    else:
                        parsed = parse_native(agent,path,True,self.secrets())
                    if parsed.sid != sid:
                        raise ValueError('原生会话身份已变化，未展示该文件内容')
                    lower = max(data['range']['start'],retention_cutoff(self.settings()))
                    messages = [m for m in parsed.messages if lower <= m['time'] <= data['range']['end']]
                    source = 'native-on-demand'
                    session['title'] = parsed.title or session.get('title')
                except (OSError,ValueError,sqlite3.Error) as exc:
                    error = redact(str(exc),self.secrets())
        if not messages and self.settings()['content'] and session:
            # Gateway snapshots include replayed input; they are exchanges, not
            # a second set of native messages. No caller-supplied file path.
            gateway_agent=session.get('gatewayAgent') or agent
            digest=hashlib.sha256(json.dumps([gateway_agent,sid],separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            directories=[self.root/('gateway/config/'+name+'/gateway-conversations') for name in ('magpie','agent-hub')]
            paths=sorted((p,directory) for directory in directories for p in directory.glob('????-??-??/'+digest+'/turns.jsonl'))
            if paths:
                lower=max(data['range']['start'],retention_cutoff(self.settings()),time.time()-CONTENT_DAYS*86400)
                try:
                    for path,directory in paths:
                        if not path.resolve().is_relative_to(directory.resolve()):continue
                        for i,turn in enumerate(open_lines(path)):
                            at=stamp(turn.get('time'))
                            if turn.get('agent')!=gateway_agent or turn.get('session')!=sid or not at or not lower<=at<=data['range']['end']:
                                continue
                            messages.append({'id':key(agent,sid,str(path),i,at),'time':at,'role':'gateway/exchange · 输入含重放历史',
                                'body':redact(json.dumps({k:turn[k] for k in ('model','status','input','output','cut') if k in turn},ensure_ascii=False),self.secrets())})
                    if messages:source='gateway-on-demand'
                except (OSError,ValueError) as exc:error=redact(str(exc),self.secrets())
        return {'session': session, 'messages': messages, 'contentSource': source, 'contentError': error,
                'contentEnabled': self.settings()['content'], 'range': data['range']}

    def secrets(self):
        values = []
        for provider in (self.config().get('providers') or {}).values():
            for block in [provider, *(provider.get('agents') or {}).values()]:
                if isinstance(block, dict):
                    values.extend(v for k,v in block.items() if k.lower() in ('apikey','openaiapikey'))
                    if block.get('apiKeyEnv'):
                        values.append(os.environ.get(block['apiKeyEnv']))
        return values


def summarize(rows):
    result = {'calls': len(rows), 'succeeded': 0, 'failed': 0, 'interrupted': 0, 'unknown': 0,
              'unpriced': 0, 'priced': 0, 'cost': None, 'retried': None, 'attempts': None,
              'tokens': {}, 'durationMs': None, 'durationSamples': 0, 'ttftMs': None, 'speed': None}
    costs, durations = [], []
    for r in rows:
        result[r['status']] += 1
        if r['cost'] is None:
            result['unpriced'] += 1
        else:
            result['priced'] += 1
            costs.append(r['cost'])
        if r['durationMs'] is not None:
            durations.append(r['durationMs'])
    result['cost'] = sum(costs) if costs else None
    for field in ('input', 'output', 'reasoning', 'cacheRead', 'cacheWrite', 'context'):
        values = [(r.get('billableUsage',{}).get(field) if field!='context' and 'billableUsage' in r
                   else fresh_input(r['usage']) if field=='input' else r['usage'][field]) for r in rows]
        values = [v for v in values if v is not None]
        result['tokens'][field] = {'value': sum(values) if values else None, 'samples': len(values)}
    if durations:
        result.update(durationMs=sum(durations)/len(durations), durationSamples=len(durations))
    for target, source in (('ttftMs','ttftMs'),('speed','outputSpeed')):
        values=[r[source] for r in rows if r.get(source) is not None]
        result[target]=sum(values)/len(values) if values else None
        result[target+'Samples']=len(values)
    known=[r for r in rows if r.get('attempts') is not None]
    if known:
        result.update(attempts=sum(r['attempts'] for r in known),retried=sum(bool(r['retried']) for r in known),attemptSamples=len(known))
    return result
