"""Supervisor handshake for experimental managed-configuration restarts."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from app.control_plane import RESTART_EXIT_CODE, ControlPlaneStore


def _run(command: list[str], *, revision: str | None) -> int:
    environment = dict(os.environ)
    if revision:
        environment["KIMI_CONTROL_REVISION"] = revision
    else:
        environment.pop("KIMI_CONTROL_REVISION", None)
    return subprocess.run(command, env=environment, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--control-plane-dir",
        default=os.environ.get("CONTROL_PLANE_DIR", "data/control-plane"),
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command or [sys.executable, "bot.py"]
    if command and command[0] == "--":
        command = command[1:]
    store = ControlPlaneStore(Path(args.control_plane_dir))

    while True:
        state = store.state()
        pending = str(state.get("pending") or "") or None
        result = _run(command, revision=pending)
        after = store.state()

        if pending and after.get("pending") == pending:
            store.rollback_pending(
                reason=f"candidate revision exited with status {result} before healthy startup"
            )
            restored = _run(command, revision=None)
            if restored == RESTART_EXIT_CODE:
                continue
            return restored

        if result == RESTART_EXIT_CODE:
            continue
        return result


if __name__ == "__main__":
    raise SystemExit(main())
