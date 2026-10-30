"""Local session resumption and explicit, reviewable cross-agent handoff.

The browser may supply only indexed session and inventory identifiers. Native
paths, cwd and launch commands are taken from the store and inventory, then
revalidated. Nothing here silently sends conversation content to a model.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from agent_hub.observability.store import parse_native, parse_opencode_database, opencode_database

_SID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$')
_TARGETS = frozenset(('claude', 'codex', 'pi', 'dsh', 'opencode'))
_MAX_TEXT = 256 * 1024


def _check_id(agent, sid):
    if agent not in _TARGETS or not isinstance(sid, str) or not sid or len(sid) > 256:
        raise ValueError('Agent 或会话标识无效')


def _gateway_only_source(store, source):
    """An indexed gateway ledger has not been associated with a native file."""
    if not isinstance(source, str) or not source:
        return False
    try:
        path = Path(source)
        if not path.is_absolute():
            return False
        root = Path(store.root)
        ledgers = (root / 'gateway/config/magpie/usage.jsonl',
                   root / 'gateway/config/agent-hub/usage.jsonl')
        return path.resolve() in {ledger.resolve() for ledger in ledgers}
    except (OSError, ValueError, RuntimeError):
        return False


def recent_sessions(store, limit=30):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 30:
        raise ValueError('最近会话数量必须在 1 至 30 之间')
    # Only session metadata is required here; Store.query(days=all) loads every
    # historical request and computes all analytics on each homepage refresh.
    store.cleanup()
    with store.db() as db:
        total = db.execute("SELECT COUNT(*) FROM (SELECT agent,sid FROM requests "
                           "WHERE sid IS NOT NULL AND sid<>'' GROUP BY agent,sid)").fetchone()[0]
        indexed = db.execute("SELECT agent,sid,MAX(at) AS last_at FROM requests "
                             "WHERE sid IS NOT NULL AND sid<>'' GROUP BY agent,sid "
                             "ORDER BY last_at DESC LIMIT ?", (limit,)).fetchall()
        rows = []
        for agent, sid, last_at in indexed:
            found = db.execute("SELECT data FROM sessions WHERE agent=? AND sid=? LIMIT 1", (agent, sid)).fetchone()
            item = json.loads(found[0]) if found else {}
            row = {k: item.get(k) for k in ('cwd', 'title', 'parentSessionId', 'source')}
            row.update(agent=agent, sessionId=sid, lastAt=last_at)
            if _gateway_only_source(store, row.get('source')):
                row['sourceKind'] = 'gateway-only'
            rows.append(row)
    from agent_hub.sessions.titles import apply_saved_titles, recent_titles
    rows = apply_saved_titles(store, recent_titles(store, rows))
    for row in rows:
        row.pop('source', None)
    return {'sessions': rows, 'total': total,
            'coverage': '来自 Hub 已索引且有调用记录的会话；不存在记录不代表没有会话。'}


def set_session_title(store, agent, sid, title):
    """Save a user's label for an indexed session, or remove its manual label."""
    _check_id(agent, sid)
    from agent_hub.observability.store import retention_cutoff
    from agent_hub.sessions.titles import clean_manual_title
    # Fail closed if configured secrets cannot be loaded for redaction.
    title = clean_manual_title(title, store.secrets())
    with store.lock, store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        cutoff = retention_cutoff(store.settings())
        present = db.execute('SELECT 1 FROM requests WHERE agent=? AND sid=? AND at>=? LIMIT 1',
                             (agent, sid, cutoff)).fetchone()
        if not present:
            raise ValueError('找不到已索引的会话，无法命名')
        if title:
            db.execute('''INSERT INTO session_manual_titles(agent,sid,title,at)
                          VALUES(?,?,?,?) ON CONFLICT(agent,sid) DO UPDATE SET
                          title=excluded.title, at=excluded.at''',
                       (agent, sid, title, time.time()))
        else:
            db.execute('DELETE FROM session_manual_titles WHERE agent=? AND sid=?',
                       (agent, sid))
    return {'ok': True}


def _detail(store, agent, sid):
    _check_id(agent, sid)
    result = store.session_detail(agent, sid, {'days': 'all'})
    session = result.get('session')
    if not session or session.get('agent') != agent or session.get('sessionId') != sid:
        raise ValueError('找不到已索引的会话；可能已清理或原生日志尚未采集')
    return result


def _verified_source(store, result, agent, sid):
    """Recheck the source and cwd at action time, never trust caller paths."""
    session = result['session']
    raw_source = session.get('source')
    if not isinstance(raw_source, str) or not raw_source:
        raise ValueError('没有可验证的原生会话文件，无法自动恢复')
    source = Path(raw_source)
    if not source.is_absolute() or not source.is_file():
        raise ValueError('原生会话文件不存在或路径无效')
    home = Path(store.home)
    roots = {
        'claude': [Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude') / 'projects'],
        'codex': [Path(os.environ.get('CODEX_HOME') or home / '.codex') / 'sessions',
                  Path(os.environ.get('CODEX_HOME') or home / '.codex') / 'archived_sessions'],
        'pi': [Path(os.environ.get('PI_CODING_AGENT_DIR') or home / '.pi/agent') / 'sessions'],
        'dsh': [Path(os.environ.get('DSH_HOME') or home / '.dsh') / 'sessions'],
    }
    resolved = source.resolve()
    if agent == 'opencode':
        safe = resolved == opencode_database(home).resolve()
    else:
        safe = any(resolved.is_relative_to(root.resolve()) for root in roots[agent])
    if not safe:
        raise ValueError('原生会话路径不在客户端允许的目录内')
    try:
        if agent == 'opencode':
            found = parse_opencode_database(source, False, session_id=sid)
            parsed = found[0] if len(found) == 1 else None
        else:
            parsed = parse_native(agent, source, False)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise ValueError('原生会话无法读取或格式不受支持') from exc
    if not parsed or parsed.sid != sid:
        raise ValueError('原生会话身份与索引不一致')
    indexed_cwd, native_cwd = session.get('cwd'), parsed.cwd
    if not isinstance(indexed_cwd, str) or not isinstance(native_cwd, str):
        raise ValueError('会话缺少可靠的项目目录')
    a, b = Path(indexed_cwd), Path(native_cwd)
    if not a.is_absolute() or not b.is_absolute() or a.resolve() != b.resolve():
        raise ValueError('原生会话项目目录与索引不一致')
    if not b.is_dir():
        raise ValueError('原生会话的项目目录不存在，请先定位或修复目录')
    return {'cwd': str(b.resolve()), 'source': str(resolved)}


def _instance(inventory, agent, instance_id=None):
    if inventory is None:
        return None, '尚未检测客户端安装实例'
    if getattr(inventory, 'checked_at', None) is None:
        inventory.detect()
    with inventory.lock:
        candidates = [dict(item) for item in inventory.instances.values()
                      if item.get('agent') == agent and item.get('kind') == 'cli']
    if instance_id:
        candidates = [item for item in candidates if item.get('id') == instance_id]
        if not candidates:
            return None, '选定的 CLI 安装实例不存在或已变化，请重新检测'
    if not candidates:
        return None, '未发现此 Agent 的 CLI 安装实例'
    candidates.sort(key=lambda item: not item.get('activePath'))
    entry = candidates[0]
    launch = entry.get('launch')
    if not isinstance(launch, list) or not launch or not all(isinstance(x, str) and x for x in launch):
        return None, '客户端启动命令不可用'
    executable = Path(launch[0])
    # A cmd wrapper is a shell even with shell=False on Windows. Do not route
    # native paths and user text through one; inventory may offer node + script.
    if (not executable.is_absolute() or not executable.is_file() or
        executable.suffix.lower() in ('.cmd', '.bat', '.ps1') or
        executable.name.lower() in ('cmd.exe', 'powershell.exe', 'pwsh.exe', 'sh.exe', 'bash.exe')):
        return None, '此实例只能通过未验证的命令解释器启动，请选择可直接启动的实例'
    return entry, None


def _public_instance(item):
    return {k: item.get(k) for k in ('id', 'path', 'version')} if item else None


def _opencode_v2(entry):
    return bool(entry and re.match(r'^2\.[0-9]+\.[0-9]+(?:[-+].*)?$', str(entry.get('version') or '')))


def _start_strategy(entry, target):
    """Only versions whose interactive initial-message entry is documented."""
    version = str((entry or {}).get('version') or '')
    if target == 'pi' and version == '1.4.0':
        return 'pi-file'
    if target == 'claude' and version == '2.1.312':
        return 'path-prompt'
    if target == 'codex' and version == '0.165.0':
        return 'path-prompt'
    if target == 'opencode' and version == '2.2.1':
        return 'path-prompt'
    return None


def _path_prompt(path):
    """Only the file path, never the handoff contents, enters process argv."""
    return ('这是一份用户已经确认的跨 Agent 交接。请先读取以下本机 UTF-8 文件，'
            '读取成功后根据文件中的目标和下一步继续工作。遵守当前客户端的权限设置。'
            '如果文件无法读取，请说明原因并停止，不要猜测或编造交接内容。\n'
            'HANDOFF_FILE_JSON=' + json.dumps(str(path), ensure_ascii=False))


def _dsh_tui_ready(inventory):
    """Recognize documented optional TUI packages, without installing one."""
    home = getattr(inventory, 'home', None)
    if not home:
        return False
    profile = Path(os.environ.get('DSH_HOME') or Path(home) / '.dsh') / 'profiles' / 'tui'
    try:
        data = json.loads((profile / 'package.json').read_text(encoding='utf-8'))
        dependencies = data.get('dependencies') or {}
        names = ('@dsh-tui/dsh-tui', '@tomowang/dsh-tui')
        return any(name in dependencies and (profile / 'node_modules' / name / 'package.json').is_file()
                   for name in names)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _resume_plan(store, agent, sid, inventory, instance_id=None):
    result = _detail(store, agent, sid)
    meta = _verified_source(store, result, agent, sid)
    data = {'agent': agent, 'sessionId': sid, 'cwd': meta['cwd'], 'available': False,
            'reason': None, 'instance': None, 'command': None}
    if not _SID.fullmatch(sid):
        data['reason'] = '此标识可能属于子会话或包含不支持的字符，不能直接恢复'
        return data, None
    if agent == 'dsh' and not _dsh_tui_ready(inventory):
        data['reason'] = '未发现已安装的受支持 dsh tui profile，不能自动恢复'
        return data, None
    if os.name != 'nt':
        data['reason'] = '当前版本仅支持在 Windows 打开可交互的恢复终端'
        return data, None
    entry, error = _instance(inventory, agent, instance_id)
    if error:
        data['reason'] = error
        return data, None
    if agent == 'opencode' and not _opencode_v2(entry):
        data['reason'] = 'OpenCode CLI 未检出可运行的 v2 版本；不能假设恢复参数兼容'
        return data, None
    args = {'claude': ['--resume', sid], 'codex': ['resume', sid],
            'pi': ['--session', meta['source']],
            'dsh': ['--profile', 'tui', '--resume', sid],
            'opencode': ['mini', '--session', sid]}[agent]
    command = [*entry['launch'], *args]
    data.update(available=True, instance=_public_instance(entry), command=command,
                reason='将打开新终端；启动成功不代表客户端已成功恢复。')
    return data, command


def preview_resume(store, agent, sid, inventory, instance_id=None):
    return _resume_plan(store, agent, sid, inventory, instance_id)[0]


def resume_session(store, agent, sid, inventory, instance_id=None):
    # Re-read and revalidate at execution time rather than trusting a preview.
    data, command = _resume_plan(store, agent, sid, inventory, instance_id)
    if not command:
        raise ValueError(data['reason'])
    try:
        subprocess.Popen(command, cwd=data['cwd'], shell=False,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError as exc:
        raise ValueError('无法打开恢复终端，请检查客户端安装和项目目录') from exc
    return {'ok': True, 'launched': True, 'agent': agent, 'sessionId': sid,
            'instance': data['instance'], 'cwd': data['cwd'],
            'message': '已启动交互终端；请在客户端确认会话是否恢复。'}


def _excerpt_messages(messages):
    usable = [m for m in messages if isinstance(m, dict) and isinstance(m.get('body'), str) and m['body'].strip()]
    # Keep the first user instruction and recent recorded events. They are raw
    # excerpts, not asserted facts or a model-generated summary.
    first = next((i for i, m in enumerate(usable) if 'user' in str(m.get('role', '')).lower()), None)
    indexes = list(range(max(0, len(usable) - 8), len(usable)))
    if first is not None and first not in indexes:
        indexes.insert(0, first)
    selected, total_chars, truncated = [], 0, len(indexes) < len(usable)
    for index in indexes:
        m = usable[index]
        body = m['body']
        cap = min(1600, max(0, 12000 - total_chars))
        if not cap:
            truncated = True
            break
        if len(body) > cap:
            body = body[:cap] + '\n[该条内容已截断]'
            truncated = True
        selected.append((str(m.get('role') or 'unknown'), body))
        total_chars += len(body)
    return usable, selected, truncated


def handoff_target_capabilities(inventory, target, instance_id=None):
    if target not in _TARGETS:
        raise ValueError('不支持的目标 Agent')
    entry, error = _instance(inventory, target, instance_id)
    compatible = (target in ('claude', 'codex', 'pi') or
                  target == 'opencode' and _opencode_v2(entry) or
                  target == 'dsh' and _dsh_tui_ready(inventory))
    opened = bool(os.name == 'nt' and entry and compatible)
    strategy = _start_strategy(entry, target) if opened else None
    supported = bool(strategy)
    detail = ('pi 通过临时文件传入初始交接；文件在进程退出后清理。' if strategy == 'pi-file' else
              '初始指令会要求目标 Agent 读取临时交接文件；读取是否成功取决于客户端权限和实际行为。'
              if strategy == 'path-prompt' else
              '当前客户端版本的交互式安全传递尚未验证；请复制交接内容后在目标客户端发送。')
    reason = error or detail
    open_reason = (error or ('可在项目目录打开交互终端；需要手动粘贴交接内容。' if opened else
                            '请先确认 dsh tui profile 或可运行的 OpenCode v2 CLI；仅支持 Windows 交互终端。'))
    return {'targetAgent': target, 'instance': _public_instance(entry),
            'openSupported': opened, 'openReason': open_reason,
            'startSupported': supported, 'reason': reason}


def launch_target(store, agent, sid, target, inventory, instance_id=None):
    """Open a new interactive target, but never send the handoff text."""
    detail = _detail(store, agent, sid)
    meta = _verified_source(store, detail, agent, sid)
    cap = handoff_target_capabilities(inventory, target, instance_id)
    if not cap['openSupported']:
        raise ValueError(cap['openReason'])
    entry, error = _instance(inventory, target, instance_id)
    if error or not entry:
        raise ValueError(error or '目标客户端实例已变化，请重新预览')
    try:
        args = (['mini'] if target == 'opencode' else ['--profile', 'tui'] if target == 'dsh' else [])
        subprocess.Popen([*entry['launch'], *args], cwd=meta['cwd'], shell=False,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError as exc:
        raise ValueError('无法打开目标客户端，请检查客户端安装和项目目录') from exc
    return {'ok': True, 'launched': True, 'targetAgent': target, 'cwd': meta['cwd'],
            'instance': cap['instance'],
            'message': '已打开目标客户端，尚未发送交接内容；请复制并粘贴。'}


def draft_handoff(store, agent, sid, target):
    if target not in _TARGETS:
        raise ValueError('不支持的目标 Agent')
    result = _detail(store, agent, sid)
    session = result['session']
    usable, selected, truncated = _excerpt_messages(result.get('messages') or [])
    lines = ['# 跨 Agent 交接草稿（请核对后使用）', '',
             f'来源：{agent}；目标：{target}',
             f'源会话：{sid}', f'项目目录：{session.get("cwd") or "未记录"}', '',
             '本草稿是原生日志摘录与待填写项，不是智能摘要，也不证明摘录中的工作已完成。',
             '原始日志可能缺少记录；工具状态、权限和隐藏上下文不会随交接自动转移。', '',
             '## 任务目标与用户约束\n[请核对并补充]', '',
             '## 已完成内容、关键文件和验证结果\n[请核对并补充]', '',
             '## 未完成问题与下一步\n[请核对并补充]', '',
             '## 来源事件摘录（保留原文，未经归纳）']
    for role, body in selected:
        lines.extend(['', f'### {role}', body])
    if not selected:
        lines.extend(['', '没有可读取的正文事件；请手动填写交接内容。'])
    if truncated:
        lines.extend(['', '注意：部分记录或单条内容已省略，不能据此认为所有历史均已交接。'])
    notice = ('已按需读取客户端原生记录，仅生成可编辑草稿，尚未发送。' if result.get('contentSource') == 'native-on-demand'
              else '原生记录不可用或正文并非原生日志；草稿可能不完整，请检查来源和读取错误。')
    return {'sourceAgent': agent, 'sourceSessionId': sid, 'targetAgent': target,
            'cwd': session.get('cwd'), 'text': '\n'.join(lines),
            'source': result.get('contentSource') or 'unknown', 'generatedBy': 'structured-excerpts',
            'eventCount': len(usable), 'includedCount': len(selected), 'truncated': truncated,
            'contentError': result.get('contentError'), 'notice': notice}


def _cleanup_after_exit(process, path, directory):
    try:
        process.wait()
    finally:
        try:
            path.unlink(missing_ok=True)
            directory.rmdir()  # Only remove our directory if empty.
        except OSError:
            pass


def start_handoff(store, agent, sid, target, text, inventory, instance_id=None):
    """Explicitly start a supported interactive client with a reviewed handoff.

    A temporary user-data file is passed by path; its contents never enter the
    command line. It is removed after pi exits while Hub is running. A crash or
    Hub exit before pi exits may leave it behind, as reported to the user.
    """
    if not isinstance(text, str) or not text.strip() or '\x00' in text or len(text.encode('utf-8')) > _MAX_TEXT:
        raise ValueError('交接内容为空、包含无效字符或超过 256 KiB')
    detail = _detail(store, agent, sid)
    meta = _verified_source(store, detail, agent, sid)
    cap = handoff_target_capabilities(inventory, target, instance_id)
    if not cap['startSupported']:
        raise ValueError(cap['reason'])
    entry, error = _instance(inventory, target, instance_id)
    strategy = _start_strategy(entry, target)
    if error or not entry or not strategy:
        raise ValueError(error or '目标客户端版本已变化，请重新预览')
    root = Path(store.root).resolve()
    directory = root / 'handoff-tmp'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.resolve().parent != root:
        raise ValueError('交接临时目录无效')
    fd, filename = tempfile.mkstemp(prefix='handoff-', suffix='.txt', dir=directory)
    path = Path(filename)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write(text)
        if strategy == 'pi-file':
            command = [*entry['launch'], '@' + str(path)]
        elif target == 'opencode':
            command = [*entry['launch'], 'mini', '--prompt', _path_prompt(path)]
        else:
            command = [*entry['launch'], _path_prompt(path)]
        process = subprocess.Popen(command, cwd=meta['cwd'], shell=False,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    threading.Thread(target=_cleanup_after_exit, args=(process, path, directory), daemon=True).start()
    msg = ('已启动 pi 并传入交接文件；请在终端确认执行状态。' if strategy == 'pi-file' else
           '已启动目标客户端并请求读取交接文件；请在终端确认读取与执行状态。')
    return {'ok': True, 'launched': True, 'targetAgent': target, 'cwd': meta['cwd'],
            'instance': cap['instance'],
            'message': msg + '临时文件在目标进程退出后清理；Hub 提前退出或崩溃时可能残留。'}
