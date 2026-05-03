"""Two-tier cache for Scopus API responses.

Layer 1 — an in-process OrderedDict implementing LRU eviction. Sub-millisecond
hits, bounded memory footprint.

Layer 2 — a `shelve` persistent store on disk that survives restarts. One
shelve file per cache directory (instead of one JSON per key, which produces
thousands of inodes for a typical session).

Cache keys are derived from the canonical URL form (scheme + lowered host +
sorted query params) so that ``?count=5&q=x`` and ``?q=x&count=5`` collapse to
the same entry. TTL is per-entry — the request layer chooses how long each
class of payload should live.
"""

from __future__ import annotations

import logging
import shelve
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Entry:
    """One stored payload + its absolute expiry epoch (seconds)."""
    payload: Any
    expires_at: float

    def is_fresh(self, now: float) -> bool:
        return now < self.expires_at


class CacheManager:
    """Two-tier (memory + shelve) TTL cache.

    Public interface preserved for the request layer:
        cache.get(url, params) -> dict | None
        cache.set(url, data, params, ttl=...)

    Both layers store the same `_Entry` objects; the in-process layer enforces
    an LRU bound (default 256 entries) so that long sessions don't grow
    unboundedly even if the disk file does.
    """

    _DEFAULT_MEMORY_SLOTS = 256
    _SHELVE_FILENAME = "scopus_responses"

    def __init__(
        self,
        cache_dir: Union[str, Path] = ".cache",
        expiration_seconds: int = 86400,
        memory_slots: int = _DEFAULT_MEMORY_SLOTS,
    ):
        self._dir = Path(cache_dir).resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._default_ttl = max(0, int(expiration_seconds))
        self._memory_slots = max(1, int(memory_slots))
        # OrderedDict + Lock = our hand-rolled thread-safe LRU
        self._mem: "OrderedDict[str, _Entry]" = OrderedDict()
        self._mem_lock = Lock()
        self._shelve_path = str(self._dir / self._SHELVE_FILENAME)

    # ------------------------------------------------------------------ #
    # Key derivation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _canonical_key(url: str, params: Optional[Dict[str, Any]]) -> str:
        """Return a stable string identifying a (URL, params) request.

        Implementation note: we don't hash, we keep the readable canonical
        form. Shelve's gdbm/dbm backend already hashes keys internally; an
        extra SHA prefix gains us nothing and only makes debugging harder.
        """
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        # Merge URL query string with explicit params, then sort
        merged: list = list(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        if params:
            for key in sorted(params.keys()):
                value = params[key]
                if value is None:
                    continue
                merged.append((str(key), str(value)))
        merged.sort()
        encoded = urllib.parse.urlencode(merged)
        return f"{parts.scheme}://{host}{parts.path}?{encoded}"

    # ------------------------------------------------------------------ #
    # Memory tier
    # ------------------------------------------------------------------ #

    def _mem_lookup(self, key: str, now: float) -> Optional[_Entry]:
        with self._mem_lock:
            entry = self._mem.get(key)
            if entry is None:
                return None
            if not entry.is_fresh(now):
                # Lazy eviction of stale entry
                self._mem.pop(key, None)
                return None
            # Move to end = mark as most-recently-used
            self._mem.move_to_end(key)
            return entry

    def _mem_store(self, key: str, entry: _Entry) -> None:
        with self._mem_lock:
            self._mem[key] = entry
            self._mem.move_to_end(key)
            while len(self._mem) > self._memory_slots:
                self._mem.popitem(last=False)

    # ------------------------------------------------------------------ #
    # Disk tier (shelve)
    # ------------------------------------------------------------------ #

    def _disk_lookup(self, key: str, now: float) -> Optional[_Entry]:
        try:
            with shelve.open(self._shelve_path, flag="r") as db:
                stored = db.get(key)
        except Exception as exc:
            # Corrupted shelve, missing dbm backend, locked file, etc. Treat
            # as cache miss; the request layer will refetch.
            logger.debug("cache disk lookup failed: %s", exc)
            return None
        if not isinstance(stored, _Entry):
            return None
        if not stored.is_fresh(now):
            return None
        return stored

    def _disk_store(self, key: str, entry: _Entry) -> None:
        try:
            with shelve.open(self._shelve_path, flag="c") as db:
                db[key] = entry
        except Exception as exc:
            # We never let the cache crash the request flow.
            logger.debug("cache disk store failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Public API (matches what the request layer expects)
    # ------------------------------------------------------------------ #

    def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        key = self._canonical_key(url, params)
        now = time.time()
        # Try memory first, fall back to disk and warm the memory tier on hit.
        hit = self._mem_lookup(key, now)
        if hit is not None:
            return hit.payload
        disk_hit = self._disk_lookup(key, now)
        if disk_hit is not None:
            self._mem_store(key, disk_hit)
            return disk_hit.payload
        return None

    def set(
        self,
        url: str,
        data: Any,
        params: Optional[Dict[str, Any]] = None,
        ttl: Optional[int] = None,
    ) -> None:
        ttl_seconds = ttl if ttl is not None else self._default_ttl
        if ttl_seconds <= 0:
            return
        entry = _Entry(payload=data, expires_at=time.time() + ttl_seconds)
        key = self._canonical_key(url, params)
        self._mem_store(key, entry)
        self._disk_store(key, entry)
