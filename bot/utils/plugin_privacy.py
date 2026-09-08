"""Typed host contract for operator-plugin privacy deletion callbacks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Literal

PrivacyDeletionScope = Literal["memory", "all"]
PrivacyDeletionCallback = Callable[
    [str, PrivacyDeletionScope], Awaitable["PrivacyDeletionCallbackResult"]
]


@dataclass(frozen=True, slots=True)
class PrivacyDeletionCallbackResult:
    """Truthful user-visible outcome from one plugin-owned data deletion."""

    ok: bool
    lines: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.lines or any(not line.strip() for line in self.lines):
            raise ValueError("Privacy deletion callback results require non-empty lines.")


@dataclass(frozen=True, slots=True)
class _RegisteredCallback:
    callback: PrivacyDeletionCallback
    scopes: frozenset[PrivacyDeletionScope]


class PrivacyDeletionCallbackRegistry:
    """Process-local callbacks whose required names are persisted by the host."""

    def __init__(self) -> None:
        self._callbacks: dict[str, _RegisteredCallback] = {}

    def register(
        self,
        name: str,
        callback: PrivacyDeletionCallback,
        *,
        scopes: frozenset[PrivacyDeletionScope],
    ) -> None:
        key = name.strip()
        if not key:
            raise ValueError("Privacy deletion callback name is required.")
        if key in self._callbacks:
            raise ValueError(f"Privacy deletion callback already registered: {key}")
        if not callable(callback):
            raise TypeError("Privacy deletion callback must be callable.")
        if not scopes or not scopes.issubset({"memory", "all"}):
            raise ValueError("Privacy deletion callback scopes must contain memory and/or all.")
        self._callbacks[key] = _RegisteredCallback(callback=callback, scopes=scopes)

    def names_for(self, scope: PrivacyDeletionScope) -> tuple[str, ...]:
        """Return the stable callback set that must survive durable replay."""
        return tuple(
            name for name, registered in self._callbacks.items() if self._applies(registered, scope)
        )

    def resolve(self, name: str, scope: PrivacyDeletionScope) -> PrivacyDeletionCallback | None:
        registered = self._callbacks.get(name)
        if registered is None or not self._applies(registered, scope):
            return None
        return registered.callback

    @staticmethod
    def _applies(registered: _RegisteredCallback, scope: PrivacyDeletionScope) -> bool:
        # Full deletion subsumes memory-only deletion, while a full-data callback
        # must never run for a memory-only request.
        return scope in registered.scopes or (scope == "all" and "memory" in registered.scopes)

    def snapshot(self) -> tuple[str, ...]:
        """Capture registration order so a failed plugin can be rolled back."""
        return tuple(self._callbacks)

    def restore(self, names: Iterable[str]) -> None:
        """Remove registrations added after a prior snapshot."""
        keep = frozenset(names)
        self._callbacks = {
            name: registered for name, registered in self._callbacks.items() if name in keep
        }


__all__ = [
    "PrivacyDeletionCallback",
    "PrivacyDeletionCallbackRegistry",
    "PrivacyDeletionCallbackResult",
    "PrivacyDeletionScope",
]
