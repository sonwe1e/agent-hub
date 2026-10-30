"""Lifecycle for optional, local workspace services."""
from __future__ import annotations

import threading


class WorkspaceServices:
    def __init__(self, library, quota, interval=5):
        self.library, self.quota = library, quota
        self.interval = max(1, interval)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()
        self._auto_error = None
        self._auto_results = []

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self.quota.start()
            self._thread = threading.Thread(target=self._auto_loop, name="capability-sync", daemon=True)
            self._thread.start()

    def _auto_loop(self):
        previous = {}
        while not self._stop.wait(self.interval):
            try:
                items = self.library.list_items()["items"]
                current = {item["id"]: item.get("sourceHash") for item in items
                           if item.get("mode") == "auto" and item.get("published") and item.get("sourceHash")}
                stable = [key for key, value in current.items() if previous.get(key) == value]
                previous = current
                if stable and not self._stop.is_set():
                    result = self.library.sync_auto(item_ids=stable, expected_hashes={key: current[key] for key in stable})
                    with self._lock:
                        self._auto_results = result.get("results", [])
                with self._lock:
                    self._auto_error = None
            except Exception:
                # Keep the worker alive and expose the failure without paths,
                # instructions or credential-bearing exception strings.
                with self._lock:
                    self._auto_error = "自动同步检查失败；请查看能力库状态。"

    def status(self):
        with self._lock:
            return {"running": bool(self._thread and self._thread.is_alive()),
                    "autoError": self._auto_error, "autoResults": list(self._auto_results)}

    def close(self, timeout=25):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(min(5, max(0, timeout)))
        return self.quota.close(timeout=max(0, timeout - 5)) and not (thread and thread.is_alive())
