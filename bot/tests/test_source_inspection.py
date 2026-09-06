from __future__ import annotations

import ast

import pytest

from tests.source_inspection import runtime_imports


@pytest.mark.parametrize("guard", ["TYPE_CHECKING", "typing.TYPE_CHECKING"])
def test_runtime_imports_include_type_checking_else_branches(guard: str) -> None:
    tree = ast.parse(
        f"""
from typing import TYPE_CHECKING
import typing
if {guard}:
    import typing_only
else:
    import runtime_fallback
    if enabled:
        from storage.db import Database
"""
    )

    assert runtime_imports(tree) == {"typing", "runtime_fallback", "storage.db"}


def test_runtime_imports_include_conditional_and_lazy_absolute_imports() -> None:
    tree = ast.parse(
        """
import typing as t
from pathlib import Path
if enabled:
    import enabled_backend
else:
    import disabled_backend
def load():
    from providers.base import LLMProvider
    if t.TYPE_CHECKING:
        import typing_only
"""
    )

    assert runtime_imports(tree) == {
        "typing",
        "pathlib",
        "enabled_backend",
        "disabled_backend",
        "providers.base",
    }


@pytest.mark.parametrize(
    "import_line,guard",
    [
        ("from typing import TYPE_CHECKING as TC", "TC"),
        ("import typing as t", "t.TYPE_CHECKING"),
    ],
)
def test_typing_aliases_and_negated_guards(import_line: str, guard: str) -> None:
    tree = ast.parse(
        f"{import_line}\nif not {guard}:\n    import runtime\nelse:\n    import type_only"
    )
    assert runtime_imports(tree) == {"typing", "runtime"}


@pytest.mark.parametrize(
    "binding",
    [
        "from config import settings",
        "import typing as settings\nsettings = other",
        "import typing as settings\nsettings.TYPE_CHECKING = True",
        "import typing as settings\ndef f(settings): pass",
    ],
)
def test_unrelated_or_shadowed_type_checking_attributes_do_not_hide_imports(binding: str) -> None:
    tree = ast.parse(f"{binding}\nif settings.TYPE_CHECKING:\n    import discord")
    assert "discord" in runtime_imports(tree)


def test_relative_imports_are_resolved_against_the_source_package() -> None:
    tree = ast.parse("from . import sibling\nfrom ..shared import helper\nfrom .child import thing")
    assert runtime_imports(tree, package="package.nested") == {
        "package.nested.sibling",
        "package.shared",
        "package.nested.child",
    }
    with pytest.raises(ValueError, match="containing package"):
        runtime_imports(tree)


def test_source_discovery_includes_new_layouts_and_excludes_private_data(tmp_path) -> None:
    from tests.source_inspection import python_sources, source_module

    sources = {
        "branding.py": "branding",
        "new_namespace/client.py": "new_namespace.client",
        "packages/kimi-agent-module-api/src/kimi_agent_module_api/nested/api.py": "kimi_agent_module_api.nested.api",
        "modules/example/src/community_agent_reference_module/__init__.py": "community_agent_reference_module",
        "modules/minimal/hello_module.py": "hello_module",
    }
    excluded = [
        ".venv/lib/site.py",
        "data/dev/workspaces/script.py",
        "workspaces/script.py",
        "skills/store/private/script.py",
        "config.dev/plugin.py",
        "modules/example/tests/test_module.py",
        "modules/example/build/lib/module.py",
        "deploy/betterwright/node_modules/script.py",
    ]
    for name in [*sources, *excluded]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid Python; discovery must not execute or parse files")
    observed = {
        path.relative_to(tmp_path).as_posix(): source_module(path, tmp_path)
        for path in python_sources(tmp_path)
    }
    assert observed == sources
