"""Last-known-good cache for operator config fragments read every turn.

`config/fragments/tool_config.py` and `config/fragments/tool_policy.py` both read a hand-edited
markdown fragment on every responding turn, so both need the same thing: when a
reload fails, keep serving that path's last value rather than reverting to
"unset" mid-conversation. Both were carrying their own copy of this cache with
mechanically renamed identifiers.

What the two do with a failure still differs and stays with them: tool config
falls back to defaults when there is no cached value, while tool policy raises
rather than silently un-blocking a tool. This only owns the remembering.

Keyed by resolved path, LRU-bounded so a deployment that generates fragment
paths cannot grow it without limit, and locked because turns run concurrently.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from threading import Lock

DEFAULT_MAX_ENTRIES = 64


class LastKnownGoodCache[T]:
    """Per-path cache of the last value that parsed cleanly."""

    def __init__(self, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._entries: OrderedDict[Path, T] = OrderedDict()
        self._lock = Lock()
        self._max_entries = max_entries

    @staticmethod
    def key(fragment: Path) -> Path:
        """Resolve a fragment path to its cache key.

        Non-strict: the fragment legitimately may not exist yet, and an absent
        file must still map to the same key it will have once created.
        """

        return fragment.resolve(strict=False)

    def remember(self, key: Path, value: T) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def forget(self, key: Path) -> None:
        """Drop a path's cached value.

        Callers use this when the file is genuinely absent, so deleting a
        fragment actually reverts the setting instead of pinning the last value
        it had.
        """

        with self._lock:
            self._entries.pop(key, None)

    def last_good(self, key: Path) -> T | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                return None
            self._entries.move_to_end(key)
            return value
