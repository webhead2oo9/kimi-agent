"""Thread-safe last-known-good cache for hot-reloaded config fragments.

Entries use a bounded LRU by default. Fail-closed policies can disable eviction.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from threading import Lock

DEFAULT_MAX_ENTRIES = 64


class LastKnownGoodCache[T]:
    """Per-path cache of the last value that parsed cleanly."""

    def __init__(self, *, max_entries: int | None = DEFAULT_MAX_ENTRIES) -> None:
        self._entries: OrderedDict[Path, T] = OrderedDict()
        self._lock = Lock()
        self._max_entries = max_entries

    @staticmethod
    def key(fragment: Path) -> Path:
        """Return a stable resolved key for an optional fragment."""

        return fragment.resolve(strict=False)

    def remember(self, key: Path, value: T) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            if self._max_entries is not None:
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)

    def forget(self, key: Path) -> None:
        """Remove the cached value for ``key``."""

        with self._lock:
            self._entries.pop(key, None)

    def last_good(self, key: Path) -> T | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                return None
            self._entries.move_to_end(key)
            return value
