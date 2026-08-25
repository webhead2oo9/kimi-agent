"""Shared pytest setup for the `tests/` root.

Only process-global state guards belong here. `app/tool_surfaces.py` keeps a
module-level table that the composition root fills once at startup; a test that
loads a plugin writes into it for real, so reset it per test rather than leaving
later tests to depend on collection order.

Reusable stubs and builders are *not* fixtures. They take constructor
arguments, so they live as plain classes in `tests/helpers.py`
(``StubProvider``, ``StubProviderManager``, ``StubContextManager``,
``RecordingEnsureUserBank``, ``RecordingRecall``, ``FakeResponses``,
``make_message_context``). Import from there before hand-rolling another copy.

Every async test needs an explicit `@pytest.mark.asyncio`. There is no
`asyncio_mode = auto` (see pyproject.toml); an async test missing the marker
does not fail, it silently never runs its body.
"""

from __future__ import annotations

import pytest

from app.tool_surfaces import reset_surface_tools


@pytest.fixture(autouse=True)
def _reset_tool_surfaces() -> None:
    reset_surface_tools()
