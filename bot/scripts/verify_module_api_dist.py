"""Verify the built module API through an independently packaged consumer."""

from __future__ import annotations

import argparse
from importlib.metadata import entry_points
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import zipfile


def _artifact_names(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    with tarfile.open(path) as archive:
        return set(archive.getnames())


def _verify_artifacts(wheel: Path, sdist: Path) -> None:
    wheel_names = _artifact_names(wheel)
    sdist_names = _artifact_names(sdist)
    assert "kimi_agent_module_api/py.typed" in wheel_names
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_names)
    assert any(name.endswith("/kimi_agent_module_api/py.typed") for name in sdist_names)
    assert any(name.endswith("/LICENSE") for name in sdist_names)


def _verify_consumer() -> None:
    import kimi_agent_module_api as api

    assert api.MODULE_API_VERSION == 1
    matches = [
        point
        for point in entry_points(group=api.MODULE_ENTRYPOINT_GROUP)
        if point.name == "reference_greeter"
    ]
    assert len(matches) == 1
    spec = matches[0].load()
    assert isinstance(spec, api.ModuleSpec)
    assert spec.name == "reference_greeter"
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
