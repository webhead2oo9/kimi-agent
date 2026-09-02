"""Keep KimiApplication private state out of the test suite."""

from __future__ import annotations

import re
from pathlib import Path


TESTS_DIR = Path(__file__).parent
_PRIVATE_APP_REACH = re.compile(r"app[.][_][a-z]")


def test_kimi_application_private_reaches_stay_behind_test_seams() -> None:
    offenders: list[str] = []

    for path in sorted(TESTS_DIR.rglob("*.py")):
        relative_path = path.relative_to(TESTS_DIR).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if _PRIVATE_APP_REACH.search(line):
                offenders.append(f"{relative_path}:{line_number}: {line.strip()}")

    assert offenders == [], "KimiApplication private reaches are forbidden in tests:\n" + "\n".join(
        offenders
    )
