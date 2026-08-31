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

from collections.abc import Generator
import os

import pytest

from app.tool_surfaces import reset_surface_tools

# The modules whose live-jail tests skip when the Linux boundary cannot start.
# Under KIMI_REQUIRE_SANDBOX_TESTS=1 (the CI sandbox job) a skip in any of them
# is a failure: the job exists to prove those jails ran, and a new skip
# condition must not be able to hollow it out while the pass count looks fine.
_SANDBOX_MODULES = frozenset(
    {"test_sandbox_runner", "test_code_exec_tool", "test_skill_sandbox", "test_sandbox_required"}
)
_REQUIRE_SANDBOX_ENV = "KIMI_REQUIRE_SANDBOX_TESTS"


@pytest.fixture(autouse=True)
def _reset_tool_surfaces() -> None:
    reset_surface_tools()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None]:
    outcome = yield
    if os.environ.get(_REQUIRE_SANDBOX_ENV) != "1":
        return
    report = outcome.get_result()  # type: ignore[attr-defined]
    if not report.skipped or item.path.stem not in _SANDBOX_MODULES:
        return
    report.outcome = "failed"
    report.longrepr = f"skipped under {_REQUIRE_SANDBOX_ENV}=1: {report.longrepr}"
