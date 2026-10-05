"""Preview and reversible native-config writes, without touching live auth stores.

Adapters run in a separate Python process with a private HOME. Only the explicit
files in TARGET_FILES cross that boundary; configured ``files`` patterns are not
trusted as an allowlist. Plans contain secret-bearing bytes only in bounded,
short-lived process memory. Public previews never expose those bytes.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TARGET_FILES = {
    "pi": (".pi/agent/models.json", ".pi/agent/auth.json", ".pi/agent/settings.json"),
    "mcode": (".minimax/config.yaml",),
    "zcode": (".zcode/v2/provider_config.json",),
    "dsh": (".dsh/.credentials.yaml", ".dsh/settings.yaml.imported",
            ".dsh/profiles/desktop/cordis.patch.yml", ".dsh/settings.yaml"),
    "claude": (".claude/settings.json",),
    "codex": (".codex/config.toml", ".codex/.agent-hub-state.json"),
    "omp": (".omp/agent/config.yml",),
}
ALIASES = {"dsh-desktop": "dsh"}
MAX_PLANS = 32
PLAN_TTL = 15 * 60
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_CACHE_BYTES = 64 * 1024 * 1024
_LOCK = threading.RLock()
_PLANS: OrderedDict[str, Any] = OrderedDict()


class TransactionError(ValueError):
    """A rejected preview/apply/restore; messages never contain config contents."""


@dataclass
class Snapshot:
    data: bytes | None
    mode: int | None
    fingerprint: tuple


@dataclass
class Plan:
    created: float
    home: Path
    config: Path
    backups: Path
    engine: Path
    config_hash: str
    targets: list[str]
    sources: dict[Path, Snapshot]
    outputs: dict[Path, bytes | None]
    warnings: list[str]


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _json_hash(value: Any) -> str:
    try:
        data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TransactionError("Hub configuration is not valid JSON") from exc
    return hashlib.sha256(data).hexdigest()


def _module(hub):
    module = sys.modules.get(type(hub).__module__)
    if module is None or not all(hasattr(module, name) for name in ("HOME", "HUB_CONFIG", "HUB_BACKUP")):
        raise TransactionError("Cannot determine the Hub configuration paths")
    return module


def _context(hub) -> tuple[Path, Path, Path, Path]:
    module = _module(hub)
    return tuple(Path(path).absolute() for path in
                 (module.HOME, module.HUB_CONFIG, module.HUB_BACKUP, module.__file__))


def _safe_path(path: Path) -> None:
    """Never follow an adapter/config/backup symlink, including parent symlinks."""
    for node in (path, *path.parents):
        if node.is_symlink():
            raise TransactionError(f"Refusing symlink path: {path}")
        if node != path and node.exists() and not node.is_dir():
            raise TransactionError(f"Parent is not a directory: {path}")


def _effective_mode(requested: int | None) -> int | None:
    """写入完成后这个文件实际会有的 mode。

    POSIX 上 chmod 如实生效；Windows 上只有只读位可表达，0o600 这类
    "仅所有者可写" 会被压成 0o666，所以清单里不能记录请求值，否则
    restore 拿"记录值"和"真实值"一比就永远判定配置被外部改动过。
    """
    if requested is None:
        return None
    if os.name != "nt":
        return requested
    return 0o444 if requested & 0o200 == 0 else 0o666


def _mode_matches(current: int | None, recorded: int | None) -> bool:
    """按平台语义比较 mode：Windows 只比只读位，POSIX 严格相等。"""
    if os.name != "nt":
        return current == recorded
    if (current is None) != (recorded is None):
        return False
    if current is None:
        return True
    return bool(current & 0o200) == bool(recorded & 0o200)


def _snapshot(path: Path) -> Snapshot:
    _safe_path(path)
    try:
        before = path.stat()
    except FileNotFoundError:
        return Snapshot(None, None, (False,))
    if not stat.S_ISREG(before.st_mode):
        raise TransactionError(f"Expected a regular configuration file: {path}")
    if before.st_size > MAX_FILE_BYTES:
        raise TransactionError(f"Configuration exceeds the safe size limit: {path}")
    data = path.read_bytes()
    after = path.stat()
    before_stat = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode)
    after_stat = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode)
    if before_stat != after_stat:
        raise TransactionError(f"Configuration changed while being read: {path}")
    return Snapshot(data, stat.S_IMODE(after.st_mode), (True, *after_stat, _digest(data)))


def _select(cfg: dict, only: list[str] | None) -> list[str]:
    targets = (cfg.get("sync") or {}).get("targets") or {}
    if not isinstance(targets, dict):
        raise TransactionError("sync.targets must be an object")
    if only is not None and (not isinstance(only, list) or any(not isinstance(x, str) for x in only)):
        raise TransactionError("targets must be an array of target names")
    selected = only if only is not None else [name for name, entry in targets.items()
                                            if isinstance(entry, dict) and entry.get("enabled") is True]
    canonical = []
    for name in selected:
        entry = targets.get(name)
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            raise TransactionError(f"Target is not explicitly enabled: {name}")
        real = ALIASES.get(name, name)
        if entry.get("aliasOf") not in (None, ALIASES.get(name)):
            raise TransactionError(f"Target has an unsupported aliasOf: {name}")
        if name in ALIASES:
            canonical_entry = targets.get(real)
            if not isinstance(canonical_entry, dict) or canonical_entry.get("enabled") is not True:
                raise TransactionError(f"Alias requires its canonical target to be explicitly enabled: {name} -> {real}")
        if real == "cc-switch":
            raise TransactionError("cc-switch is not supported by safe file transactions: its live SQLite database "
                                   "cannot be atomically replaced. Deselect cc-switch before previewing.")
        if real not in TARGET_FILES:
            raise TransactionError(f"Unsupported native target: {name}")
        if real not in canonical:
            canonical.append(real)
    if not canonical:
        raise TransactionError("Select at least one explicitly enabled native target")
    return canonical


def _validate_config(cfg: Any) -> None:
    if not isinstance(cfg, dict) or not isinstance(cfg.get("providers"), dict):
        raise TransactionError("Hub providers must be an object")
    if not isinstance(cfg.get("sync", {}), dict):
        raise TransactionError("Hub sync must be an object")
    for name, provider in cfg["providers"].items():
        if not isinstance(name, str) or not isinstance(provider, dict):
            raise TransactionError("Each Hub provider must be a named object")
        models = provider.get("models", [])
        if not isinstance(models, list) or any(not isinstance(model, dict) or
                                             not isinstance(model.get("id"), str) or
                                             not model["id"].strip() for model in models):
            raise TransactionError(f"Provider {name} has an invalid model list")


def _yaml_subset(text: str) -> Any:
    """Strict, deliberately small YAML subset, independently checked against Hub.

    Reject anchors, tags, block strings, ambiguous scalars, duplicate keys and
    unconsumed indentation instead of letting the legacy parser discard them.
    No optional YAML package is required or imported.
    """
    def split_flow(value):
        parts, start, depth, quote = [], 0, 0, None
        for index, char in enumerate(value):
            if quote:
                if char == "\\":
                    raise ValueError("Escaped YAML strings are unsupported")
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
                if depth < 0:
                    raise ValueError("Invalid flow collection")
            elif char == "," and depth == 0:
                parts.append(value[start:index].strip())
                start = index + 1
        if depth or quote:
            raise ValueError("Unclosed flow collection")
        parts.append(value[start:].strip())
        return parts

    escapes = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r",
               "b": "\b", "f": "\f", "0": "\0", "a": "\a", "v": "\v", "e": "\x1b", " ": " "}

    def unquote(raw: str) -> str:
        """去掉引号并按需反转义，语义与 hub.yaml_unquote 完全一致。

        双引号走 JSON 风格转义（写入端 json.dumps 的产物）；单引号在 YAML 里
        没有转义，内容原样（Windows 路径 'C:\\Program Files\\...' 因此可读）。
        """
        if len(raw) < 2 or raw[-1] != raw[0] or raw[0] not in "\"'":
            raise ValueError("Unsupported quoted YAML scalar")
        body = raw[1:-1]
        if raw[0] == "'":
            if "'" in body:
                raise ValueError("Unsupported quoted YAML scalar")
            return body
        out, index = [], 0
        while index < len(body):
            char = body[index]
            if char == '"':
                raise ValueError("Unsupported quoted YAML scalar")
            if char == "\\":
                index += 1
                if index >= len(body):
                    raise ValueError("Unsupported quoted YAML scalar")
                esc = body[index]
                if esc == "u":
                    digits = body[index + 1:index + 5]
                    if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                        raise ValueError("Unsupported quoted YAML scalar")
                    out.append(chr(int(digits, 16)))
                    index += 5
                    continue
                if esc not in escapes:
                    raise ValueError("Unsupported quoted YAML scalar")
                out.append(escapes[esc])
                index += 1
                continue
            out.append(char)
            index += 1
        return "".join(out)

    # 键名：带引号的键（'...' / "..."，无转义）或裸键。裸键允许模型 ID 这类
    # 含 [ ] / 的标识，也允许 off / on / true 这类词（工具自己的适配器就把
    # reasoningEfforts 的 "off" 写成键；键一律按字符串处理，与宽松解析器一致）。
    # 含 ": "、以指示符开头的键必须加引号，否则不同解析器会读出不同类型。
    bare_key = re.compile(r"[^\s:#]+\Z")
    key_indicators = "-?:,[]{}&*!|>'\"%@`"

    def mapping_pair(value):
        if value[0] in "\"'":
            quote = value[0]
            # 双引号键用的是写入端 json.dumps 的转义：扫结束引号时必须跳过
            # `\"` / `\\`，否则键 `\` 会被读成结束引号，键和值一起读坏。
            end = -1
            index = 1
            while index < len(value):
                char = value[index]
                if quote == '"' and char == "\\":
                    index += 2
                    continue
                if char == quote:
                    end = index
                    break
                index += 1
            if end < 1:
                raise ValueError("Unsupported quoted YAML key")
            if quote == "'":
                if "'" in value[1:end] or "\\" in value[1:end]:
                    raise ValueError("Unsupported quoted YAML key")
                key = value[1:end]
            else:
                key = unquote(value[:end + 1])
            rest = value[end + 1:]
            if not rest.startswith(":") or (len(rest) > 1 and rest[1] not in " \t"):
                raise ValueError("Unsupported YAML mapping key")
            return key, rest[2:].strip() if len(rest) > 1 else ""
        match = re.fullmatch(r"([^\s:#]+):(?:\s+(.*))?", value)
        if not match:
            raise ValueError("Unsupported YAML mapping key")
        key = match.group(1)
        if key[0] in key_indicators:
            raise ValueError("Unsupported YAML mapping key")
        return key, match.group(2) or ""

    def list_item_is_mapping(content: str) -> bool:
        """`- ` 后面到底是「映射的第一个键」还是普通列表项。

        先走原来那条严格的裸键快路径，保持既有判定完全不变；只有它不匹配时才
        用 mapping_pair 试一次，用来认出带引号的键（`- "a:b": {k: v}`）。
        """
        if re.match(r"[\w./-]+:(?:\s|$)", content):
            return True
        if content[:1] not in "\"'":
            return False
        try:
            mapping_pair(content)
        except ValueError:
            return False
        return True

    # 纯数字 / 浮点 / 进制 / 六十进制前缀的裸标量必须显式加引号：不加引号时
    # YAML 解析器会把它读成数值，静默改变类型。以数字开头但解析不成数值的
    # （例如 UUID 形态的密钥 "3fa85f64-..." ）按规范就是字符串，可以裸写。
    int_like = re.compile(r"[-+]?(0b[0-1_]+|0x[0-9a-fA-F_]+|0o[0-7_]+|[0-9][0-9_]*)\Z")
    float_like = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?\Z")
    sexagesimal = re.compile(r"[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+\Z")

    def scalar(value):
        if not value or value in ("null", "Null", "NULL", "~"):
            return None
        if value.startswith(("[", "{")):
            closing = "]" if value[0] == "[" else "}"
            if not value.endswith(closing):
                raise ValueError("Unclosed flow collection")
            contents = value[1:-1].strip()
            items = split_flow(contents) if contents else []
            if value[0] == "[":
                return [scalar(item) for item in items]
            result = {}
            for item in items:
                key, val = mapping_pair(item)
                if key in result:
                    raise ValueError("Duplicate YAML key")
                result[key] = scalar(val)
            return result
        if value[0] in "\"'":
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Unsupported quoted YAML scalar")
            text = unquote(value)
            # 写入端把「裸写会被本校验器拒绝」的数字形态（负数、科学计数法、
            # inf / nan）加引号写出，这里必须用同一套规则还原类型，否则
            # parse_yaml 与 _yaml_subset 会分别得到 -1 和 "-1"，校验必然失败。
            native = sys.modules.get("hub")
            restore = getattr(native, "_repr_quoted_number", None) if native is not None else None
            coerce = getattr(native, "_numeric", None) if native is not None else None
            if restore is not None and coerce is not None and restore(text):
                return coerce(text)
            return text
        if any(c in value for c in "[]{}&#*!|>'\"%@`") or re.search(r":(?:\s|$)", value):
            raise ValueError("Unsupported YAML scalar")
        if value.lower() in ("true", "false"):
            return value.lower() == "true"
        if value[0] in "-+?." or value.lower() in ("yes", "no", "on", "off"):
            raise ValueError("Ambiguous YAML scalar")
        if value[0].isdigit():
            # 数字开头：整数 / 小数按数值读取；进制、科学计数法、六十进制这类
            # 形态必须显式加引号；其余（例如 UUID 形态的密钥 "3fa85f64-..."）
            # 按 YAML 规范就是字符串，可以裸写。
            if re.fullmatch(r"0|[1-9]\d*", value):
                return int(value)
            if re.fullmatch(r"(0|[1-9]\d*)\.\d+", value):
                return float(value)
            if int_like.fullmatch(value) or float_like.fullmatch(value) or sexagesimal.fullmatch(value):
                raise ValueError("Ambiguous YAML scalar")
            return value
        if re.fullmatch(r"-?(?:0|[1-9]\d*)", value):
            return int(value)
        if re.fullmatch(r"-?(?:0|[1-9]\d*)\.\d+", value):
            return float(value)
        return value

    tokens = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line or line.rstrip() != line:
            raise ValueError("Unsupported YAML whitespace")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            raise ValueError("YAML must use two-space indentation")
        body = line[indent:]
        if body.startswith("- ") and list_item_is_mapping(body[2:]):
            tokens.extend(((indent, "-"), (indent + 2, body[2:])))
        else:
            tokens.append((indent, body))
    if not tokens:
        return None

    def block(index, indent):
        is_list = tokens[index][1] == "-" or tokens[index][1].startswith("- ")
        result = [] if is_list else {}
        while index < len(tokens) and tokens[index][0] == indent:
            body = tokens[index][1]
            if is_list:
                if body != "-" and not body.startswith("- "):
                    raise ValueError("Mixed YAML collection")
                key, value = None, body[2:]
            else:
                key, value = mapping_pair(body)
                if key in result:
                    raise ValueError("Duplicate YAML key")
            index += 1
            if not value and index < len(tokens) and tokens[index][0] > indent:
                if tokens[index][0] != indent + 2:
                    raise ValueError("Unexpected YAML indentation")
                parsed, index = block(index, indent + 2)
            else:
                parsed = scalar(value)
            if is_list:
                result.append(parsed)
            else:
                result[key] = parsed
        return result, index

    if len(tokens) == 1 and tokens[0][1] in ("{}", "[]"):
        return scalar(tokens[0][1])
    if tokens[0][0] != 0:
        raise ValueError("Unexpected YAML indentation")
    result, end = block(0, 0)
    if end != len(tokens):
        raise ValueError("Unconsumed YAML content")
    return result


def _validate_file(path: Path, data: bytes | None, module=None, *, output=False) -> None:
    if data is None:
        return
    try:
        text = data.decode("utf-8")
        if path.suffix == ".toml":
            try:
                import tomllib
            except ImportError as exc:
                raise TransactionError("Safe TOML validation requires Python 3.11 or newer") from exc
            parsed = tomllib.loads(text)
        elif path.suffix == ".json":
            if not output:
                if module is None:
                    module = sys.modules.get("hub")
                if module is not None:
                    text = module.strip_json_comments(text)
            parsed = json.loads(text)
        else:
            try:
                parsed = _yaml_subset(text)
            except ValueError as exc:
                raise TransactionError(f"Unsupported or invalid YAML: {path}. Safe sync accepts only simple "
                                       "maps, lists and scalars; advanced YAML must be simplified first.") from exc
            if module is None:
                module = sys.modules.get("hub")
            if module is None or module.parse_yaml(text) != parsed:
                raise TransactionError(f"Unsupported YAML syntax for the native adapter: {path}")
            rendered = module.dump_yaml(parsed) if parsed is not None else ""
            if _yaml_subset(rendered) != parsed:
                raise TransactionError(f"YAML cannot be round-tripped safely: {path}")
        expected = list if path.name == "cordis.patch.yml" else dict
        if not isinstance(parsed, expected):
            # An empty YAML document is accepted as an empty native mapping.
            if parsed is not None or expected is list or text.strip():
                raise ValueError("Unexpected document type")
    except TransactionError:
        raise
    except Exception as exc:
        raise TransactionError(f"Invalid {'generated ' if output else ''}configuration: {path}") from exc


def _prune_plans() -> None:
    now = time.monotonic()
    for key in list(_PLANS):
        if now - _PLANS[key].created > PLAN_TTL:
            del _PLANS[key]
    def size(plan):
        return sum(len(s.data or b"") for s in plan.sources.values()) + sum(len(d or b"") for d in plan.outputs.values())
    while _PLANS and (len(_PLANS) > MAX_PLANS or sum(size(p) for p in _PLANS.values()) > MAX_CACHE_BYTES):
        _PLANS.popitem(last=False)


def _compile(engine: Path, root: Path, targets: list[str]) -> None:
    # Keep API tokens and other user environment credentials out of the child.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP")
           if key in os.environ}
    env.update(HOME=str(root / "home"), USERPROFILE=str(root / "home"),
               CODEX_HOME=str(root / "home" / ".codex"), PYTHONUTF8="1")
    try:
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).absolute()), "--compile",
                                 str(engine), str(root), json.dumps(targets)],
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransactionError("Isolated native configuration compilation did not finish") from exc
    try:
        report = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise TransactionError("Isolated native configuration compilation failed") from exc
    if result.returncode or not report.get("ok"):
        raise TransactionError(report.get("error") or "Isolated native configuration compilation failed")


def preview(hub, only: list[str] | None = None) -> dict:
    cfg = copy.deepcopy(hub.cfg)
    _validate_config(cfg)
    targets = _select(cfg, only)
    home, config, backups, engine = _context(hub)
    relative_files = sorted({rel for name in targets for rel in TARGET_FILES[name]})
    sources = {path: _snapshot(path) for path in
               (config, engine, engine.with_name("codex_config.py"), Path(__file__).absolute())}
    # A long-lived Hub instance must not compile an obsolete in-memory config.
    try:
        disk_cfg = json.loads(_module(hub).strip_json_comments(sources[config].data.decode("utf-8")))
    except (AttributeError, UnicodeError, ValueError) as exc:
        raise TransactionError("The saved Hub configuration is invalid; reload it before previewing") from exc
    if _json_hash(disk_cfg) != _json_hash(cfg):
        raise TransactionError("Hub configuration is stale; reload it before previewing")
    for rel in relative_files:
        path = home / rel
        sources[path] = _snapshot(path)
        _validate_file(path, sources[path].data, _module(hub))
    outputs = {}
    warnings = []
    if "claude" in targets:
        warnings.append("Claude sync covers settings.json only; cc-switch is a separate, unsupported database target.")
    with tempfile.TemporaryDirectory(prefix="agent-hub-preview-") as directory:
        root = Path(directory)
        (root / "home").mkdir()
        (root / "hub").mkdir()
        (root / "hub" / "providers.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        for rel in relative_files:
            dest = root / "home" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            original = sources[home / rel].data
            if original is not None:
                dest.write_bytes(original)
                dest.chmod(0o600)
        _compile(engine, root, targets)
        allowed = set(relative_files)
        # This walk is confined to newly-created scratch files, never the user's HOME.
        for path in (root / "home").rglob("*"):
            if path.is_symlink() or (path.is_file() and path.relative_to(root / "home").as_posix() not in allowed):
                raise TransactionError("Adapter produced an unexpected native file; no changes were applied")
        for rel in relative_files:
            staged = _snapshot(root / "home" / rel)
            _validate_file(home / rel, staged.data, _module(hub), output=True)
            if staged.data != sources[home / rel].data:
                outputs[home / rel] = staged.data
        # All selected adapters compile successfully before a plan becomes applicable.
    for name in targets:
        if not any(home / rel in outputs for rel in TARGET_FILES[name]):
            warnings.append(f"{name}: no native file changes are needed")
    plan_id = uuid.uuid4().hex
    plan = Plan(time.monotonic(), home, config, backups, engine, _json_hash(cfg), targets,
                sources, outputs, warnings)
    with _LOCK:
        _PLANS[plan_id] = plan
        _prune_plans()
        if plan_id not in _PLANS:
            raise TransactionError("Preview exceeds the safe in-memory size limit")
    return {"planId": plan_id, "targets": list(targets), "warnings": list(warnings),
            "files": [{"path": str(path), "action": "delete" if data is None else
                       ("create" if sources[path].data is None else "update"),
                       "targets": [name for name in targets if path.relative_to(home).as_posix() in TARGET_FILES[name]]}
                      for path, data in outputs.items()]}


def _ensure_directory(path: Path, created: list[Path] | None = None) -> None:
    _safe_path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        if created is not None:
            created.append(directory)


def _atomic_write(path: Path, data: bytes | None, mode: int | None = None) -> None:
    _safe_path(path)
    if data is None:
        path.unlink(missing_ok=True)
        return
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.agent-hub-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(_effective_mode(mode) if mode is not None else 0o600)
        os.replace(temp_path, path)
        # Directory fsync is unavailable on Windows; data files are still fsynced.
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_manifest(path: Path, manifest: dict) -> None:
    _atomic_write(path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o600)


@contextmanager
def _disk_lock(backups: Path):
    root = backups / "transactions"
    _ensure_directory(root)
    lock_path = root / ".write-lock"
    _safe_path(lock_path)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise TransactionError("Another native transaction is running (or its lock needs manual recovery)") from exc
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(str(os.getpid()))
        yield root
    finally:
        lock_path.unlink(missing_ok=True)


def _assert_sources(sources: dict[Path, Snapshot]) -> None:
    for path, snapshot in sources.items():
        if _snapshot(path).fingerprint != snapshot.fingerprint:
            raise TransactionError(f"Preview is stale because a source changed: {path}. Preview again.")


def _commit(home: Path, root: Path, targets: list[str], sources: dict[Path, Snapshot],
            outputs: dict[Path, bytes | None], *, restored_from: str | None = None) -> dict:
    _assert_sources(sources)
    transaction_id = uuid.uuid4().hex
    folder = root / transaction_id
    folder.mkdir(mode=0o700)
    manifest = {"version": 1, "transactionId": transaction_id, "home": str(home),
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "targets": targets, "status": "prepared", "files": []}
    if restored_from:
        manifest["restoredFrom"] = restored_from
    for index, (path, data) in enumerate(outputs.items()):
        before = sources[path]
        filename = f"{index:04d}.before" if before.data is not None else None
        if filename:
            _atomic_write(folder / filename, before.data, 0o600)
        manifest["files"].append({"path": path.relative_to(home).as_posix(), "backup": filename,
                                  "beforeHash": _digest(before.data), "afterHash": _digest(data),
                                  "beforeMode": before.mode,
                                  "afterMode": _effective_mode(
                                      before.mode if before.mode is not None else 0o600)
                                  if data is not None else None})
    manifest_path = folder / "manifest.json"
    _write_manifest(manifest_path, manifest)
    _assert_sources(sources)
    applied = []
    created_dirs = []
    try:
        manifest["status"] = "committing"
        _write_manifest(manifest_path, manifest)
        for path, data in outputs.items():
            if _snapshot(path).fingerprint != sources[path].fingerprint:
                raise TransactionError(f"Native configuration changed during apply: {path}")
            _ensure_directory(path.parent, created_dirs)
            # Record before invoking replace, so a post-replace fsync error is rolled back too.
            applied.append(path)
            _atomic_write(path, data, sources[path].mode)
        manifest["status"] = "committed"
        _write_manifest(manifest_path, manifest)
    except Exception as exc:
        failures = []
        for path in reversed(applied):
            before = sources[path]
            try:
                current = _snapshot(path)
                if current.data == before.data:
                    continue
                if current.data != outputs[path]:
                    raise TransactionError("Concurrent modification during rollback")
                _atomic_write(path, before.data, before.mode)
            except Exception:
                failures.append(str(path))
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        manifest["status"] = "rollback_failed" if failures else "rolled_back"
        manifest["rollbackFailures"] = failures
        try:
            _write_manifest(manifest_path, manifest)
        except Exception:
            pass
        if failures:
            raise TransactionError(f"Apply failed and rollback needs manual recovery; transaction {transaction_id}. "
                                   f"Unrestored files: {', '.join(failures)}") from exc
        raise TransactionError(f"Apply failed; all applied native changes were rolled back. Transaction {transaction_id}") from exc
    suffix = " (restored)" if restored_from else ""
    return {"transactionId": transaction_id, "results": [f"  ✓ {name}{suffix}" for name in targets]}


def apply(hub, plan_id: str) -> dict:
    with _LOCK:
        _prune_plans()
        plan = _PLANS.get(plan_id) if isinstance(plan_id, str) else None
        if plan is None:
            raise TransactionError("Preview is missing or expired; preview again before applying")
        if _context(hub) != (plan.home, plan.config, plan.backups, plan.engine) or _json_hash(hub.cfg) != plan.config_hash:
            raise TransactionError("Preview is stale because the Hub configuration changed; preview again")
        _assert_sources(plan.sources)
        for path, data in plan.outputs.items():
            _validate_file(path, data, _module(hub), output=True)
        with _disk_lock(plan.backups) as root:
            _PLANS.pop(plan_id, None)
            return _commit(plan.home, root, plan.targets, plan.sources, plan.outputs)


def restore(hub, transaction_id: str) -> dict:
    if not isinstance(transaction_id, str) or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
        raise TransactionError("Invalid transaction ID")
    home, _config, backups, _engine = _context(hub)
    with _LOCK, _disk_lock(backups) as root:
        folder = root / transaction_id
        manifest_path = folder / "manifest.json"
        snapshot = _snapshot(manifest_path)
        if snapshot.data is None:
            raise TransactionError("Transaction was not found")
        try:
            manifest = json.loads(snapshot.data)
        except (ValueError, TypeError) as exc:
            raise TransactionError("Transaction manifest is invalid") from exc
        if (manifest.get("version") != 1 or manifest.get("transactionId") != transaction_id or
                manifest.get("home") != str(home) or manifest.get("status") != "committed"):
            raise TransactionError("Only a committed transaction from this HOME can be restored")
        targets = manifest.get("targets")
        if (not isinstance(targets, list) or not targets or
                any(not isinstance(name, str) or name not in TARGET_FILES for name in targets)):
            raise TransactionError("Transaction has invalid native targets")
        allowed = {rel for name in targets for rel in TARGET_FILES[name]}
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) > len(allowed):
            raise TransactionError("Transaction file manifest is invalid")
        sources, outputs = {}, {}
        for entry in files:
            if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or
                    entry["path"] not in allowed):
                raise TransactionError("Transaction contains a non-allowlisted native file")
            path = home / entry["path"]
            if path in sources:
                raise TransactionError("Transaction contains a duplicate native file")
            current = _snapshot(path)
            if (_digest(current.data) != entry.get("afterHash")
                    or not _mode_matches(current.mode, entry.get("afterMode"))):
                raise TransactionError(f"Cannot restore because native configuration changed after apply: {path}")
            filename = entry.get("backup")
            if filename is not None and (not isinstance(filename, str) or re.fullmatch(r"\d{4}\.before", filename) is None):
                raise TransactionError("Transaction backup path is invalid")
            data = _snapshot(folder / filename).data if filename else None
            if (filename is not None and data is None) or _digest(data) != entry.get("beforeHash"):
                raise TransactionError("Transaction backup is missing or its integrity check failed")
            _validate_file(path, data, _module(hub))
            sources[path] = current
            outputs[path] = data
        result = _commit(home, root, targets, sources, outputs, restored_from=transaction_id)
        result["restored"] = transaction_id
        return result


def _worker(engine: Path, root: Path, targets: list[str]) -> int:
    """Child-only global path patching; never imported/executed by the web process."""
    # Defense in depth: existing adapters do not use the network or execute tools.
    def guard(event, args):
        if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system"}:
            raise PermissionError("Native compilation cannot access the network or execute programs")
        if event == "open":
            path, mode, flags = args
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and isinstance(path, (str, bytes, os.PathLike)):
                candidate = Path(os.fsdecode(path)).absolute()
                if not candidate.is_relative_to(root):
                    raise PermissionError("Native compilation cannot write outside scratch space")
        for index in {"os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,),
                      "os.rename": (0, 1), "os.chmod": (0,), "os.link": (0, 1),
                      "os.symlink": (0, 1)}.get(event, ()):
            value = args[index]
            if isinstance(value, (str, bytes, os.PathLike)) and not Path(os.fsdecode(value)).absolute().is_relative_to(root):
                raise PermissionError("Native compilation cannot change files outside scratch space")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(engine.parent))
    sys.addaudithook(guard)
    spec = importlib.util.spec_from_file_location("_agent_hub_compile", engine)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.HOME = root / "home"
    module.HUB_DIR = root / "hub"
    module.HUB_CONFIG = root / "hub" / "providers.json"
    module.HUB_STATE = root / "hub" / "state.json"
    module.HUB_BACKUP = root / "backups"
    hub = module.Hub()
    target = "configuration"
    try:
        for target in targets:
            getattr(hub, "push_" + target.replace("-", "_"))()
    except Exception as exc:
        message = str(exc)
        def redact(value):
            nonlocal message
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, str) and item and re.search(r"api.?key|token|password|secret", str(key), re.I):
                        message = message.replace(item, "[redacted]")
                    else:
                        redact(item)
            elif isinstance(value, list):
                for item in value:
                    redact(item)
        redact(hub.cfg)
        print(json.dumps({"ok": False, "error": f"Cannot compile {target}: {type(exc).__name__}: {message[:400]}"}))
        return 1
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "--compile":
    sys.exit(_worker(Path(sys.argv[2]), Path(sys.argv[3]), json.loads(sys.argv[4])))
