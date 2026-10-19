"""Read-only native telemetry. Configuration is never evidence of a call.

Normalized records share one filter/query path for all four observation pages.
Only explicit native ids establish ownership. Managed Magpie records supplement
native logs; exact response IDs merge the two views without time-based guessing.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_inventory import AGENTS

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


def open_lines(path):
    if path.suffix in ('.zstd', '.zst'):
        try:
            import zstandard
        except ImportError as exc:
            raise ValueError('压缩日志需要 zstandard；运行 python -m pip install -r requirements.txt') from exc
        raw = path.open('rb')
        try:
            with zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) as reader:
                yield from _lines(io.BufferedReader(reader))
        finally:
            raw.close()
    else:
        with path.open('rb') as reader:
            yield from _lines(reader)


def _lines(reader):
    total = 0
    while True:
        line = reader.readline(MAX_LINE + 1)
        if not line:
            return
        total += len(line)
        if len(line) > MAX_LINE or total > MAX_DECODED:
            raise ValueError(f'日志超过单行 {MAX_LINE//1024//1024} MiB / 单文件解压 {MAX_DECODED//1024//1024} MiB 安全上限，未完成索引')
        # A partially written tail is never treated as a completed event.
        if not line.endswith(b'\n'):
            return
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict):
            yield row


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
        if agent == 'dsh' and root.exists():
            latest = {}
            for f in root.rglob('session*.jsonl*'):
                m = re.fullmatch(r'session(?:\.v(\d+))?\.jsonl(?:\.zstd)?', f.name)
                if not m or not f.is_file():
                    continue
                rank = (int(m[1] or 0), f.stat().st_mtime_ns)
                if f.parent not in latest or rank > latest[f.parent][0]:
                    latest[f.parent] = (rank, f)
            files = [x[1] for x in latest.values()]
        files = [f for f in files if f.is_file()]
        sources.extend((agent, f) for f in files)
        info.append({'agent': agent, 'root': str(root), 'exists': root.exists(), 'files': len(files),
                     'coverage': 'native-partial', 'note': '只读原生记录；未记录的网络失败、重试、TTFT 无法补推。'})
    return sorted(sources, key=lambda x: x[1].stat().st_mtime, reverse=True), info


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


def parse_native(agent, path, content=False, secrets=()):
    p = Parsed(agent, path, content, secrets)
    rows = list(open_lines(path))
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
        self.stop_event = threading.Event()
        self.monitor = None
        self.progress = {'state': 'idle', 'processed': 0, 'total': 0, 'errors': [], 'sources': []}
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, agent TEXT, sid TEXT, at REAL, data TEXT);
                CREATE INDEX IF NOT EXISTS request_time ON requests(at);
                CREATE INDEX IF NOT EXISTS request_session ON requests(agent,sid);
                CREATE INDEX IF NOT EXISTS request_source ON requests(json_extract(data,'$.source'));
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, agent TEXT, sid TEXT, at REAL, data TEXT);
                CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, agent TEXT, sid TEXT, at REAL, data TEXT);
                CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, fingerprint TEXT);
                CREATE TABLE IF NOT EXISTS quotas(id TEXT PRIMARY KEY, at REAL, data TEXT);
            ''')

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
            if not value['content']:
                with self.db() as db:
                    db.execute('DELETE FROM messages')
                    for sid, data in db.execute('SELECT id,data FROM sessions').fetchall():
                        data = json.loads(data)
                        data['title'] = None
                        db.execute('UPDATE sessions SET data=? WHERE id=?', (json.dumps(data), sid))
            self.cleanup()
        if value['enabled']:
            self.refresh()
        return self.status()

    def status(self):
        with self.db() as db:
            count = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            ncontent = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            content_bytes = db.execute('SELECT COALESCE(SUM(length(CAST(data AS BLOB))),0) FROM messages').fetchone()[0]
        return {'settings': self.settings(), 'collector': dict(self.progress), 'records': count,
                'contentRecords': ncontent, 'contentBytes': content_bytes,
                'contentRetentionDays': CONTENT_DAYS, 'contentMaxBytes': CONTENT_MAX_BYTES, 'path': str(self.path),
                'coverage': '原生日志的局部视图；没有记录不等于零请求。真实客户端验证范围与版本见 docs/validation.md；不代表完整网络流水。'}

    def refresh(self):
        with self.lock:
            if not self.settings()['enabled']:
                return self.status()
            if not self.worker or not self.worker.is_alive():
                self.worker = threading.Thread(target=self.collect, daemon=True)
                self.worker.start()
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
        if self.monitor:
            self.monitor.join(timeout=2)
        if self.worker:
            self.worker.join(timeout=2)

    def collect(self):
        self.progress = {'state': 'reading', 'processed': 0, 'total': 0, 'errors': [], 'sources': [], 'startedAt': time.time()}
        try:
            sources, info = native_files(self.home)
            self.progress.update(total=len(sources), sources=info)
            secrets = self.secrets()
            for agent, path in sources:
                with self.lock:
                    settings = self.settings()
                    if not settings['enabled'] or self.stop_event.is_set():
                        self.progress['state'] = 'stopped'
                        return
                    st = path.stat()
                    fingerprint = f'{PARSER_REVISION}:{st.st_size}:{st.st_mtime_ns}:{settings["content"]}:{settings["retentionDays"]}'
                    with self.db() as db:
                        old = db.execute('SELECT fingerprint FROM sources WHERE path=?', (str(path),)).fetchone()
                    if old and old[0] == fingerprint:
                        self.progress['processed'] += 1
                        continue
                try:
                    parsed = parse_native(agent, path, settings['content'], secrets)
                    with self.lock:
                        current = self.settings()
                        if not current['enabled'] or current != settings or self.stop_event.is_set():
                            continue
                        self.ingest(parsed, settings)
                        with self.db() as db:
                            db.execute('INSERT OR REPLACE INTO sources VALUES(?,?)', (str(path), fingerprint))
                except Exception as exc:
                    message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    self.progress['errors'].append({'agent': agent, 'path': str(path), 'message': message})
                    self.progress['errors'] = self.progress['errors'][-100:]
                self.progress['processed'] += 1
            self.cleanup()
            # Keep legacy records readable without running the former engine.
            for gateway_path in (self.root/'gateway/config/magpie/usage.jsonl',self.root/'gateway/config/agent-hub/usage.jsonl'):
              if gateway_path.exists() and self.settings()['enabled']:
                try:
                    self.ingest_gateway(gateway_path)
                    self.progress['sources'].append({'agent':'gateway','path':str(gateway_path),'state':'available'})
                except (OSError,ValueError) as exc:
                    self.progress['errors'].append({'agent':'gateway','message':redact(str(exc),secrets)})
            self.progress['state'] = 'complete'
        except Exception as exc:
            self.progress['state'] = 'failed'
            self.progress['errors'].append({'message': type(exc).__name__})
        finally:
            self.progress['finishedAt'] = time.time()

    def ingest(self, parsed, settings=None):
        settings = settings or self.settings()
        price_config=self.config()
        cutoff = retention_cutoff(settings)
        content_cutoff = max(cutoff, time.time() - CONTENT_DAYS * 86400)
        with self.db() as db:
            # Parser migrations may replace a synthetic native key with an exact
            # response ID. Remove only superseded rows from this same source.
            for old_id, data in db.execute("SELECT id,data FROM requests WHERE json_extract(data,'$.source')=?",(str(parsed.path),)).fetchall():
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
        from gateway_observation import read_requests
        if not self.settings()['enabled']:
            return
        cutoff=retention_cutoff(self.settings())
        price_config=self.config()
        with self.lock, self.db() as db:
            identities={}
            for agent,data in db.execute('SELECT agent,data FROM requests'):
                native=json.loads(data)
                identities.setdefault(native['nativeId'],set()).add(agent)
            for row in read_requests(path,mapping,identities,self.secrets()):
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
            # DELETE is also applied to WAL; secure_delete scrubs old cell payloads.
            with self.db() as db:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if clear:
                with self.db() as db:
                    db.execute('VACUUM')
        return {'ok': True, 'cleared': clear}

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
            allowed = ([roots[agent]/'sessions', roots[agent]/'archived_sessions'] if agent == 'codex' else [roots[agent]])
            if any(path.resolve().is_relative_to(root.resolve()) for root in allowed):
                try:
                    parsed = parse_native(agent,path,True,self.secrets())
                    if parsed.sid != sid:
                        raise ValueError('原生会话身份已变化，未展示该文件内容')
                    lower = max(data['range']['start'],retention_cutoff(self.settings()))
                    messages = [m for m in parsed.messages if lower <= m['time'] <= data['range']['end']]
                    source = 'native-on-demand'
                    session['title'] = parsed.title or session.get('title')
                except (OSError,ValueError) as exc:
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
