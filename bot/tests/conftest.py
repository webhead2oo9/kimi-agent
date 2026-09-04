"""Shared pytest setup for the `tests/` root.

Only process-global state guards belong here. The composition root updates the
tool surface table and default config directory at startup. Tests that build an
app mutate these globals for real, so isolate them per test rather than leaving
later tests to depend on collection order.

Reusable stubs and builders are *not* fixtures. They take constructor
arguments, so they live as plain classes in `tests/helpers.py`
(``StubProvider``, ``StubProviderManager``, ``StubContextManager``,
``RecordingEnsureUserBank``, ``RecordingRecall``, ``FakeResponses``,
``make_message_context``, ``make_settings``). Import from there before hand-rolling
another copy.

Every async test needs an explicit `@pytest.mark.asyncio`. There is no
`asyncio_mode = auto` (see pyproject.toml); an async test missing the marker
does not fail, it silently never runs its body.
"""

from __future__ import annotations

import os

import pytest

from app import tool_surfaces
from config import paths
from config.settings import Settings


def _settings_env_names() -> frozenset[str]:
    """Every environment variable pydantic-settings would read into Settings.

    Settings declares no env prefix, no aliases, and case-insensitive names, so
    the upper-cased field name is the whole story. The assertions make a future
    alias or prefix fail this derivation loudly instead of leaking past the
    scrub below.
    """

    assert not Settings.model_config.get("env_prefix"), "update _settings_env_names"
    assert not Settings.model_config.get("case_sensitive"), "update _settings_env_names"
    aliased = [
        name
        for name, field in Settings.model_fields.items()
        if field.validation_alias is not None or field.alias is not None
    ]
    assert not aliased, f"aliased Settings fields need env names here: {aliased}"
    return frozenset(name.upper() for name in Settings.model_fields)


_SETTINGS_ENV_NAMES = _settings_env_names()


@pytest.fixture(autouse=True)
def _reset_tool_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_surfaces, "_SURFACE_TOOLS", {})


@pytest.fixture(autouse=True)
def _isolate_default_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # build_app changes the process default. Restore it after each test so a
    # later prompt render cannot read an earlier test's temporary config tree.
    monkeypatch.setattr(paths, "_default_config_dir", paths.default_config_dir())


@pytest.fixture(autouse=True)
def _isolate_settings_environment(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if request.node.get_closest_marker("uses_live_settings_env") is not None:
        return

    # Ambient Settings variables would otherwise leak into every Settings(...)
    # a test builds, even with the dotenv file suppressed.
    for env_name in tuple(os.environ):
        if env_name.upper() in _SETTINGS_ENV_NAMES:
            monkeypatch.delenv(env_name)
