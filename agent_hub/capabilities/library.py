"""Local, explicitly opted-in management of global skills and instructions.

Nothing in this module discovers or alters the user's files at import time. A
caller must register each source and explicitly publish it (or opt that item
into automatic synchronization). Paths passed to this service are local data;
the service does not contact an external provider.
"""
from __future__ import annotations

import base64
import copy
import ctypes
import difflib
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import uuid
import sys
from contextlib import contextmanager, ExitStack
from pathlib import Path

AGENTS = {
    "claude": (".claude/skills", ".claude/CLAUDE.md"),
    "codex": (".codex/skills", ".codex/AGENTS.md"),
    "pi": (".pi/agent/skills", ".pi/agent/AGENTS.md"),
    "dsh": (".dsh/skills", ".dsh/AGENTS.md"),
    "opencode": (".config/opencode/skills", ".config/opencode/AGENTS.md"),
}
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_WINDOWS_DEVICE = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.I)
_ID = re.compile(r"^[0-9a-f]{32}$")
_BLOCKS = re.compile(r"(?ms)^<!-- agent-hub:instruction:([0-9a-f]{32}):start -->\r?\n.*?^<!-- agent-hub:instruction:\1:end -->\r?\n?")
_MAX_BYTES = 32 * 1024 * 1024
_MAX_FILES = 2000
_PLAN_TTL = 15 * 60
_LOCK = threading.RLock()


class LibraryError(ValueError):
    """A safe-to-display validation or transaction failure."""


class RecoveryRequired(LibraryError):
    """A directory swap needs manual reconciliation; backup is retained."""


def _safe(path: Path) -> None:
    """Reject links and Windows junction/reparse points at all existing levels."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            info = os.lstat(part)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise LibraryError(f"路径包含符号链接或重解析点：{part}")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _bytes(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise LibraryError("版本数据损坏") from exc


def _file(path: Path):
    _safe(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise LibraryError(f"目标不是普通文件：{path}")
    if path.stat().st_size > _MAX_BYTES:
        raise LibraryError("文件超过大小限制")
    return {"data": _bytes(path.read_bytes()), "mode": stat.S_IMODE(path.stat().st_mode)}


def _tree(path: Path):
    _safe(path)
    if not path.exists():
        return None
    if not path.is_dir():
        raise LibraryError(f"技能目录不是目录：{path}")
    files, directories, size = {}, [], 0
    for current, dirs, names in os.walk(path, followlinks=False):
        dirs.sort(); names.sort()
        base = Path(current)
        _safe(base)
        for name in dirs:
            child = base / name
            _safe(child)
            directories.append(child.relative_to(path).as_posix())
        for name in names:
            child = base / name
            _safe(child)
            if not child.is_file():
                raise LibraryError(f"技能包含非普通文件：{child}")
            size += child.stat().st_size
            if size > _MAX_BYTES or len(files) >= _MAX_FILES:
                raise LibraryError("技能目录超过大小或文件数量限制")
            files[child.relative_to(path).as_posix()] = _file(child)
    return {"files": files, "dirs": directories}


def _source(item):
    path = Path(item["source"])
    if item["kind"] == "skill":
        snap = _tree(path)
        if not snap or "SKILL.md" not in snap["files"]:
            raise LibraryError("技能主来源必须是包含 SKILL.md 的目录")
        return snap
    snap = _file(path)
    if snap is None:
        raise LibraryError("指令主来源不存在")
    try:
        # A global file may also contain blocks from other library items. Do not
        # recursively spread those generated blocks as part of this source.
        text = _decode(snap["data"]).decode("utf-8-sig")
    except UnicodeError as exc:
        raise LibraryError("全局指令必须是 UTF-8 文本") from exc
    text = _BLOCKS.sub("", text).strip("\r\n")
    if re.search(r"<!-- agent-hub:instruction:[0-9a-f]{32}:(?:start|end) -->", text):
        raise LibraryError("主来源包含不完整的 Hub 指令区块，请先修复")
    if not text.strip():
        raise LibraryError("指令主来源为空")
    # The user-approved per-client variants are part of the published snapshot
    # and stability fingerprint, not live mutable settings applied to old text.
    return {"text": text, "overrides": _validated_overrides(item.get("overrides", {}), "instruction")}


def _validated_overrides(value, kind):
    """Validate explicit per-client exact replacements without inferring any."""
    if value is None:
        return {}
    if not isinstance(value, dict) or any(agent not in AGENTS for agent in value):
        raise LibraryError("按端文本替换配置无效")
    if kind != "instruction" and value:
        raise LibraryError("按端文本替换仅适用于全局指令")
    result = {}
    for agent, replacements in value.items():
        if not isinstance(replacements, list) or len(replacements) > 20:
            raise LibraryError("每端最多配置 20 条文本替换")
        seen = set()
        rows = []
        for pair in replacements:
            if not isinstance(pair, dict) or set(pair) != {"from", "to"}:
                raise LibraryError("替换项必须包含 from 和 to")
            before, after = pair["from"], pair["to"]
            if (not isinstance(before, str) or not before or not isinstance(after, str)
                or len(before) > 2048 or len(after) > 2048 or "\x00" in before or "\x00" in after
                or "<!-- agent-hub:instruction:" in after or before in seen):
                raise LibraryError("文本替换包含空原文、重复原文、无效字符或超长内容")
            seen.add(before)
            rows.append({"from": before, "to": after})
        if rows:
            result[agent] = rows
    return result


def _render_instruction(item, agent, source):
    """Apply only user-defined replacements and report unmatched inputs."""
    text = source["text"]
    unmatched = []
    for index, rule in enumerate(_validated_overrides(source.get("overrides", {}), "instruction").get(agent, []), 1):
        before, after = rule["from"], rule["to"]
        count = text.count(before)
        if not count:
            unmatched.append(index)
            continue
        old_size = len(text.encode("utf-8"))
        predicted = old_size + count * (len(after.encode("utf-8")) - len(before.encode("utf-8")))
        if predicted > _MAX_BYTES:
            raise LibraryError("按端替换后的指令超过大小限制")
        text = text.replace(before, after)
    if not text.strip():
        raise LibraryError("按端替换后的指令为空")
    return {"text": text}, unmatched


def _instruction_warnings(item, agent, source):
    variant, unmatched = _render_instruction(item, agent, source)
    warnings = []
    if unmatched:
        warnings.append(f"{agent} 的第 {', '.join(str(x) for x in unmatched)} 条替换未匹配主来源。")
    identity_pattern = r"(?i)(?:--agent\s+|agent\s*标识(?:\s*为|\s*[:：])\s*)[`\"']?(codex|claude|pi|dsh|opencode)\b"
    foreign = sorted({match.group(1).lower() for match in re.finditer(identity_pattern, variant["text"])
                      if match.group(1).lower() != agent})
    if foreign:
        warnings.append(f"{agent} 的指令包含可能指向其他客户端的硬编码标识：{', '.join(foreign)}；请核对并按需配置明确替换。")
    return variant, warnings


def _block(item_id, text, newline="\n"):
    body = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return (f"<!-- agent-hub:instruction:{item_id}:start -->\n{body}\n"
            f"<!-- agent-hub:instruction:{item_id}:end -->\n").replace("\n", newline)


def _instruction_target(old, item_id, source):
    raw = _decode(old["data"]) if old else b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise LibraryError("目标指令不是 UTF-8 文本，无法安全合并") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    starts = f"<!-- agent-hub:instruction:{item_id}:start -->"
    ends = f"<!-- agent-hub:instruction:{item_id}:end -->"
    matches = list(_BLOCKS.finditer(text))
    own = [m for m in matches if m.group(1) == item_id]
    if text.count(starts) != len(own) or text.count(ends) != len(own) or len(own) > 1:
        raise LibraryError("受管理指令区块不完整或重复")
    before = own[0].group(0) if own else None
    after = _block(item_id, source["text"], newline)
    if own:
        output = text[:own[0].start()] + after + text[own[0].end():]
    else:
        separator = "" if not text else (newline if text.endswith(("\n", "\r")) else newline * 2)
        output = text + separator + after
    data = (b"\xef\xbb\xbf" if bom else b"") + output.encode("utf-8")
    return before, after, {"data": _bytes(data), "mode": old["mode"] if old else 0o600}


def _instruction_restore(current, old, new, item_id, retired_before=None):
    """Restore this item's block while preserving other current rule blocks."""
    if current == new:
        return old
    if current is None or new is None:
        raise LibraryError("目标指令已被删除或修改")
    def read_block(snapshot):
        if snapshot is None:
            return None, "", False
        raw = _decode(snapshot["data"])
        bom = raw.startswith(b"\xef\xbb\xbf")
        try: text = raw.decode("utf-8-sig")
        except UnicodeError as exc: raise LibraryError("目标指令不是 UTF-8 文本") from exc
        matches = [m for m in _BLOCKS.finditer(text) if m.group(1) == item_id]
        starts = text.count(f"<!-- agent-hub:instruction:{item_id}:start -->")
        ends = text.count(f"<!-- agent-hub:instruction:{item_id}:end -->")
        if len(matches) > 1 or starts != len(matches) or ends != len(matches):
            raise LibraryError("受管理指令区块不完整或重复")
        return matches[0] if matches else None, text, bom
    expected, _, _ = read_block(new)
    before, _, _ = read_block(old)
    actual, text, bom = read_block(current)
    if (actual.group(0) if actual else None) != (expected.group(0) if expected else None):
        raise LibraryError("该项受管理区块在发布后已被修改")
    if actual is None:
        return current
    replacement = before.group(0) if before else retired_before or ""
    output = text[:actual.start()] + replacement + text[actual.end():]
    if old is None and not output.strip():
        return None
    raw = (b"\xef\xbb\xbf" if bom else b"") + output.encode("utf-8")
    return {"data": _bytes(raw), "mode": current["mode"]}


def _retired_blocks(snapshot, retired_ids):
    if snapshot is None or not retired_ids:
        return []
    try: text = _decode(snapshot["data"]).decode("utf-8-sig")
    except UnicodeError as exc: raise LibraryError("目标指令不是 UTF-8 文本") from exc
    matches = [(m.group(1), m.group(0)) for m in _BLOCKS.finditer(text) if m.group(1) in retired_ids]
    for old_id in retired_ids:
        count = sum(1 for found, _ in matches if found == old_id)
        if text.count(f"<!-- agent-hub:instruction:{old_id}:start -->") != count or text.count(f"<!-- agent-hub:instruction:{old_id}:end -->") != count:
            raise LibraryError("停止管理的旧指令区块不完整，请先手动处理")
    return matches


def _adopt_instruction(old, item_id, source, retired_id):
    raw = _decode(old["data"])
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    matches = [m for m in _BLOCKS.finditer(text) if m.group(1) == retired_id]
    if len(matches) != 1:
        raise LibraryError("旧受管理区块不完整或重复，请先手动处理")
    newline = "\r\n" if "\r\n" in text else "\n"
    before = matches[0].group(0)
    after = _block(item_id, source["text"], newline)
    output = text[:matches[0].start()] + after + text[matches[0].end():]
    data = (b"\xef\xbb\xbf" if bom else b"") + output.encode("utf-8")
    return before, after, {"data": _bytes(data), "mode": old["mode"]}


def _atomic_file(path, snapshot):
    _safe(path)
    if snapshot is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".agent-hub-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_decode(snapshot["data"]))
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(tmp, snapshot["mode"])
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _validate_relative(rel):
    p = Path(rel)
    if p.is_absolute() or not p.parts or any(x in ("", ".", "..") for x in p.parts) or "\\" in rel:
        raise LibraryError("版本数据含不安全的相对路径")
    return p


def _valid_name(name, kind, targets):
    return (isinstance(name, str) and bool(_NAME.fullmatch(name)) and len(name) <= 64
            and not _WINDOWS_DEVICE.fullmatch(name)
            and not (kind == "skill" and "claude" in targets and name in ("synced", "anthropic-skills")))


def _rename_noreplace(source, destination):
    """Atomic rename that never replaces a new concurrent destination."""
    source, destination = Path(source), Path(destination)
    _safe(source); _safe(destination.parent)
    if os.name == "nt":
        # Unlike os.replace, Windows os.rename fails if destination exists.
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise LibraryError("此系统不支持安全的不覆盖目录交换")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
        return
    raise LibraryError("此系统不支持安全的不覆盖目录交换")


def _stage_native(path, snapshot, kind):
    """Prepare a new target under the transaction's journaled unique path."""
    if snapshot is None:
        return
    _safe(path)
    if kind == "instruction":
        with path.open("xb") as stream:
            stream.write(_decode(snapshot["data"]))
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(path, snapshot["mode"])
        return
    path.mkdir()
    for rel in snapshot["dirs"]:
        (path / _validate_relative(rel)).mkdir(parents=True, exist_ok=True)
    for rel, content in snapshot["files"].items():
        dest = path / _validate_relative(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as stream:
            stream.write(_decode(content["data"]))
        os.chmod(dest, content["mode"])


def _native_snapshot(path, kind):
    return _tree(path) if kind == "skill" else _file(path)


@contextmanager
def _windows_write_guard(path, snapshot, kind):
    """Prevent already-open or new write handles during a Windows swap.

    READ and DELETE remain shared so our own reads and same-volume renames
    work. A writer that opened before this guard prevents acquisition instead
    of silently writing into a detached escrow object after publication.
    """
    if os.name != "nt" or snapshot is None:
        yield
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]; close.restype = wintypes.BOOL
    # Windows refuses renaming a directory if *any child* is open, even when
    # that child's handle shares DELETE. Holding our own child handles would
    # therefore make every skill update fail. Guard the directory itself;
    # the OS denies a directory move while any descendant handle is open,
    # and the moved tree is fingerprinted immediately after the move.
    paths = [path]
    handles = []
    try:
        for entry in paths:
            _safe(entry)
            handle = create(str(entry), 0x80000000, 0x1 | 0x4, None, 3, 0x02000000, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise LibraryError(f"目标正在使用或无法锁定，未写入：{entry}")
            handles.append(handle)
        if _native_snapshot(path, kind) != snapshot:
            raise LibraryError("目标在锁定期间发生变化，未写入")
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


def _target_storage(path):
    # Never leave staged/old SKILL.md files inside a client's discoverable
    # skills root: some clients scan that root recursively, including dot dirs.
    base = path.parent.parent if path.parent.name.lower() == "skills" else path.parent
    return base / ".agent-hub-transactions"


def _op_paths(op, tx, phase):
    path = Path(op["path"])
    label = f"{tx}-{phase}-{op['agent']}"
    storage = _target_storage(path)
    return {"stage": str(storage / (".agent-hub-stage-" + label)),
            "escrow": str(storage / (".agent-hub-escrow-" + label)),
            "undo": str(storage / (".agent-hub-undo-" + label))}


def _target_marker(path):
    path = Path(path)
    identity = os.path.normcase(os.path.abspath(path)).encode("utf-8")
    return _target_storage(path) / ("pending-" + hashlib.sha256(identity).hexdigest() + ".json")


def _swap_native(op, kind):
    """Capture the actual old target before checking it and never overwrite."""
    path = Path(op["path"]); stage = Path(op["stage"]); escrow = Path(op["escrow"])
    _safe(path); path.parent.mkdir(parents=True, exist_ok=True); _safe(path.parent)
    _safe(stage.parent); stage.parent.mkdir(parents=True, exist_ok=True); _safe(stage.parent)
    if op["old"] == op["new"]:
        if _native_snapshot(path, kind) != op["old"]:
            raise LibraryError(f"目标在写入前发生变化：{op['agent']}")
        return
    _stage_native(stage, op["new"], kind)
    with _windows_write_guard(path, op["old"], kind):
        _swap_native_locked(op, kind, path, stage, escrow)


def _swap_native_locked(op, kind, path, stage, escrow):
    if op["old"] is not None:
        try:
            _rename_noreplace(path, escrow)
        except OSError as exc:
            raise LibraryError(f"目标在交换时变化或正在使用：{op['agent']}") from exc
        actual = _native_snapshot(escrow, kind)
        if actual != op["old"]:
            try:
                _rename_noreplace(escrow, path)
            except OSError as exc:
                raise RecoveryRequired(f"目标并发变化，原内容保留在 {escrow}") from exc
            raise LibraryError(f"目标在交换时发生变化：{op['agent']}")
    try:
        if op["new"] is not None:
            _rename_noreplace(stage, path)
    except OSError as exc:
        if op["old"] is not None and escrow.exists():
            try: _rename_noreplace(escrow, path)
            except OSError:
                raise RecoveryRequired(f"目标出现新的内容，原内容保留在 {escrow}") from exc
        raise LibraryError(f"目标在交换时出现新内容：{op['agent']}") from exc


def _undo_native(op, kind):
    """Undo a journaled swap; retain all displaced data and refuse conflicts."""
    path = Path(op["path"]); escrow = Path(op["escrow"]); undo = Path(op["undo"]); stage = Path(op["stage"])
    actual = _native_snapshot(path, kind)
    old, new = op["old"], op["new"]
    if old == new:
        if actual != old:
            raise LibraryError(f"目标已改变：{op['agent']}")
        return
    if actual == old:
        return  # This operation never started, or was already safely undone.
    if stage.exists() and not escrow.exists():
        # The staged replacement was never installed; an independent change
        # to the original path must be left untouched.
        return
    backed = _native_snapshot(escrow, kind)
    if old is not None and backed != old:
        raise LibraryError(f"事务原内容缺失或已改变：{op['agent']}；保留位置 {escrow}")
    if actual is not None and actual != new:
        raise LibraryError(f"目标在事务后发生变化：{op['agent']}；未覆盖")
    with ExitStack() as guards:
        if actual is not None:
            guards.enter_context(_windows_write_guard(path, actual, kind))
        if old is not None:
            guards.enter_context(_windows_write_guard(escrow, old, kind))
        if actual is not None:
            try:
                _rename_noreplace(path, undo)
            except OSError as exc:
                raise LibraryError(f"目标无法安全移开：{op['agent']}") from exc
            moved = _native_snapshot(undo, kind)
            if moved != new:
                try: _rename_noreplace(undo, path)
                except OSError as exc: raise RecoveryRequired(f"并发修改保留在 {undo}") from exc
                raise LibraryError(f"目标在恢复时发生变化：{op['agent']}")
        if old is not None:
            try: _rename_noreplace(escrow, path)
            except OSError as exc:
                raise RecoveryRequired(f"目标出现新内容，原内容保留在 {escrow}") from exc


def _atomic_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_file(path, {"data": _bytes(raw), "mode": 0o600})


class CapabilityLibrary:
    """Per-Hub local library. All mutations happen only through explicit methods."""

    def __init__(self, root, home):
        self.root = Path(os.path.abspath(root)) / "capability-library"
        self.home = Path(os.path.abspath(home))
        self._plans = {}

    @contextmanager
    def _guard(self):
        """Serialize library writes across desktop/browser processes as well."""
        with _LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            lock_path = self.root / "library.lock"
            _safe(lock_path)
            with open(lock_path, "a+b") as stream:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0"); stream.flush()
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise LibraryError("能力库正在被另一个进程更新，请重试") from exc
                try:
                    yield
                finally:
                    if os.name == "nt":
                        stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read(self):
        path = self.root / "state.json"
        _safe(path)
        if not path.exists():
            return {"version": 1, "items": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("items"), list):
                raise ValueError("unsupported state")
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise LibraryError("能力库状态无法读取或格式不受支持") from exc

    def _save(self, state):
        _atomic_json(self.root / "state.json", state)

    def _pending_path(self, item_id):
        if not isinstance(item_id, str) or not _ID.fullmatch(item_id):
            raise LibraryError("能力项标识无效")
        return self.root / "pending" / f"{item_id}.json"

    def _pending(self, item):
        """Return the journal ID if a possibly interrupted operation exists."""
        path = self._pending_path(item["id"])
        _safe(path)
        if not path.exists():
            return None
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
            tx = marker.get("transactionId")
            if not isinstance(tx, str) or not _ID.fullmatch(tx):
                return "unknown"
            journal = self.root / "transactions" / f"{tx}.json"
            _safe(journal)
            if not journal.exists():
                # Marker is written before its journal and before any native
                # operation; absent journal therefore cannot hide a write.
                return None
            record = json.loads(journal.read_text(encoding="utf-8"))
            status = record.get("status")
            if status in ("preparing", "rollback_failed", "restoring", "restore_failed"):
                return tx
            if (status == "applied" and item.get("headTransaction") != tx and
                    item.get("headTransaction") == record.get("oldHeadTransaction") and
                    item.get("published") == record.get("oldPublished")):
                return tx
            return None
        except (OSError, ValueError, TypeError, AttributeError):
            return "unknown"

    def _ensure_no_pending(self, item):
        pending = self._pending(item)
        if pending:
            raise LibraryError(f"存在未完成的事务 {pending}，请先在发布记录中恢复")

    def _mark_pending(self, item_id, transaction_id):
        _atomic_json(self._pending_path(item_id), {"transactionId": transaction_id})

    def _clear_pending(self, item_id, transaction_id):
        path = self._pending_path(item_id)
        _safe(path)
        if not path.exists():
            return
        try: marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError): return
        if marker.get("transactionId") == transaction_id:
            path.unlink()

    @contextmanager
    def _target_guards(self, operations):
        """Locks are shared by all Hub roots that write the same target."""
        paths = sorted({_target_marker(op["path"]) for op in operations}, key=lambda p: os.path.normcase(str(p)))
        with ExitStack() as stack:
            for marker in paths:
                _safe(marker.parent); marker.parent.mkdir(parents=True, exist_ok=True); _safe(marker.parent)
                lock_path = marker.with_suffix(".lock"); _safe(lock_path)
                stream = stack.enter_context(lock_path.open("a+b"))
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0: stream.write(b"0"); stream.flush()
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        stack.callback(lambda s=stream: (s.seek(0), msvcrt.locking(s.fileno(), msvcrt.LK_UNLCK, 1)))
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        stack.callback(lambda s=stream: fcntl.flock(s.fileno(), fcntl.LOCK_UN))
                except OSError as exc:
                    raise LibraryError("客户端文件正在被另一个 Hub 更新，请重试") from exc
            yield

    def _target_reservation(self, target):
        """An uncertain or incomplete foreign marker is always active."""
        marker = _target_marker(target); _safe(marker)
        if not marker.exists():
            return None
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            tx, item_id, root = data.get("transactionId"), data.get("itemId"), data.get("root")
            if not isinstance(tx, str) or not _ID.fullmatch(tx) or not isinstance(item_id, str) or not _ID.fullmatch(item_id) or not isinstance(root, str) or not os.path.isabs(root):
                return {"active": True, "transactionId": "unknown", "root": "unknown"}
            journal = Path(root) / "transactions" / f"{tx}.json"; _safe(journal)
            record = json.loads(journal.read_text(encoding="utf-8"))
            if record.get("id") != tx or record.get("itemId") != item_id:
                raise ValueError("mismatch")
            status = record.get("status")
            completed = status in ("restored", "rolled_back")
            if status == "applied":
                state_path = Path(root) / "state.json"; _safe(state_path)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                item = next((v for v in state.get("items", []) if v.get("id") == item_id), None)
                # A committed item may later be deliberately removed from Hub.
                # Removal is blocked while its own transaction is unfinished.
                completed = item is None or (item.get("headTransaction") == tx and item.get("published") == record.get("newPublished"))
            return {"active": not completed, "transactionId": tx, "root": root}
        except (OSError, ValueError, TypeError, AttributeError, LibraryError):
            return {"active": True, "transactionId": "unknown", "root": "unknown"}

    def _reserve_targets(self, operations, item_id, transaction_id, allow_own=False):
        created = []
        try:
            for op in operations:
                target = Path(op["path"]); marker = _target_marker(target)
                reservation = self._target_reservation(target)
                if reservation and reservation["active"]:
                    own = (reservation["transactionId"] == transaction_id and
                           os.path.normcase(reservation["root"]) == os.path.normcase(str(self.root)))
                    if allow_own and own:
                        continue
                    raise LibraryError(f"目标有未完成事务 {reservation['transactionId']}，请在原 Hub 恢复")
                if reservation:
                    # Caller holds the common target lock, so no other Hub can
                    # replace this completed marker between check and removal.
                    marker.unlink()
                data = {"root": str(self.root), "itemId": item_id, "transactionId": transaction_id}
                fd, temporary = tempfile.mkstemp(prefix=".agent-hub-reservation-", dir=str(marker.parent))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                        json.dump(data, stream, ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
                    # A crash before this rename leaves only an unreferenced
                    # temp file, never a partially written active marker.
                    _rename_noreplace(Path(temporary), marker)
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
                created.append(marker)
        except Exception:
            for marker in created:
                try: marker.unlink()
                except OSError: pass
            raise

    def _release_targets(self, operations, transaction_id):
        for op in operations:
            marker = _target_marker(op["path"]); _safe(marker)
            if not marker.exists(): continue
            try: row = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError): continue
            if row.get("transactionId") == transaction_id and os.path.normcase(row.get("root", "")) == os.path.normcase(str(self.root)):
                marker.unlink()

    @staticmethod
    def _item(state, item_id):
        if not isinstance(item_id, str) or not _ID.fullmatch(item_id):
            raise LibraryError("能力项标识无效")
        item = next((v for v in state["items"] if v.get("id") == item_id), None)
        if item is None:
            raise LibraryError("能力项不存在")
        return item

    def _path(self, item, agent):
        if agent not in AGENTS:
            raise LibraryError("不支持的客户端")
        if not _valid_name(item.get("name"), item.get("kind"), [agent]) or item.get("kind") not in ("skill", "instruction"):
            raise LibraryError("能力项名称或类型无效")
        skill, instruction = AGENTS[agent]
        return self.home / (skill if item["kind"] == "skill" else instruction) / item["name"] if item["kind"] == "skill" else self.home / instruction

    def list_items(self):
        with _LOCK:
            state = self._read()
            rows = []
            for item in state["items"]:
                row = copy.deepcopy(item)
                row["pendingTransaction"] = self._pending(item)
                current = None
                try:
                    current = _source(item)
                    row["sourceHash"] = _digest(current)
                    row["sourceStatus"] = "unpublished" if not item.get("published") else ("current" if row["sourceHash"] == item["published"] else "changed")
                except (LibraryError, OSError) as exc:
                    row["sourceStatus"] = "error"
                    row["sourceError"] = str(exc)
                row["targetPaths"] = {agent: str(self._path(item, agent)) for agent in item["targets"]}
                statuses, auto_warnings, target_pending = {}, {}, {}
                try:
                    published = self._snapshot(item["published"]) if item.get("published") else None
                except (LibraryError, OSError):
                    published = None
                for agent in item["targets"]:
                    try:
                        path = self._path(item, agent)
                        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(item["source"]):
                            statuses[agent] = "source"; continue
                        reservation = self._target_reservation(path)
                        if reservation and reservation["active"]:
                            statuses[agent] = "conflict"; target_pending[agent] = reservation; continue
                        if item["kind"] == "instruction" and item.get("mode") == "auto" and current:
                            _variant, issues = _instruction_warnings(item, agent, current)
                            if issues:
                                statuses[agent] = "conflict"; auto_warnings[agent] = issues; continue
                        old = _tree(path) if item["kind"] == "skill" else _file(path)
                        prior = item.get("applied", {}).get(agent)
                        if item["kind"] == "skill":
                            if prior is None and old is not None:
                                statuses[agent] = "conflict"; continue
                            if prior is not None and prior.get("hash") != _digest(old):
                                statuses[agent] = "conflict"; continue
                            desired_hash = _digest(published) if published else None
                        else:
                            variant = _render_instruction(item, agent, published)[0] if published else {"text": ""}
                            before, after, _ = _instruction_target(old, item["id"], variant)
                            retired_ids = {r.get("id") for r in state.get("retired", [])
                                           if r.get("kind") == "instruction" and r.get("name") == item["name"]}
                            if _retired_blocks(old, retired_ids):
                                statuses[agent] = "conflict"; continue
                            if (prior is None and before is not None) or (prior is not None and prior.get("hash") != _digest(before)):
                                statuses[agent] = "conflict"; continue
                            desired_hash = _digest(after) if published else None
                        if not published:
                            statuses[agent] = "unpublished"
                        elif prior is None or prior.get("hash") != desired_hash:
                            statuses[agent] = "pending"
                        else:
                            statuses[agent] = "current"
                    except (LibraryError, OSError):
                        statuses[agent] = "error"
                row["targetStatus"] = statuses
                row["autoWarnings"] = auto_warnings
                row["targetPending"] = target_pending
                rows.append(row)
            return {"items": rows, "agents": list(AGENTS)}

    def discover(self):
        """List candidates by metadata only; never read or import their text."""
        roots = {
            ".claude/skills": ["claude", "opencode"],
            ".codex/skills": ["codex"],
            ".agents/skills": ["codex", "pi", "dsh", "opencode"],
            ".pi/agent/skills": ["pi"],
            ".dsh/skills": ["dsh"],
            ".config/opencode/skills": ["opencode"],
        }
        candidates, warnings = [], []
        for relative, agents in roots.items():
            directory = self.home / relative
            try:
                _safe(directory)
                if not directory.is_dir(): continue
                for child in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
                    if child.name.startswith("."): continue
                    try:
                        _safe(child)
                        if child.is_dir() and (child / "SKILL.md").is_file():
                            _safe(child / "SKILL.md")
                            supported = bool(_NAME.fullmatch(child.name) and len(child.name) <= 64)
                            candidates.append({"kind": "skill", "name": child.name, "source": str(child),
                                               "foundBy": agents, "canImport": supported})
                        elif child.is_file() and child.suffix.lower() == ".md":
                            candidates.append({"kind": "skill", "name": child.stem, "source": str(child),
                                               "foundBy": [a for a in agents if a in ("pi", "dsh", "opencode")],
                                               "canImport": False, "reason": "单文件技能暂不能作为目录导入"})
                    except (LibraryError, OSError):
                        warnings.append(f"跳过无法安全检查的项目：{child}")
            except (LibraryError, OSError):
                warnings.append(f"无法检查技能目录：{directory}")
        instructions = {rel: [agent] for agent, (_skill, rel) in AGENTS.items()}
        instructions[".agents/AGENTS.md"] = ["dsh"]
        for relative, agents in instructions.items():
            path = self.home / relative
            try:
                _safe(path)
                if path.is_file():
                    candidates.append({"kind": "instruction", "name": path.stem.lower(),
                                       "source": str(path), "foundBy": agents, "canImport": True})
            except (LibraryError, OSError):
                warnings.append(f"无法检查指令文件：{path}")
        return {"candidates": candidates, "warnings": warnings}

    def import_item(self, body):
        with self._guard():
            kind = body.get("kind")
            name = body.get("name", "")
            source = body.get("source", "")
            targets = body.get("targets", [])
            mode = body.get("mode", "manual")
            if kind not in ("skill", "instruction"):
                raise LibraryError("能力项类型无效")
            if not isinstance(source, str) or not os.path.isabs(source):
                raise LibraryError("主来源需要绝对路径")
            if not isinstance(targets, list) or any(not isinstance(a, str) or a not in AGENTS for a in targets) or len(set(targets)) != len(targets):
                raise LibraryError("目标客户端无效")
            if not _valid_name(name, kind, targets):
                raise LibraryError("名称须为 1–64 位小写字母、数字和单横线，且不能是客户端保留名称")
            if mode not in ("manual", "auto"):
                raise LibraryError("同步方式无效")
            overrides = _validated_overrides(body.get("overrides", {}), kind)
            item = {"id": uuid.uuid4().hex, "kind": kind, "name": name,
                    "source": os.path.abspath(source), "targets": targets,
                    "mode": mode, "overrides": overrides, "published": None, "applied": {}, "createdAt": time.time()}
            _source(item)
            state = self._read()
            if any(x["kind"] == kind and x["name"] == name for x in state["items"]):
                raise LibraryError("已有同类型、同名能力项")
            state["items"].append(item)
            self._save(state)
            return copy.deepcopy(item)

    def update_item(self, item_id, body):
        with self._guard():
            state = self._read(); item = self._item(state, item_id)
            self._ensure_no_pending(item)
            if "targets" in body:
                targets = body["targets"]
                if not isinstance(targets, list) or any(not isinstance(a, str) or a not in AGENTS for a in targets) or len(set(targets)) != len(targets):
                    raise LibraryError("目标客户端无效")
                if not _valid_name(item["name"], item["kind"], targets):
                    raise LibraryError("该客户端使用此名称作为保留名称")
                item["targets"] = targets
            if "mode" in body:
                if body["mode"] not in ("manual", "auto"):
                    raise LibraryError("同步方式无效")
                item["mode"] = body["mode"]
            if "overrides" in body:
                item["overrides"] = _validated_overrides(body["overrides"], item["kind"])
            # Changing a source has the same explicit validation as importing.
            if "source" in body:
                source = body["source"]
                if not isinstance(source, str) or not os.path.isabs(source):
                    raise LibraryError("主来源需要绝对路径")
                candidate = dict(item, source=os.path.abspath(source)); _source(candidate)
                item["source"] = candidate["source"]
            self._save(state)
            return copy.deepcopy(item)

    def remove_item(self, item_id):
        """Forget this item and stop auto-sync, leaving all native files alone."""
        with self._guard():
            state = self._read()
            item = self._item(state, item_id)
            self._ensure_no_pending(item)
            if item["kind"] == "instruction":
                # Keep identity only; the previous block remains user data.
                state.setdefault("retired", []).append({"id": item_id, "kind": item["kind"], "name": item["name"]})
            state["items"] = [item for item in state["items"] if item["id"] != item_id]
            self._save(state)
            for token in [token for token, plan in self._plans.items() if plan["item"]["id"] == item_id]:
                self._plans.pop(token, None)
            return {"ok": True, "itemId": item_id, "nativeFilesRetained": True}

    def _snapshot(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise LibraryError("版本标识无效")
        path = self.root / "snapshots" / f"{digest}.json"
        _safe(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LibraryError("已发布版本无法读取") from exc
        if _digest(data) != digest:
            raise LibraryError("已发布版本完整性校验失败")
        return data

    def preview(self, item_id, body=None):
        body = body or {}
        with _LOCK:
            state = self._read(); item = copy.deepcopy(self._item(state, item_id))
            self._ensure_no_pending(item)
            force = body.get("forceTargets", [])
            if not isinstance(force, list) or any(x not in item["targets"] for x in force):
                raise LibraryError("冲突处理目标无效")
            from_published = body.get("published", False)
            if not isinstance(from_published, bool):
                raise LibraryError("版本选择无效")
            source = self._snapshot(item["published"]) if from_published and item.get("published") else _source(item)
            if from_published and not item.get("published"):
                raise LibraryError("尚无已发布版本")
            source_hash = _digest(source)
            operations, results, warnings, auto_unsafe = [], [], [], []
            if item["kind"] == "skill":
                if os.name == "nt":
                    warnings.append("Windows 发布或恢复会重建技能目录；快照不保证保留原 ACL、安全描述符及全部文件元数据。同盘保留原目录供核查。")
                try:
                    header = _decode(source["files"]["SKILL.md"]["data"]).decode("utf-8-sig")
                    frontmatter = re.match(r"\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|$)", header, re.S)
                    match = re.search(r"(?m)^name:\s*['\"]?([^'\"\r\n#]+)", frontmatter.group(1)) if frontmatter else None
                    description = re.search(r"(?m)^description:\s*\S", frontmatter.group(1)) if frontmatter else None
                    if not match or match.group(1).strip() != item["name"] or not description:
                        warnings.append("SKILL.md 的 name、description 或目录名称可能不满足各客户端的技能发现要求；请检查后在各端验证。")
                except (UnicodeError, KeyError, LibraryError):
                    warnings.append("SKILL.md 不是可检查的 UTF-8 文本，不能确认客户端会识别该技能。")
                shared = self.home / ".agents/skills" / item["name"]
                if os.path.normcase(os.path.abspath(shared)) == os.path.normcase(item["source"]):
                    warnings.append("主来源位于共享技能目录；Codex、pi、dsh 和 OpenCode 可能直接读取它，即使尚未发布到目标目录。")
                if shared.exists():
                    warnings.append(f"共享目录存在同名技能：{shared}。多来源发现或优先顺序由各客户端决定。")
                claude_shared = self.home / ".claude/skills" / item["name"]
                if "claude" in item["targets"] or claude_shared.exists():
                    warnings.append("OpenCode 也可能读取 Claude 的全局技能目录；该目录的同名技能可能影响 OpenCode。")
            for agent in item["targets"]:
                path = self._path(item, agent)
                variant = source
                if item["kind"] == "instruction":
                    variant, issue = _instruction_warnings(item, agent, source)
                    warnings.extend(issue)
                    if issue:
                        auto_unsafe.append(agent)
                if os.path.normcase(os.path.abspath(path)) == os.path.normcase(item["source"]):
                    if item["kind"] == "instruction" and source.get("overrides", {}).get(agent):
                        warnings.append(f"{agent} 的目标就是主来源，Hub 不会改写源文件，该端替换不会生效。")
                    results.append({"agent": agent, "path": str(path), "status": "source", "detail": "主来源本身，不改写"})
                    continue
                if item["kind"] == "skill":
                    src = os.path.normcase(os.path.abspath(item["source"]))
                    dst = os.path.normcase(os.path.abspath(path))
                    try: overlap = os.path.commonpath((src, dst)) in (src, dst)
                    except ValueError: overlap = False  # Different Windows drives.
                    if overlap:
                        raise LibraryError("技能主来源与目标目录不能互相包含")
                reservation = self._target_reservation(path)
                if reservation and reservation["active"]:
                    results.append({"agent": agent, "path": str(path), "status": "conflict",
                                    "blockedByRecovery": True,
                                    "detail": f"此目标有未完成事务 {reservation['transactionId']}；请在原 Hub 数据目录 {reservation['root']} 恢复。"})
                    continue
                old = _tree(path) if item["kind"] == "skill" else _file(path)
                old_hash = _digest(old)
                prior = item.get("applied", {}).get(agent)
                detail, conflict, retired_before = "", False, None
                if item["kind"] == "skill":
                    new = source
                    if old is not None and prior is None:
                        conflict = True; detail = "目标已有非本能力库管理的技能，需明确处理"
                    elif prior is not None and prior.get("hash") != old_hash:
                        conflict = True; detail = "目标技能自上次发布后发生变化"
                    before_names = set(old["files"]) if old else set()
                    after_names = set(new["files"])
                    diff = {"added": sorted(after_names - before_names), "removed": sorted(before_names - after_names),
                            "changed": sorted(x for x in before_names & after_names if old["files"][x] != new["files"][x])}
                    # Present actual text differences for small UTF-8 files,
                    # while keeping binary resources as filename-only changes.
                    text_diffs, remaining = {}, 16000
                    for rel in diff["added"] + diff["removed"] + diff["changed"]:
                        if remaining <= 0: break
                        previous = old["files"].get(rel) if old else None
                        following = new["files"].get(rel)
                        try:
                            a = _decode(previous["data"]) if previous else b""
                            b = _decode(following["data"]) if following else b""
                            if max(len(a), len(b)) > 65536 or b"\x00" in a or b"\x00" in b: continue
                            before_text, after_text = a.decode("utf-8"), b.decode("utf-8")
                            unified = "".join(difflib.unified_diff(before_text.splitlines(True), after_text.splitlines(True),
                                                                   fromfile=f"当前/{rel}", tofile=f"发布/{rel}", n=3))
                            if unified:
                                text_diffs[rel] = unified[:remaining]
                                remaining -= len(text_diffs[rel])
                        except (UnicodeError, LibraryError):
                            pass
                    diff["textFiles"] = text_diffs
                else:
                    before_block, after_block, new = _instruction_target(old, item_id, variant)
                    retired_ids = {r.get("id") for r in state.get("retired", [])
                                   if r.get("kind") == "instruction" and r.get("name") == item["name"]}
                    retired_blocks = _retired_blocks(old, retired_ids)
                    if len(retired_blocks) > 1 or (retired_blocks and before_block is not None):
                        raise LibraryError(f"{agent} 存在多个同名指令区块，请先手动处理")
                    if retired_blocks:
                        retired_before, after_block, new = _adopt_instruction(old, item_id, variant, retired_blocks[0][0])
                        before_block = retired_before
                        conflict = True; detail = "存在停止管理时保留的旧同名区块，需明确选择接管"
                    if prior is not None and prior.get("hash") != _digest(before_block):
                        conflict = True; detail = "受管理区块自上次发布后发生变化或被删除"
                    if prior is None and before_block is not None and not retired_before:
                        conflict = True; detail = "目标存在未登记的同标识区块"
                    diff = {"text": "".join(difflib.unified_diff((before_block or "").splitlines(True), after_block.splitlines(True),
                                                                  fromfile="当前受管理区块", tofile="准备发布", n=3))[:16000]}
                if conflict and agent not in force:
                    results.append({"agent": agent, "path": str(path), "status": "conflict", "detail": detail, "diff": diff})
                    continue
                status = "unchanged" if old == new else ("create" if old is None else "update")
                results.append({"agent": agent, "path": str(path), "status": status, "detail": detail, "forced": conflict, "diff": diff})
                if item["kind"] == "instruction":
                    applied_hash = _digest(after_block)
                else:
                    applied_hash = _digest(new)
                operations.append({"agent": agent, "path": str(path), "old": old, "new": new, "beforeHash": old_hash,
                                   "appliedHash": applied_hash, "prior": copy.deepcopy(prior),
                                   "retiredBefore": retired_before})
            token = uuid.uuid4().hex
            self._plans[token] = {"at": time.monotonic(), "item": item, "source": source,
                                  "sourceHash": source_hash, "fromPublished": from_published,
                                  "operations": operations, "results": results}
            self._plans = {k: v for k, v in self._plans.items() if time.monotonic() - v["at"] < _PLAN_TTL}
            return {"planId": token, "itemId": item_id, "sourceHash": source_hash,
                    "publishedVersion": from_published, "results": results,
                    "warnings": warnings, "autoUnsafeTargets": auto_unsafe,
                    "hasConflicts": any(x["status"] == "conflict" for x in results)}

    def discard_preview(self, plan_id):
        """Discard an unused in-memory preview without touching native files."""
        if not isinstance(plan_id, str) or not _ID.fullmatch(plan_id):
            raise LibraryError("预览标识无效")
        with _LOCK:
            return {"ok": True, "discarded": self._plans.pop(plan_id, None) is not None}

    def apply(self, plan_id, skip_conflicts=False, require_auto=False):
        with self._guard():
            plan = self._plans.pop(plan_id, None)
            if not plan or time.monotonic() - plan["at"] >= _PLAN_TTL:
                raise LibraryError("预览已过期，请重新预览")
            if any(x["status"] == "conflict" for x in plan["results"]) and not skip_conflicts:
                raise LibraryError("存在冲突，请处理后重新预览")
            state = self._read(); item = self._item(state, plan["item"]["id"])
            self._ensure_no_pending(item)
            if item != plan["item"]:
                raise LibraryError("能力项已变更，请重新预览")
            if require_auto and (item.get("mode") != "auto" or not item.get("published")):
                raise LibraryError("自动同步已暂停或尚未首次发布")
            conflicts = any(x["status"] == "conflict" for x in plan["results"])
            source_only = any(x["status"] == "source" for x in plan["results"])
            if not plan["operations"] and (conflicts or not source_only):
                raise LibraryError("没有可安全发布的目标；请先处理冲突或选择目标")
            if conflicts and not any(op["old"] != op["new"] for op in plan["operations"]):
                raise LibraryError("没有可安全更新的目标；请先处理冲突")
            if not plan["fromPublished"] and _digest(_source(item)) != plan["sourceHash"]:
                raise LibraryError("主来源已变更，请重新预览")
            for op in plan["operations"]:
                path = Path(op["path"])
                now = _tree(path) if item["kind"] == "skill" else _file(path)
                if _digest(now) != op["beforeHash"]:
                    raise LibraryError(f"目标在预览后发生变化：{op['agent']}")
            tx = uuid.uuid4().hex
            self.root.mkdir(parents=True, exist_ok=True); _safe(self.root)
            if not plan["fromPublished"]:
                _atomic_json(self.root / "snapshots" / f"{plan['sourceHash']}.json", plan["source"])
            operations = copy.deepcopy(plan["operations"])
            for op in operations:
                op.update(_op_paths(op, tx, "publish"))
            with self._target_guards(operations):
                return self._apply_record(plan, state, item, tx, operations)

    def _apply_record(self, plan, state, item, tx, operations):
        record = {"id": tx, "itemId": item["id"], "kind": item["kind"], "createdAt": time.time(),
                  "oldPublished": item.get("published"), "newPublished": plan["sourceHash"],
                  "oldHeadTransaction": item.get("headTransaction"),
                  "operations": operations, "status": "preparing", "targetsReserved": False}
        tx_path = self.root / "transactions" / f"{tx}.json"
        self._mark_pending(item["id"], tx)
        _atomic_json(tx_path, record)
        began = False
        try:
            self._reserve_targets(operations, item["id"], tx)
            record["targetsReserved"] = True
            _atomic_json(tx_path, record)
            began = True
            for op in operations:
                _swap_native(op, item["kind"])
                item["applied"][op["agent"]] = {"hash": op["appliedHash"], "at": time.time()}
            item["published"] = plan["sourceHash"]
            item["publishedAt"] = time.time()
            item["headTransaction"] = tx
            record["status"] = "applied"; _atomic_json(tx_path, record)
            self._save(state)
        except Exception as exc:
            failures = [str(exc)] if isinstance(exc, RecoveryRequired) else []
            if began:
                for op in reversed(operations):
                    try: _undo_native(op, item["kind"])
                    except Exception: failures.append(op["agent"])
            record["status"] = "rollback_failed" if failures else "rolled_back"
            record["rollbackFailures"] = failures
            try:
                _atomic_json(tx_path, record)
                if not failures:
                    self._release_targets(operations, tx)
                    self._clear_pending(item["id"], tx)
            except OSError: pass
            if failures:
                raise LibraryError(f"发布失败且部分回滚失败，请查看事务 {tx}：{', '.join(failures)}") from exc
            raise LibraryError(f"发布失败，已恢复本次写入：{exc}") from exc
        try:
            self._release_targets(operations, tx)
            self._clear_pending(item["id"], tx)
        except OSError: pass  # Applied state makes stale markers harmless.
        return {"ok": True, "transactionId": tx, "results": plan["results"],
                "partial": any(x["status"] == "conflict" for x in plan["results"])}

    def restore(self, transaction_id):
        if not isinstance(transaction_id, str) or not _ID.fullmatch(transaction_id):
            raise LibraryError("事务标识无效")
        with self._guard():
            path = self.root / "transactions" / f"{transaction_id}.json"
            _safe(path)
            try: record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc: raise LibraryError("事务记录无法读取") from exc
            state = self._read(); item = self._item(state, record["itemId"])
            pending = self._pending(item)
            if pending and pending != transaction_id:
                raise LibraryError(f"存在未完成的事务 {pending}，请先恢复")
            if record.get("id") != transaction_id or record.get("kind") != item["kind"]:
                raise LibraryError("事务标识或类型不匹配")
            def checked_operations(operations, phase):
                if not isinstance(operations, list):
                    raise LibraryError("事务记录不完整")
                for op in operations:
                    expected = self._path(item, op["agent"])
                    if str(expected) != op["path"] or any(op.get(k) != v for k, v in _op_paths(op, transaction_id, phase).items()):
                        raise LibraryError("事务目标或保护路径不匹配")
                return operations
            operations = checked_operations(record.get("operations"), "publish")
            with self._target_guards(operations):
                return self._restore_locked(transaction_id, path, record, state, item, operations, checked_operations)

    def _restore_locked(self, transaction_id, path, record, state, item, operations, checked_operations):
        status = record.get("status")
        prior_state = (item.get("headTransaction") == record.get("oldHeadTransaction") and
                       item.get("published") == record.get("oldPublished"))
        applied_state = (item.get("headTransaction") == transaction_id and item.get("published") == record.get("newPublished"))
        # Crash during a publish: metadata still describes the preceding
        # version. Reconcile each journaled native swap back to that state.
        if status in ("preparing", "rollback_failed", "applied") and prior_state and not applied_state:
            failures = []
            # A false journal flag was committed before any target change;
            # another Hub may have changed an unreserved target in the meantime.
            # Leave it alone, only release this transaction's markers.
            if record.get("targetsReserved") is not False:
                self._reserve_targets(operations, item["id"], transaction_id, allow_own=True)
                for op in reversed(operations):
                    try: _undo_native(op, item["kind"])
                    except (LibraryError, OSError) as exc: failures.append(str(exc))
            if failures:
                raise LibraryError("中断事务仍有冲突，原内容已保留：" + "；".join(failures))
            auto_paused = item.get("mode") == "auto"
            if auto_paused:
                item["mode"] = "manual"; self._save(state)
            record["status"] = "rolled_back"; record["recoveredAt"] = time.time(); _atomic_json(path, record)
            self._release_targets(operations, transaction_id)
            self._clear_pending(item["id"], transaction_id)
            return {"ok": True, "transactionId": transaction_id, "autoSyncPaused": auto_paused, "interrupted": True}
        # Crash during restore. If metadata was already committed, finish
        # the journal; otherwise undo only the partially restored writes.
        if status in ("restoring", "restore_failed"):
            if prior_state and not applied_state:
                record["status"] = "restored"; record["restoredAt"] = time.time(); _atomic_json(path, record)
                self._release_targets(operations, transaction_id)
                self._clear_pending(item["id"], transaction_id)
                return {"ok": True, "transactionId": transaction_id, "autoSyncPaused": item.get("mode") == "manual", "interrupted": True}
            if not applied_state:
                raise LibraryError("事务之后已有其他发布，不能恢复")
            phase = record.get("restorePhase")
            if not isinstance(phase, str) or not re.fullmatch(r"restore-[0-9a-f]{32}", phase):
                raise LibraryError("恢复事务记录不完整")
            restore_ops = checked_operations(record.get("restoreOperations"), phase)
            failures = []
            if record.get("restoreTargetsReserved") is not False:
                self._reserve_targets(operations, item["id"], transaction_id, allow_own=True)
                for op in reversed(restore_ops):
                    try: _undo_native(op, item["kind"])
                    except (LibraryError, OSError) as exc: failures.append(str(exc))
            if failures:
                raise LibraryError("中断恢复仍有冲突，现有内容已保留：" + "；".join(failures))
            record["status"] = "applied"; _atomic_json(path, record)
            self._release_targets(operations, transaction_id)
            self._clear_pending(item["id"], transaction_id)
            return {"ok": True, "transactionId": transaction_id, "autoSyncPaused": False,
                    "interrupted": True, "message": "已撤回中断的恢复；可重新选择恢复该发布。"}
        if status != "applied" or not applied_state:
            raise LibraryError("已有后续发布或该事务不能恢复")
        auto_paused = item.get("mode") == "auto"
        restore_ops, phase = [], "restore-" + uuid.uuid4().hex
        for op in reversed(operations):
            target = Path(op["path"])
            current = _native_snapshot(target, item["kind"])
            if item["kind"] == "instruction":
                try: desired = _instruction_restore(current, op["old"], op["new"], item["id"], op.get("retiredBefore"))
                except LibraryError as exc: raise LibraryError(f"目标在发布后被修改，不能覆盖：{op['agent']}") from exc
            else:
                if current != op["new"]:
                    raise LibraryError(f"目标在发布后被修改，不能覆盖：{op['agent']}")
                desired = op["old"]
            change = {"agent": op["agent"], "path": op["path"], "old": current, "new": desired}
            change.update(_op_paths(change, transaction_id, phase))
            restore_ops.append(change)
        record.update(status="restoring", restorePhase=phase, restoreOperations=restore_ops, restoreTargetsReserved=False)
        self._mark_pending(item["id"], transaction_id)
        _atomic_json(path, record)
        began = False
        try:
            self._reserve_targets(operations, item["id"], transaction_id, allow_own=True)
            record["restoreTargetsReserved"] = True
            _atomic_json(path, record)
            began = True
            for op in restore_ops:
                _swap_native(op, item["kind"])
            for op in operations:
                if op["prior"] is None: item["applied"].pop(op["agent"], None)
                else: item["applied"][op["agent"]] = op["prior"]
            item["published"] = record["oldPublished"]
            item["headTransaction"] = record.get("oldHeadTransaction")
            # Otherwise the unchanged latest main source would overwrite
            # the restored version again at the next auto-sync tick.
            if auto_paused:
                item["mode"] = "manual"
            self._save(state)
        except Exception as exc:
            failures = []
            if began:
                for op in reversed(restore_ops):
                    try: _undo_native(op, item["kind"])
                    except Exception: failures.append(op["agent"])
            record["status"] = "restore_failed" if failures else "applied"
            try:
                _atomic_json(path, record)
                if not failures:
                    self._release_targets(operations, transaction_id)
                    self._clear_pending(item["id"], transaction_id)
            except OSError: pass
            raise LibraryError(f"恢复失败；未能回滚的目标：{', '.join(failures) or '无'}") from exc
        record["status"] = "restored"; record["restoredAt"] = time.time()
        try: _atomic_json(path, record)
        except OSError:
            return {"ok": True, "transactionId": transaction_id, "autoSyncPaused": auto_paused,
                    "warning": "恢复已应用，但事务记录未更新；请在历史中恢复该中断事务。"}
        self._release_targets(operations, transaction_id)
        self._clear_pending(item["id"], transaction_id)
        return {"ok": True, "transactionId": transaction_id, "autoSyncPaused": auto_paused}

    def history(self, item_id):
        with _LOCK:
            item = self._item(self._read(), item_id)
            folder = self.root / "transactions"
            _safe(folder)
            rows = []
            if folder.is_dir():
                for path in folder.glob("*.json"):
                    try:
                        _safe(path)
                        row = json.loads(path.read_text(encoding="utf-8"))
                        if row.get("itemId") != item_id:
                            continue
                        prior = (item.get("headTransaction") == row.get("oldHeadTransaction") and
                                 item.get("published") == row.get("oldPublished"))
                        applied = (item.get("headTransaction") == row.get("id") and
                                   item.get("published") == row.get("newPublished"))
                        status = row.get("status")
                        interrupted = ((status in ("preparing", "rollback_failed", "applied") and prior and not applied)
                                       or (status in ("restoring", "restore_failed") and (prior or applied)))
                        can_restore = interrupted or (status == "applied" and applied)
                        if can_restore and not interrupted:
                            try:
                                for op in row.get("operations", []):
                                    target = self._path(item, op["agent"])
                                    if str(target) != op["path"]:
                                        raise LibraryError("事务目标不匹配")
                                    current = _native_snapshot(target, item["kind"])
                                    if item["kind"] == "skill" and current != op["new"]:
                                        raise LibraryError("目标已修改")
                                    if item["kind"] == "instruction":
                                        _instruction_restore(current, op["old"], op["new"], item["id"], op.get("retiredBefore"))
                            except (LibraryError, OSError, KeyError, TypeError):
                                can_restore = False
                        rows.append({"id": row["id"], "status": row.get("status"),
                                     "createdAt": row.get("createdAt"), "restoredAt": row.get("restoredAt"),
                                     "targets": [op["agent"] for op in row.get("operations", [])],
                                     "interrupted": interrupted, "canRestore": can_restore})
                    except (LibraryError, OSError, ValueError, KeyError, TypeError):
                        continue
            rows.sort(key=lambda row: row.get("createdAt") or 0, reverse=True)
            return {"transactions": rows}

    def sync_auto(self, item_ids=None, expected_hashes=None):
        if item_ids is not None and (not isinstance(item_ids, list) or any(not isinstance(x, str) or not _ID.fullmatch(x) for x in item_ids)):
            raise LibraryError("自动同步的能力项标识无效")
        if expected_hashes is not None and (not isinstance(expected_hashes, dict) or
                any(not isinstance(k, str) or not _ID.fullmatch(k) or not isinstance(v, str) or
                    not re.fullmatch(r"[a-f0-9]{64}", v) for k, v in expected_hashes.items())):
            raise LibraryError("自动同步的来源哈希无效")
        results = []
        for item in self.list_items()["items"]:
            if item["mode"] != "auto" or (item_ids is not None and item["id"] not in item_ids):
                continue
            # Automatic sync applies only after the user has reviewed and
            # explicitly published the first version. Merely registering an
            # item with auto mode must never write to client files.
            if not item.get("published"):
                results.append({"itemId": item["id"], "ok": False,
                                "error": "首次发布需要先预览并由用户应用"})
                continue
            try:
                if expected_hashes is not None and item.get("sourceHash") != expected_hashes.get(item["id"]):
                    results.append({"itemId": item["id"], "ok": False, "error": "主来源已改变，等待稳定后同步"})
                    continue
                # New targets receive the published version when the main source
                # has not changed; pending conflicts are retried on each scan.
                plan = self.preview(item["id"])
                if self._plans.get(plan["planId"], {}).get("item", {}).get("mode") != "auto":
                    self._plans.pop(plan["planId"], None)
                    results.append({"itemId": item["id"], "ok": False, "error": "自动同步已暂停"})
                    continue
                if expected_hashes is not None and plan["sourceHash"] != expected_hashes.get(item["id"]):
                    self._plans.pop(plan["planId"], None)
                    results.append({"itemId": item["id"], "ok": False, "error": "主来源已改变，等待稳定后同步"})
                    continue
                unsafe = set(plan.get("autoUnsafeTargets") or [])
                if unsafe:
                    for row in plan["results"]:
                        if row["agent"] in unsafe and row["status"] != "source":
                            row["status"] = "conflict"
                            row["detail"] = "替换未匹配或客户端标识不一致；自动同步已暂停，请查看预览"
                    self._plans[plan["planId"]]["operations"] = [
                        op for op in self._plans[plan["planId"]]["operations"] if op["agent"] not in unsafe]
                if (item.get("published") == plan["sourceHash"] and
                    all(x["status"] in ("unchanged", "source") for x in plan["results"])):
                    self._plans.pop(plan["planId"], None)
                    continue
                if (item.get("published") == plan["sourceHash"] and
                    not any(x["status"] in ("create", "update") for x in plan["results"])):
                    self._plans.pop(plan["planId"], None)
                    results.append({"itemId": item["id"], "ok": False,
                                    "error": "目标存在冲突，自动同步已暂停", "results": plan["results"]})
                    continue
                result = self.apply(plan["planId"], skip_conflicts=True, require_auto=True)
                results.append({"itemId": item["id"], **result})
            except (LibraryError, OSError) as exc:
                results.append({"itemId": item["id"], "ok": False, "error": str(exc)})
        return {"results": results}


# A shorter name is convenient for server integration.
Library = CapabilityLibrary
