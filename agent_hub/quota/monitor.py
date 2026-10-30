"""Read-only, account-scoped subscription quota monitoring.

The service knows neither how a provider authenticates nor how a desktop sends
notifications.  Those operations are injected, allowing isolated tests and
ensuring it never changes client configuration or switches a model.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path


DEFAULTS = {"enabled": True, "intervalSeconds": 900, "thresholdRemainingPercent": 20}
PROVIDERS = ("codex-chatgpt", "zcode-subscription")
_NAMES = {"codex-chatgpt": "Codex", "zcode-subscription": "ZCode", "codex-native": "Codex 原生线路"}
_MAX_LEDGER = 512


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else copy.deepcopy(default)
    except (OSError, ValueError, UnicodeError):
        return copy.deepcopy(default)


def _validate_settings(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("额度提醒设置不受支持")
    result = {**DEFAULTS, **value}
    if type(result["enabled"]) is not bool:
        raise ValueError("额度提醒开关必须为布尔值")
    if type(result["intervalSeconds"]) is not int or not 60 <= result["intervalSeconds"] <= 86400:
        raise ValueError("查询间隔须为 60 至 86400 秒")
    threshold = result["thresholdRemainingPercent"]
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise ValueError("剩余额度提醒阈值须为 0 至 100")
    return result


def _safe_number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _safe_text(value, limit=120):
    # Provider supplied labels are display-only. The upstream API adapters
    # already strip credentials; reject control characters and bound the size.
    return value[:limit] if isinstance(value, str) and not any(ord(ch) < 32 for ch in value) else None


def _snapshot(raw, clock):
    if isinstance(raw, dict) and "quota" in raw:
        raw = raw["quota"]
    if not isinstance(raw, dict) or not isinstance(raw.get("windows"), list):
        raise ValueError("额度响应缺少窗口数据")
    rows = []
    for row in raw["windows"][:100]:
        if not isinstance(row, dict):
            continue
        rows.append({
            "name": _safe_text(row.get("name")) or "额度窗口",
            **{key: _safe_number(row.get(key)) for key in
               ("usedPercent", "windowSeconds", "resetsAt", "expiresAt", "used", "total")},
        })
    checked_at = _safe_number(raw.get("checkedAt"))
    return {"source": _safe_text(raw.get("source")) or "上游额度接口",
            "plan": _safe_text(raw.get("plan")), "checkedAt": checked_at if checked_at is not None else clock(),
            "windows": rows}


def classify_error(*, status=None, code=None, error=None):
    """Return a sanitized kind/message; never put upstream error text on disk.

    An HTTP 429 alone is ambiguous. Structured upstream codes can distinguish
    quota exhaustion from transient request-rate limiting when available.
    """
    code = str(code or "").lower()
    if code in {"insufficient_quota", "quota_exceeded", "quota_exhausted", "billing_hard_limit_reached"}:
        return "quota", "上游明确报告额度不足，请查看额度或套餐"
    if code in {"rate_limit_exceeded", "rate_limit", "too_many_requests"}:
        return "rate_limit", "上游报告请求过于频繁，请稍后重试"
    if status == 429:
        return "limited", "上游报告额度或请求速率受限，请查看额度后重试"
    if status == 401 or error == "login":
        return "login", "登录已失效或不可用，请在原应用检查登录状态"
    if status == 403:
        return "permission", "上游拒绝访问，请检查账号权限和套餐"
    if error == "network" or status in (408, 502, 503, 504):
        return "network", "网络或上游暂时不可用，请稍后重试"
    return "unknown", "额度查询或请求失败，请检查账号状态后重试"


class QuotaMonitor:
    """Background quota monitor; all account identities are HMACed in memory.

    ``queries`` functions return the current adapter's quota or {quota: ...}.
    ``identities`` functions return a stable JSON-serializable account/plan
    identity or None. ``connected`` functions return booleans. ``notify`` takes
    one public event dict. None of these functions is called while holding the
    monitor lock, and no raw identity, token, or error is persisted or returned.
    """

    def __init__(self, root, queries, identities, connected=None, notify=None,
                 clock=time.time, tick_seconds=5, recent_failures=None, failure_interval=30):
        self.root = Path(root)
        self.queries = dict(queries)
        self.identities = dict(identities)
        self.connected = dict(connected or {})
        if set(self.queries) - set(PROVIDERS) or set(self.queries) - set(self.identities):
            raise ValueError("每个额度查询来源都需要匹配的账号身份函数")
        if not all(callable(v) for v in (*self.queries.values(), *self.identities.values(), *self.connected.values())):
            raise ValueError("额度来源适配器必须可调用")
        self.notify = notify
        if recent_failures is not None and not callable(recent_failures):
            raise ValueError("失败事件读取器必须可调用")
        self.recent_failures = recent_failures
        self.failure_interval = max(0.05, float(failure_interval))
        self.clock = clock
        self.tick_seconds = max(0.05, float(tick_seconds))
        self._lock = threading.RLock()
        self._provider_locks = {p: threading.Lock() for p in self.queries}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._failure_thread = None
        self._started_at = self.clock()
        self._failure_cursor = self._started_at
        self._failure_read_error = False
        self._last_failure_check_at = None
        self._settings_path = self.root / "quota-monitor-settings.json"
        self._ledger_path = self.root / "quota-monitor-alerts.json"
        try:
            self._settings = _validate_settings(_load_json(self._settings_path, DEFAULTS))
        except ValueError:
            self._settings = copy.deepcopy(DEFAULTS)
        ledger = _load_json(self._ledger_path, {})
        try:
            secret = bytes.fromhex(ledger.get("secret", ""))
            if len(secret) != 32:
                raise ValueError()
        except (ValueError, TypeError):
            secret = secrets.token_bytes(32)
            ledger = {}
        self._secret = secret
        entries = ledger.get("entries")
        self._ledger = entries if isinstance(entries, dict) else {}
        self._prune_ledger()
        self._accounts = {p: self._empty_account() for p in self.queries}
        self._next = {p: 0.0 for p in self.queries}
        self._failures = {p: 0 for p in self.queries}
        self._events = []
        self._seen_requests = {}

    @staticmethod
    def _empty_account():
        return {"identity": None, "connected": False, "quota": None, "error": None, "lastAttemptAt": None}

    def _hash(self, value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hmac.new(self._secret, encoded, hashlib.sha256).hexdigest()

    def _prune_ledger(self):
        now = self.clock()
        entries = [(key, val) for key, val in self._ledger.items()
                   if isinstance(key, str) and isinstance(val, dict)
                   and _safe_number(val.get("at")) is not None and val["at"] >= now - 31 * 86400]
        self._ledger = dict(sorted(entries, key=lambda pair: pair[1]["at"], reverse=True)[:_MAX_LEDGER])

    def _save_ledger(self):
        self._prune_ledger()
        _atomic_json(self._ledger_path, {"secret": self._secret.hex(), "entries": self._ledger})

    def _identity(self, provider):
        if provider in self.connected and not self.connected[provider]():
            return False, None
        raw = self.identities[provider]()
        if raw is None or raw == "" or raw == ():
            return True, None
        return True, self._hash([provider, raw])

    def _sync_identity(self, provider):
        try:
            connected, ident = self._identity(provider)
            failure = None if not connected or ident else {"kind": "login", "message": "无法确认当前账号身份，请检查登录状态"}
        except Exception:
            connected, ident = True, None
            failure = {"kind": "login", "message": "无法确认当前账号身份，请检查登录状态"}
        with self._lock:
            item = self._accounts[provider]
            if item["identity"] != ident or (not connected and item["connected"]):
                item["quota"] = None
                item["error"] = failure
                self._next[provider] = 0
            elif failure:
                item["quota"] = None
                item["error"] = failure
            item["identity"], item["connected"] = ident, connected
        return connected, ident

    def update_settings(self, changes):
        with self._lock:
            if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
                raise ValueError("额度提醒设置不受支持")
            new = _validate_settings({**self._settings, **changes})
            _atomic_json(self._settings_path, new)
            self._settings = new
            for provider in self._next:
                self._next[provider] = 0
        self._wake.set()
        return self.status()

    def _record_event(self, event):
        with self._lock:
            self._events.append(event)
            del self._events[:-50]
        if self.notify is not None:
            try:
                self.notify(copy.deepcopy(event))
            except Exception:
                # Desktop notifications may be unavailable; the event remains
                # in the monitor status for the UI to display.
                pass

    def _warn_low(self, provider, ident, quota):
        candidates = {}
        threshold = self._settings["thresholdRemainingPercent"]
        for row in quota["windows"]:
            used = row.get("usedPercent")
            if used is None or used < 0:
                continue
            window = self._hash([provider, ident, row["name"], row.get("windowSeconds"),
                                 row.get("resetsAt"), row.get("expiresAt")])
            if used >= 100:
                level = "exhausted"
            elif 100 - used <= threshold:
                level = "low"
            else:
                level = None
            candidates[window] = (level, row)
        events = []
        with self._lock:
            if not self._settings["enabled"] or self._accounts[provider]["identity"] != ident:
                return
            # Entries for windows that are now healthy must be removed so a
            # later crossing, including windows lacking reset times, re-alerts.
            for window, (level, row) in candidates.items():
                old = self._ledger.get(window)
                if level is None:
                    self._ledger.pop(window, None)
                    continue
                if old and old.get("level") == level:
                    continue
                self._ledger[window] = {"at": self.clock(), "level": level}
                name = _NAMES[provider]
                remaining = max(0, 100 - row["usedPercent"])
                message = f"{name} · {row['name']}已使用 {row['usedPercent']:g}%"
                message += "；额度快照显示已用完" if level == "exhausted" else f"；剩余约 {remaining:g}%"
                events.append({"provider": provider, "kind": level, "time": self.clock(),
                               "title": "订阅额度提醒", "message": message,
                               "source": quota["source"], "checkedAt": quota["checkedAt"]})
            try:
                self._save_ledger()
            except OSError:
                # Alert still works within the process. The next restart may
                # repeat it, and status exposes that persistence has failed.
                self._ledger_error = True
            else:
                self._ledger_error = False
        for event in events:
            self._record_event(event)

    def _poll(self, provider):
        lock = self._provider_locks[provider]
        if not lock.acquire(blocking=False):
            return
        try:
            connected, before = self._sync_identity(provider)
            if not connected or before is None:
                with self._lock:
                    self._next[provider] = self.clock() + self._settings["intervalSeconds"]
                return
            with self._lock:
                self._accounts[provider]["lastAttemptAt"] = self.clock()
            try:
                quota = _snapshot(self.queries[provider](), self.clock)
                if self._stop.is_set():
                    return
                after_connected, after = self._sync_identity(provider)
                if not after_connected or before != after:
                    return
                with self._lock:
                    item = self._accounts[provider]
                    if item["identity"] != before:
                        return
                    item["quota"], item["error"] = quota, None
                    self._failures[provider] = 0
                    self._next[provider] = self.clock() + self._settings["intervalSeconds"]
                self._warn_low(provider, before, quota)
            except Exception as exc:
                if self._stop.is_set():
                    return
                after_connected, after = self._sync_identity(provider)
                if not after_connected or before != after:
                    return
                # Existing adapters intentionally expose safe HTTP status but
                # not response bodies. Extract only the number from the error.
                match = re.search(r"HTTP\s+(\d{3})", str(exc))
                status = int(match.group(1)) if match else None
                # The adapter turns socket exceptions into this fixed safe
                # message; preserve that category without returning raw text.
                network = isinstance(exc, (OSError, TimeoutError)) or str(exc) == "额度查询不可用，请检查网络并重试"
                kind, message = classify_error(status=status, error="network" if network else None)
                with self._lock:
                    if self._accounts[provider]["identity"] != before:
                        return
                    self._accounts[provider]["error"] = {"kind": kind, "message": message, "time": self.clock()}
                    self._failures[provider] += 1
                    base = self._settings["intervalSeconds"]
                    self._next[provider] = self.clock() + min(max(base, 3600), base * (2 ** min(self._failures[provider], 3)))
        finally:
            lock.release()

    def refresh(self, provider=None):
        """Perform a synchronous manual refresh, or all providers if omitted."""
        if provider is not None and provider not in self.queries:
            raise ValueError("不支持的额度来源")
        for name in ([provider] if provider else self.queries):
            self._poll(name)
        return self.status()

    def report_upstream(self, provider, *, status=None, code=None, error=None, event_id=None, at=None):
        """Report a newly observed request failure; historical events never notify.

        The integration passes only a provider, numeric HTTP status, structured
        code/error category and the stable request ID. No raw vendor text is
        needed. Returns True if a public event was generated.
        """
        if (provider not in self.queries and provider != "codex-native") or self._stop.is_set():
            return False
        now = self.clock()
        at = now if at is None else _safe_number(at)
        if at is None or at < self._started_at:
            return False
        kind, message = classify_error(status=status, code=code, error=error)
        if kind == "unknown":
            return False
        # The observation index identifies a route, not the account active when
        # that request was sent. Do not attach it to the current login: it may
        # have changed before the collector imported the event.
        fingerprint = self._hash([provider, "request", str(event_id)]) if event_id is not None else None
        with self._lock:
            if not self._settings["enabled"] or (fingerprint and fingerprint in self._seen_requests):
                return False
            if fingerprint:
                self._seen_requests[fingerprint] = at
        self._record_event({"provider": provider, "kind": kind, "time": at,
                            "title": f"{_NAMES[provider]}请求受限" if kind in ("quota", "rate_limit", "limited") else f"{_NAMES[provider]}请求失败",
                            "message": message + "；请求所属账号未记录",
                            "source": "Hub 已观测的上游线路请求", "accountAttribution": "unknown"})
        return True

    def status(self):
        for provider in self.queries:
            self._sync_identity(provider)
        with self._lock:
            now = self.clock()
            accounts = {}
            for provider, item in self._accounts.items():
                quota = copy.deepcopy(item["quota"])
                checked = quota.get("checkedAt") if quota else None
                stale = bool(quota and (item["error"] or checked is None or now - checked > self._settings["intervalSeconds"] * 2))
                accounts[provider] = {"connected": item["connected"], "quota": quota,
                                      "error": copy.deepcopy(item["error"]), "stale": stale,
                                      "lastAttemptAt": item["lastAttemptAt"]}
            return {"settings": copy.deepcopy(self._settings), "accounts": accounts,
                    "events": copy.deepcopy(self._events), "running": bool(self._thread and self._thread.is_alive()),
                    "alertPersistenceError": bool(getattr(self, "_ledger_error", False)),
                    "failureMonitoringAvailable": self.recent_failures is not None,
                    "failureReadError": self._failure_read_error,
                    "lastFailureCheckAt": self._last_failure_check_at}

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                enabled = self._settings["enabled"]
                due = [p for p in self.queries if self._next[p] <= self.clock()] if enabled else []
            for provider in due:
                if self._stop.is_set():
                    break
                self._poll(provider)
            self._wake.wait(self.tick_seconds)
            self._wake.clear()

    def _check_recent_failures(self):
        if self.recent_failures is None:
            return
        with self._lock:
            if not self._settings["enabled"]:
                return
            since = max(self._started_at, self._failure_cursor - 300)
        try:
            rows = self.recent_failures(since)
            if not isinstance(rows, (list, tuple)):
                raise ValueError("无法读取失败事件")
            latest = since
            for row in rows:
                if not isinstance(row, dict):
                    continue
                at = _safe_number(row.get("time"))
                event_id = row.get("id")
                if at is None or at < self._started_at or not isinstance(event_id, str) or not event_id:
                    continue
                latest = max(latest, at)
                self.report_upstream(row.get("provider"), status=row.get("httpStatus"),
                                     code=row.get("code"), event_id=event_id, at=at)
            with self._lock:
                self._failure_cursor = max(self._failure_cursor, latest)
                floor = max(self._started_at, self._failure_cursor - 300)
                self._seen_requests = {key: when for key, when in self._seen_requests.items() if when >= floor}
                self._failure_read_error = False
                self._last_failure_check_at = self.clock()
        except Exception:
            # An unavailable observation database must not stop quota checks.
            # The next pass retries from the previous cursor.
            with self._lock:
                self._failure_read_error = True
                self._last_failure_check_at = self.clock()
            return

    def _failure_loop(self):
        while not self._stop.is_set():
            self._check_recent_failures()
            self._stop.wait(self.failure_interval)

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._started_at = self.clock()
            self._failure_cursor = self._started_at
            self._thread = threading.Thread(target=self._loop, name="quota-monitor", daemon=True)
            self._thread.start()
            if self.recent_failures is not None:
                self._failure_thread = threading.Thread(target=self._failure_loop, name="quota-failures", daemon=True)
                self._failure_thread.start()

    def close(self, timeout=25):
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
            failure_thread = self._failure_thread
        deadline = time.monotonic() + max(0, timeout)
        for worker in (thread, failure_thread):
            if worker and worker is not threading.current_thread():
                worker.join(max(0, deadline - time.monotonic()))
        return not any(worker and worker.is_alive() for worker in (thread, failure_thread))
