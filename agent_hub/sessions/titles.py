"""Bounded, on-demand titles for the recent native session list.

Only indexed session paths within the native client roots are considered.
Native titles are read on demand and kept in memory. Saved manual and gateway
titles are overlaid separately from the local observation database.
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import threading
import time
import weakref
from collections import OrderedDict
from pathlib import Path

from agent_hub.observability.store import opencode_database, redact, stamp, retention_cutoff

_READ_BYTES = 1024 * 1024
_MAX_RECORDS = 3000
_MAX_TITLE = 60
_CACHE_LIMIT = 180
_SCAN_BUDGET_SECONDS = 2.0
_SQL_BUDGET_SECONDS = .15
_CACHE = weakref.WeakKeyDictionary()
_LOCK = threading.RLock()
_GENERIC = re.compile(
    r'^(?:继续(?:吧|啊|呀|下去|执行|完成|工作|当前任务|上一步|上一条|上次|之前|'
    r'之前的工作|原来的任务|刚才的任务|上述工作|以上|上个任务|前面的工作)?|'
    r'好的?|好吧|可以|行|收到|明白|确认|批准|是的?|对|嗯|谢谢|'
    r'未命名(?:会话|对话)?|新(?:会话|对话)|'
    r'(?:new|untitled)(?:\s+(?:session|conversation|chat))?|'
    r'continue|resume|yes|okay|ok|sure|go\s+ahead|proceed|do\s+it)[\s.!！?？,，。]*$',
    re.I,
)
_SYSTEM_START = re.compile(
    r'^(?:#\s*(?:AGENTS\.md|CLAUDE\.md)\s*(?:instructions|指令|说明)|'
    r'#\s*(?:System|Developer|Environment|Tools)\s*(?:Instructions|Context)?|'
    r'#\s*Context from my IDE setup|'
    r'<(?:environment_context|permissions|system|developer|instructions|skill|'
    r'user_instructions|system-reminder|system_message|user_info)\b)',
    re.I,
)
_XML_TAGS = ('environment_context', 'permissions', 'system', 'developer', 'instructions',
             'skill', 'user_instructions', 'system-reminder', 'system_message', 'user_info')
_XML_OPEN = re.compile(r'<(' + '|'.join(_XML_TAGS) + r')\b[^<>]{0,512}>', re.I)
_XML_CLOSE = {tag: re.compile(r'</' + re.escape(tag) + r'\s*>', re.I) for tag in _XML_TAGS}
_PATH_ONLY = re.compile(
    r'^(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/]|https?://|file://)[^\s]*[\s.!！?？,，。]*$',
    re.I,
)
_LABEL_ONLY = re.compile(r'^(?:[#@/][\w./\\-]+|[0-9a-f]{8,}(?:-[0-9a-f]+)*|\d+)[\s.!！?？,，。]*$', re.I)
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)', re.I | re.S)
_SENSITIVE_ASSIGNMENT = re.compile(
    r'''(?i)((?:密码|口令|密钥|令牌|凭证|client[_ -]?secret|private[_ -]?key|'''
    r'''access[_ -]?key(?:[_ -]?id)?)\s*[=:：]\s*["']?)[^\s,;，；"'<>]+'''
)


def _text_parts(value):
    """Extract text blocks only; never stringify tool outputs or attachments."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get('type') in ('text', 'input_text') and isinstance(item.get('text'), str):
                parts.append(item['text'])
        return parts
    return []


def _remove_wrapped(value):
    """Discard known injected blocks in linear passes, including partial tails."""
    pieces, pos = [], 0
    while opening := _XML_OPEN.search(value, pos):
        pieces.append(value[pos:opening.start()])
        pieces.append(' ')
        closing = _XML_CLOSE[opening.group(1).lower()].search(value, opening.end())
        if not closing:
            return ''.join(pieces)
        pos = closing.end()
    pieces.append(value[pos:])
    return ''.join(pieces)


def _clean_title(value, secrets=(), *, native=False):
    if not isinstance(value, str):
        return None
    # Treat injected instruction blocks as data to discard, not as user intent.
    raw = _remove_wrapped(value.strip()).strip()
    if _SYSTEM_START.match(raw):
        return None
    raw = _PRIVATE_KEY.sub('[REDACTED]', raw)
    raw = re.sub(r'(?s)```.*?```', ' ', raw)
    raw = re.sub(r'[\x00-\x1f\x7f-\x9f\u200b-\u200f\ufeff]+', ' ', raw)
    raw = re.sub(r'\s+', ' ', raw).strip()
    if _PATH_ONLY.fullmatch(raw) or _LABEL_ONLY.fullmatch(raw):
        return None
    raw = raw.strip(' \t\r\n#*_-')
    if not raw or _GENERIC.fullmatch(raw) or _PATH_ONLY.fullmatch(raw) or _LABEL_ONLY.fullmatch(raw):
        return None
    raw = _SENSITIVE_ASSIGNMENT.sub(r'\1[REDACTED]', redact(raw, secrets))
    if not raw or raw.strip() in ('[REDACTED]', 'Bearer [REDACTED]'):
        return None
    # No generated summary: this is deliberately a literal, labelled excerpt.
    return raw if len(raw) <= _MAX_TITLE else raw[:_MAX_TITLE].rstrip() + '…'


def _candidate(content, secrets):
    for part in _text_parts(content):
        candidate = _clean_title(part, secrets)
        if candidate:
            return candidate
    return None


def clean_manual_title(value, secrets=()):
    """Sanitize a deliberately entered short title without interpreting it as a prompt."""
    if not isinstance(value, str):
        raise ValueError('会话名称必须是文字')
    raw = value.strip()
    if len(raw) > _MAX_TITLE or '\x00' in raw:
        raise ValueError('会话名称不能超过 60 个字或包含空字符')
    if not raw:
        return ''
    raw = _PRIVATE_KEY.sub('[REDACTED]', raw)
    raw = re.sub(r'[\x00-\x1f\x7f-\x9f\u200b-\u200f\ufeff]+', ' ', raw)
    raw = re.sub(r'\s+', ' ', raw).strip()
    raw = _SENSITIVE_ASSIGNMENT.sub(r'\1[REDACTED]', redact(raw, secrets))
    return raw[:_MAX_TITLE]


def apply_saved_titles(store, sessions):
    """Overlay short local names; a manual name wins, a gateway name is a fallback."""
    try:
        secrets = store.secrets()
    except Exception:
        # Never expose previously saved text if current secrets cannot be checked.
        return sessions
    try:
        cutoff = retention_cutoff(store.settings())
        with store.db() as db:
            saved = {}
            for row in sessions:
                key = (row.get('agent'), row.get('sessionId'))
                manual = db.execute('SELECT title,at FROM session_manual_titles WHERE agent=? AND sid=?', key).fetchone()
                if manual and manual[1] >= cutoff:
                    saved[key] = (manual[0], 'manual', manual[1])
                    continue
                automatic = db.execute('SELECT title,source,at FROM session_title_metadata WHERE agent=? AND sid=?', key).fetchone()
                if automatic and automatic[1] == 'gateway-user-message' and automatic[2] >= cutoff:
                    saved[key] = automatic
        latest_cutoff = retention_cutoff(store.settings())
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return sessions
    for row in sessions:
        found = saved.get((row.get('agent'), row.get('sessionId')))
        if not found or found[2] < latest_cutoff:
            continue
        value, source, _ = found
        if source == 'manual' or not row.get('title'):
            try:
                title = clean_manual_title(value, secrets) if source == 'manual' else _clean_title(value, secrets)
            except (ValueError, TypeError):
                continue
            if title:
                row.update(title=title, titleSource=source)
    return sessions


def _roots(store, agent):
    home = Path(store.home)
    if agent == 'claude':
        return [Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude') / 'projects']
    if agent == 'codex':
        root = Path(os.environ.get('CODEX_HOME') or home / '.codex')
        return [root / 'sessions', root / 'archived_sessions']
    if agent == 'pi':
        return [Path(os.environ.get('PI_CODING_AGENT_DIR') or home / '.pi/agent') / 'sessions']
    if agent == 'dsh':
        return [Path(os.environ.get('DSH_HOME') or home / '.dsh') / 'sessions']
    return []


def _safe_source(store, agent, source):
    if not isinstance(source, str) or not source:
        return None
    path = Path(source)
    if not path.is_absolute() or not path.is_file():
        return None
    resolved = path.resolve()
    if agent == 'opencode':
        return resolved if resolved == opencode_database(store.home).resolve() else None
    return resolved if any(resolved.is_relative_to(root.resolve()) for root in _roots(store, agent)) else None


def _segments(path):
    """Return bounded, complete line segments; compressed streams only have a head."""
    if path.suffix.lower() in ('.zst', '.zstd'):
        try:
            import zstandard
        except ImportError:
            return []
        with path.open('rb') as raw:
            # Bound both input and output, including files with many empty frames.
            compressed = io.BytesIO(raw.read(_READ_BYTES * 2))
            with zstandard.ZstdDecompressor().stream_reader(compressed, read_across_frames=True) as stream:
                with io.BufferedReader(stream) as buffered:
                    data = buffered.read(_READ_BYTES + 1)
        return [data[:_READ_BYTES].rsplit(b'\n', 1)[0]] if b'\n' in data else []
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        head = stream.read(min(size, _READ_BYTES))
        if size <= _READ_BYTES:
            return [head if head.endswith(b'\n') else head.rsplit(b'\n', 1)[0] if b'\n' in head else b'']
        head = head.rsplit(b'\n', 1)[0] if b'\n' in head else b''
        offset = max(_READ_BYTES, size - _READ_BYTES)
        stream.seek(offset - 1)
        boundary = stream.read(1) == b'\n'
        stream.seek(offset)
        tail = stream.read(_READ_BYTES)
        # The seek lands in the middle of a record. Never parse a partial line.
        if not boundary:
            tail = tail.split(b'\n', 1)[1] if b'\n' in tail else b''
        if not tail.endswith(b'\n'):
            tail = tail.rsplit(b'\n', 1)[0] if b'\n' in tail else b''
        return [head, tail]


def _records(segment):
    lines = segment.splitlines()
    if len(lines) > _MAX_RECORDS:
        # Preserve the identity header and the newest titles in dense segments.
        half = _MAX_RECORDS // 2
        lines = lines[:half] + lines[-half:]
    for line in lines:
        if not line or len(line) > _READ_BYTES:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict):
            yield row


def _event_time(agent, row):
    if agent == 'dsh':
        return stamp(row.get('time') or row.get('createdAt'))
    return stamp(row.get('timestamp'))


def _after_cutoff(at, cutoff):
    return (at is not None and at >= cutoff) or (not cutoff and at is None)


def _native_file_title(path, agent, sid, cutoff, secrets):
    segments = _segments(path)
    if not segments:
        return None, 'unavailable', None
    head = list(_records(segments[0]))
    if not head:
        return None, 'unavailable', None
    # Identity must come from the native session, never from filename or time.
    actual = None
    header = head[0]
    if agent == 'claude':
        actual = next((r.get('sessionId') or r.get('session_id') for r in head
                       if r.get('sessionId') or r.get('session_id')), None)
        if actual and path.parent.name == 'subagents' and path.stem.startswith('agent-'):
            actual += '/' + path.stem
    elif agent == 'codex':
        meta = next((r for r in head if r.get('type') == 'session_meta'), {})
        data = meta.get('payload') or {}
        actual = data.get('id') or data.get('session_id')
    elif agent in ('pi', 'dsh'):
        meta = next((r for r in head if r.get('type') == 'session'), {})
        actual = meta.get('id')
        header = meta
    if not actual or actual != sid:
        return None, 'unavailable', None
    tail = list(_records(segments[1])) if len(segments) > 1 else []
    rows = head + tail
    # DSH seeded sessions contain inherited parent messages. Do not label the
    # child with a parent's conversation unless the explicit seed end is seen.
    seeded = agent == 'dsh' and (header.get('isSeeded') or header.get('version', 0) < 2 and header.get('seedLength', 0))
    if seeded and header.get('version', 0) >= 2:
        cuts = [i for i, r in enumerate(rows) if r.get('type') == 'session/end-seed'
                and isinstance(r.get('data'), dict) and r['data'].get('inherited')]
        if not cuts:
            return None, 'unavailable', None
        rows = rows[max(cuts) + 1:]
    elif seeded:
        rows = [r for r in rows if isinstance(r.get('seq'), int)
                and r['seq'] >= header.get('seedLength', 0)]
    native, excerpt, native_time, excerpt_time = None, None, None, None
    pi_fork = agent == 'pi' and bool(header.get('parentSession'))
    pi_started = stamp(header.get('timestamp')) if pi_fork else None
    for row in rows:
        at = _event_time(agent, row)
        if not _after_cutoff(at, cutoff):
            continue
        if pi_fork and row is not header and (pi_started is None or at is None or at < pi_started):
            continue  # A fork can include parent entries; unclear ownership is not enough.
        typ = row.get('type')
        if agent == 'claude':
            if typ == 'ai-title':
                candidate = _clean_title(row.get('aiTitle'), secrets, native=True)
                if candidate: native, native_time = candidate, at
            message = row.get('message') if isinstance(row.get('message'), dict) else {}
            if excerpt is None and typ == 'user' and not row.get('isMeta') and not row.get('isSidechain'):
                excerpt = _candidate(message.get('content'), secrets)
                if excerpt: excerpt_time = at
        elif agent == 'codex':
            data = row.get('payload') if isinstance(row.get('payload'), dict) else {}
            if excerpt is None and typ == 'response_item' and data.get('role') == 'user':
                excerpt = _candidate(data.get('content'), secrets)
                if excerpt: excerpt_time = at
        elif agent == 'pi':
            if typ == 'session':
                candidate = _clean_title(row.get('name'), secrets, native=True)
                if candidate: native, native_time = candidate, at
            elif typ == 'session_info':
                # pi's most recent session_info is authoritative, including an
                # explicit clear. A filtered/empty latest name must not revive
                # an older native name; the user-message fallback remains.
                native = _clean_title(row.get('name'), secrets, native=True)
                native_time = at if native else None
            message = row.get('message') if isinstance(row.get('message'), dict) else {}
            if excerpt is None and typ == 'message' and message.get('role') == 'user':
                excerpt = _candidate(message.get('content'), secrets)
                if excerpt: excerpt_time = at
        elif agent == 'dsh':
            data = row.get('data') if isinstance(row.get('data'), dict) else {}
            if typ == 'session/title':
                candidate = _clean_title(data.get('title'), secrets, native=True)
                if candidate: native, native_time = candidate, at
            if excerpt is None and typ == 'user/message':
                message = data.get('message') if isinstance(data.get('message'), dict) else {}
                excerpt = _candidate(data.get('content', message.get('content')), secrets)
                if excerpt: excerpt_time = at
    return ((native, 'native-title', native_time) if native else
            (excerpt, 'user-message', excerpt_time) if excerpt else (None, 'unavailable', None))


def _opencode_title(path, sid, cutoff, secrets):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.05)
    try:
        deadline = time.monotonic() + _SQL_BUDGET_SECONDS
        # A missing database index must not make a single session scan an
        # unbounded amount of history. SQLite can abort between VM steps.
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT title,time_created FROM session_v2 WHERE id=?', (sid,)).fetchone()
        if not row:
            return None, 'unavailable', None
        created = stamp(row[1])
        # OpenCode exposes no title-change time; only use a native title when
        # its session predates no requested clearing/retention boundary.
        title = _clean_title(row[0], secrets, native=True) if _after_cutoff(created, cutoff) else None
        if title:
            return title, 'native-title', created
        lower = cutoff * 1000 if cutoff else 0
        for raw, at in db.execute("SELECT data,time_created FROM session_message WHERE session_id=? AND type='user' "
                                  "AND time_created>=? AND length(CAST(data AS BLOB))<=? "
                                  "ORDER BY time_created,seq LIMIT 32", (sid, lower, _READ_BYTES)):
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                found = _candidate(data.get('text', data.get('content')), secrets)
                if found:
                    return found, 'user-message', stamp(at)
        return None, 'unavailable', None
    finally:
        db.close()


def recent_titles(store, sessions):
    """Decorate no more than 30 indexed sessions; never modify the index."""
    settings = store.settings()
    cutoff = retention_cutoff(settings)
    try:
        secrets = store.secrets()
    except Exception:
        # If configured secrets cannot be obtained, fail closed: don't expose text.
        return [{**row, 'title': None, 'titleSource': 'unavailable'} for row in sessions[:30]]
    out = []
    deadline = time.monotonic() + _SCAN_BUDGET_SECONDS
    for item in sessions[:30]:
        row = dict(item)
        agent, sid = row.get('agent'), row.get('sessionId')
        try:
            path = _safe_source(store, agent, row.get('source'))
            if path is None:
                raise ValueError('source unavailable')
            stats = path.stat()
            wal = path.with_name(path.name + '-wal') if agent == 'opencode' else None
            wal_stats = wal.stat() if wal is not None and wal.is_file() else None
            wal_token = (wal_stats.st_mtime_ns, wal_stats.st_size) if wal_stats else None
            token = (agent, sid, str(path), stats.st_mtime_ns, stats.st_size, wal_token,
                     settings.get('retentionDays', 0), settings.get('clearedBefore', 0))
            with _LOCK:
                cache = _CACHE.setdefault(store, OrderedDict())
                hit = cache.get(token)
                if hit and hit[2] is not None and hit[2] < cutoff:
                    hit = None
                if hit is not None:
                    cache.move_to_end(token)
            if hit is None:
                # Apply the budget between sources. Cached titles remain usable;
                # later polls can fill entries that did not fit in this pass.
                if time.monotonic() >= deadline:
                    row.update(title=None, titleSource='unavailable')
                    out.append(row)
                    continue
                if agent == 'opencode':
                    result = _opencode_title(path, sid, cutoff, secrets)
                else:
                    result = _native_file_title(path, agent, sid, cutoff, secrets)
                after = path.stat()
                after_wal = wal.stat() if wal is not None and wal.is_file() else None
                after_wal_token = (after_wal.st_mtime_ns, after_wal.st_size) if after_wal else None
                if ((after.st_mtime_ns, after.st_size) != (stats.st_mtime_ns, stats.st_size)
                        or after_wal_token != wal_token):
                    result = (None, 'unavailable', None)
                with _LOCK:
                    cache[token] = result
                    while len(cache) > _CACHE_LIMIT:
                        cache.popitem(last=False)
            else:
                result = hit
            title, source, title_at = result
            # Provider secrets may change while the source fingerprint does not.
            title = _clean_title(title, secrets, native=source == 'native-title') if title else None
            row.update(title=title, titleSource=source if title else 'unavailable')
            row['_titleAt'] = title_at
        except Exception:
            # Native files/DBs can be concurrently replaced or malformed. A
            # bad source should not break the list or expose its error text.
            row.update(title=None, titleSource='unavailable')
        out.append(row)
    try:
        # Clearing can happen while source files are being read. Recheck the
        # current watermark before returning; internal timestamps are removed.
        latest_cutoff = retention_cutoff(store.settings())
    except Exception:
        latest_cutoff = float('inf')
    for row in out:
        title_at = row.pop('_titleAt', None)
        if row.get('title') and not _after_cutoff(title_at, latest_cutoff):
            row.update(title=None, titleSource='unavailable')
    return out
