"""Verify the built module API through an independently packaged consumer."""

from __future__ import annotations

import argparse
from importlib import import_module
from importlib.metadata import entry_points
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import zipfile

_EXPECTED_API_FILES = frozenset(
    {
        "kimi_agent_module_api/__init__.py",
        "kimi_agent_module_api/contracts.py",
        "kimi_agent_module_api/events.py",
        "kimi_agent_module_api/images.py",
        "kimi_agent_module_api/py.typed",
        "kimi_agent_module_api/settings.py",
        "kimi_agent_module_api/testing.py",
        "kimi_agent_module_api/tools.py",
        "kimi_agent_module_api/trust.py",
    }
)
_EXPECTED_API_MODULES = (
    "contracts",
    "events",
    "images",
    "settings",
    "testing",
    "tools",
    "trust",
)


def _artifact_names(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    with tarfile.open(path) as archive:
        return set(archive.getnames())


def _verify_artifacts(wheel: Path, sdist: Path) -> None:
    wheel_names = _artifact_names(wheel)
    sdist_names = _artifact_names(sdist)
    missing_wheel = _EXPECTED_API_FILES - wheel_names
    missing_sdist = {
        expected
        for expected in _EXPECTED_API_FILES
        if not any(name.endswith(f"/{expected}") for name in sdist_names)
    }
    assert not missing_wheel, f"API wheel missing files: {sorted(missing_wheel)}"
    assert not missing_sdist, f"API sdist missing files: {sorted(missing_sdist)}"
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_names)
    assert any(name.endswith("/LICENSE") for name in sdist_names)


def _verify_consumer() -> None:
    import kimi_agent_module_api as api

    for module in _EXPECTED_API_MODULES:
        import_module(f"kimi_agent_module_api.{module}")

    contracts = import_module("kimi_agent_module_api.contracts")
    assert hasattr(contracts, "CommandSyncError")
    assert hasattr(contracts, "GuildCommand")
    assert hasattr(contracts, "ModalSpec")
    assert hasattr(contracts, "OutgoingLayout")

    assert api.MODULE_API_VERSION == 2
    matches = [
        point
        for point in entry_points(group=api.MODULE_ENTRYPOINT_GROUP)
        if point.name == "reference_kudos"
    ]
    assert len(matches) == 1
    spec = matches[0].load()
    assert isinstance(spec, api.ModuleSpec)
    assert spec.name == "reference_kudos"
    assert spec.api_version == api.MODULE_API_VERSION


def _verify_install(api_wheel: Path, reference_wheel: Path) -> None:
    script = Path(__file__).resolve()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        venv = root / "venv"
        subprocess.run(["uv", "venv", str(venv)], check=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                str(api_wheel.resolve()),
                str(reference_wheel.resolve()),
            ],
            check=True,
        )
        subprocess.run(["uv", "pip", "check", "--python", str(python)], check=True)
        clean_cwd = root / "cwd"
        clean_cwd.mkdir()
        subprocess.run([str(python), str(script), "consumer"], cwd=clean_cwd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("api_wheel", type=Path)
    verify.add_argument("api_sdist", type=Path)
    verify.add_argument("reference_wheel", type=Path)
    subparsers.add_parser("consumer")
    args = parser.parse_args()

    if args.command == "consumer":
        _verify_consumer()
        return
    _verify_artifacts(args.api_wheel, args.api_sdist)
    _verify_install(args.api_wheel, args.reference_wheel)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, subprocess.CalledProcessError) as exc:
        print(f"module API distribution verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
