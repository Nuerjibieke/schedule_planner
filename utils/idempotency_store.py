"""Persistent idempotency key store (JSON file-backed)."""
from __future__ import annotations

import json
import os
import time
from threading import RLock
from typing import Dict


class PersistentIdempotencyStore:
    """Best-effort persistent cache for write fingerprints."""

    def __init__(self, path: str, ttl_seconds: int, max_entries: int) -> None:
        self.path = path
        self.ttl_seconds = max(1, int(ttl_seconds or 1))
        self.max_entries = max(32, int(max_entries or 32))
        self._lock = RLock()
        self._cache: Dict[str, float] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._cache = self._load_file()
            self._loaded = True

    def _load_file(self) -> Dict[str, float]:
        try:
            if not os.path.exists(self.path):
                return {}
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return {}
            out: Dict[str, float] = {}
            for k, v in raw.items():
                try:
                    out[str(k)] = float(v)
                except Exception:
                    continue
            return out
        except Exception:
            return {}

    def _persist(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            # Best effort only.
            pass

    def _evict(self, now_ts: float) -> None:
        expire_before = now_ts - self.ttl_seconds
        stale = [k for k, ts in self._cache.items() if ts < expire_before]
        for k in stale:
            self._cache.pop(k, None)
        overflow = len(self._cache) - self.max_entries
        if overflow > 0:
            oldest = sorted(self._cache.items(), key=lambda x: x[1])[:overflow]
            for k, _ in oldest:
                self._cache.pop(k, None)

    def is_recent(self, key: str, now_ts: float | None = None) -> bool:
        self._ensure_loaded()
        with self._lock:
            ts_now = float(now_ts if now_ts is not None else time.time())
            self._evict(ts_now)
            ts = self._cache.get(str(key))
            return ts is not None and (ts_now - ts) <= self.ttl_seconds

    def mark(self, key: str, now_ts: float | None = None) -> None:
        self._ensure_loaded()
        with self._lock:
            ts_now = float(now_ts if now_ts is not None else time.time())
            self._evict(ts_now)
            self._cache[str(key)] = ts_now
            self._persist()

