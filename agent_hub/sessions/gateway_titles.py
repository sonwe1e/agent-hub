"""Bounded user-message excerpts for explicitly identified gateway sessions.

The gateway and observer are separate processes.  The observer owns the schema,
collection control and clearing; this module opens only an explicitly supplied
existing database and never creates an observational store of its own.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

MAX_SOURCE_TEXT = 16 * 1024
MAX_INPUT_ITEMS = 128
MAX_GATEWAY_TITLES = 10000


def _connect(root):
    if root is None:
        return None
    database = Path(root) / 'observability.sqlite3'
    if not database.is_file():
        return None
    return sqlite3.connect(database.resolve().as_uri() + '?mode=rw', uri=True, timeout=.5)


def _settings_allow(root):
    # Missing means the ordinary observation defaults; corrupt settings fail closed.
    settings = Path(root) / 'observation-settings.json'
    try:
        data = json.loads(settings.read_text(encoding='utf-8'))
        return isinstance(data, dict) and data.get('enabled', True) is True
    except FileNotFoundError:
        return True
    except (ValueError, OSError):
        return False


def begin(root):
    """Snapshot the collection epoch for one request, or decline capture."""
    if root is None or not _settings_allow(root):
        return None
    try:
        db = _connect(root)
        if db is None:
            return None
        try:
            row = db.execute('SELECT enabled,epoch FROM session_title_control WHERE id=1').fetchone()
            return row[1] if row and row[0] == 1 else None
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return None


def _user_parts(body):
    """Yield only text from user-role input; never stringify tool output."""
    if not isinstance(body, dict):
        return
    items = body.get('messages') if isinstance(body.get('messages'), list) else body.get('input')
    if isinstance(items, str):
        if len(items) <= MAX_SOURCE_TEXT:
            yield items
        return
    if not isinstance(items, list):
        return
    for item in items[:MAX_INPUT_ITEMS]:
        if not isinstance(item, dict) or item.get('role') != 'user':
            continue
        content = item.get('content')
        if isinstance(content, str):
            if len(content) <= MAX_SOURCE_TEXT:
                yield content
        elif isinstance(content, list):
            for part in content[:MAX_INPUT_ITEMS]:
                if (isinstance(part, dict) and part.get('type') in ('text', 'input_text')
                        and isinstance(part.get('text'), str) and len(part['text']) <= MAX_SOURCE_TEXT):
                    yield part['text']


def excerpt(body, secrets=()):
    from agent_hub.sessions.titles import _clean_title
    for text in _user_parts(body):
        candidate = _clean_title(text, secrets)
        if candidate:
            return candidate
    return None


def save(root, agent, sid, body, started_at, epoch, secrets=()):
    """Persist one short excerpt if collection and the start epoch still match."""
    if isinstance(agent, str):
        agent = {'claude-desktop': 'claude', 'codex-desktop': 'codex'}.get(agent, agent)
    if (root is None or epoch is None or not isinstance(agent, str) or not agent
            or not isinstance(sid, str) or not sid or len(sid) > 512):
        return False
    title = excerpt(body, secrets)
    if not title:
        return False
    try:
        from agent_hub.observability.store import retention_cutoff
        db = _connect(root)
        if db is None:
            return False
        try:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            control = db.execute('SELECT enabled,epoch,cleared_before FROM session_title_control WHERE id=1').fetchone()
            if not control or control[0] != 1 or control[1] != epoch or started_at <= control[2] or not _settings_allow(root):
                db.rollback()
                return False
            path = Path(root) / 'observation-settings.json'
            try:
                settings = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                settings = {}
            if not isinstance(settings, dict) or started_at < retention_cutoff(settings):
                db.rollback()
                return False
            db.execute('INSERT OR IGNORE INTO session_title_metadata(agent,sid,title,source,at) VALUES(?,?,?,?,?)',
                       (agent, sid, title, 'gateway-user-message', started_at))
            db.execute('''DELETE FROM session_title_metadata WHERE source='gateway-user-message' AND rowid NOT IN (
                SELECT rowid FROM session_title_metadata WHERE source='gateway-user-message' ORDER BY at DESC,rowid DESC LIMIT ?
            )''', (MAX_GATEWAY_TITLES,))
            db.commit()
            return True
        finally:
            db.close()
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False
