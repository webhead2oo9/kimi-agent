"""Shared skip policy for tests that need a working live sandbox."""

from __future__ import annotations

import os
from typing import Never

import pytest

REQUIRE_SANDBOX_ENV = "KIMI_REQUIRE_SANDBOX_TESTS"


def sandbox_skip_allowed(unavailable: bool) -> bool:
    """Skip an unavailable live sandbox locally, but exercise it in required CI."""

    return unavailable and os.environ.get(REQUIRE_SANDBOX_ENV) != "1"


def sandbox_unavailable(reason: str) -> Never:
    """Handle a prerequisite discovered after a live-sandbox test has started."""

    if os.environ.get(REQUIRE_SANDBOX_ENV) == "1":
        pytest.fail(f"{REQUIRE_SANDBOX_ENV}=1 but {reason}")
    pytest.skip(reason)
