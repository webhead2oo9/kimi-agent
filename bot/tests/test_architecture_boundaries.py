from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse(path: str) -> ast.Module:
    # Explicit utf-8: the platform default is cp1252 on Windows, which chokes on
    # any non-latin byte a source file happens to contain.
    return ast.parse((PROJECT_ROOT / path).read_text(encoding="utf-8"), filename=path)


def _imported_modules(path: str) -> set[str]:
    tree = _parse(path)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _imports_any(modules: set[str], disallowed: set[str]) -> set[str]:
    return {
        module
        for module in modules
        for prefix in disallowed
        if module == prefix or module.startswith(f"{prefix}.")
    }


def _top_level_function_names(path: str) -> set[str]:
    tree = _parse(path)
    return {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_agent_core_stays_provider_agnostic_and_discord_free() -> None:
    modules = _imported_modules("agent/core.py")

    assert _imports_any(modules, {"discord", "storage"}) == set()

    provider_imports = _imports_any(modules, {"providers"})
    assert provider_imports <= {
        "providers.base",
        "providers.errors",
        "providers.recalled_context",
        "providers.types",
    }


def test_turn_orchestration_does_not_import_bot_module() -> None:
    assert _imports_any(_imported_modules("agent/turn.py"), {"bot"}) == set()


def test_member_lookup_tool_stays_discord_free() -> None:
    """All discord.py access lives behind DiscordGateway, not in the tool.

    `discord_adapter.*` is the allowed way to reach Discord and must not trip
    this: `_imports_any` matches `discord` exactly or the `discord.` prefix, so
    it already distinguishes them, but the distinction is stated here rather
    than left to a reader noticing the underscore.
    """

    modules = _imported_modules("tools/member.py")

    assert _imports_any(modules, {"discord"}) == set()
    assert "discord_adapter.gateway" in modules


def _runtime_imported_modules(path: str) -> set[str]:
    """Imports that actually execute, ignoring `if TYPE_CHECKING:` blocks.

    A type-only import of `discord` is not a dependency on the SDK: it costs
    nothing at runtime and exists so a signature can name the platform type.
    """

    tree = _parse(path)
    type_checking_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            is_type_checking = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            )
            if is_type_checking:
                type_checking_nodes.update(id(child) for child in ast.walk(node))

    modules: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in type_checking_nodes:
            continue
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_the_discord_sdk_is_confined_to_the_adapter_and_composition_root() -> None:
    """`import discord` may execute only where the platform boundary lives.

    The containment this asserts is real and worth keeping: it is what lets
    agent/, tools/, providers/, storage/ and the rest be exercised without a
    Discord connection.
    """

    allowed_prefixes = ("discord_adapter/", "app/", "commands/")
    offenders: list[str] = []
    for path in PROJECT_ROOT.rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative.startswith(("tests/", "evals/", ".venv/", "workspaces/")):
            continue
        if relative.startswith(allowed_prefixes):
            continue
        if _imports_any(_runtime_imported_modules(relative), {"discord"}):
            offenders.append(relative)

    assert not offenders, f"discord.py imported outside the adapter layer: {offenders}"


def test_bot_entrypoint_does_not_define_policy_helpers() -> None:
    """Keep policy helpers in the modules that own their policy.

    `bot.py` is only the entry point; `app/runtime.py:build_app` is the
    composition root.
    """

    helper_names = _top_level_function_names("bot.py")

    assert "_should_clear_provider_state_for_user_context" not in helper_names
    assert "_provider_state_metadata_for_save" not in helper_names


def test_bot_does_not_register_generic_knowledge_search_tool() -> None:
    """Generic knowledge search must not bypass the staff-curated community tools."""

    modules = _imported_modules("bot.py")
    bot_source = (PROJECT_ROOT / "bot.py").read_text(encoding="utf-8")

    assert "tools.knowledge_search" not in modules
    assert "init_knowledge_search_from_settings" not in bot_source


def test_bot_entrypoint_has_no_runtime_composition_imports() -> None:
    modules = _imported_modules("bot.py")

    assert _imports_any(modules, {"providers", "storage.db", "tools.registry"}) == set()


def test_tool_registry_has_no_package_singleton() -> None:
    registry_source = (PROJECT_ROOT / "tools/registry.py").read_text(encoding="utf-8")

    assert "registry = ToolRegistry()" not in registry_source


def test_app_modules_do_not_import_bot_entrypoint() -> None:
    for path in (PROJECT_ROOT / "app").glob("*.py"):
        modules = _imported_modules(str(path.relative_to(PROJECT_ROOT)))
        assert _imports_any(modules, {"bot"}) == set()


def test_generic_knowledge_search_surface_has_no_files() -> None:
    """Companion to the registration guard above: the files stay gone, not merely unregistered."""

    assert not (PROJECT_ROOT / "tools/knowledge_search.py").exists()
    assert not (PROJECT_ROOT / "knowledge").exists()
    assert not (PROJECT_ROOT / "docs/knowledge.md").exists()
    assert not (PROJECT_ROOT / "config/knowledge_sources.example.json").exists()


def test_tools_never_read_the_physical_guild() -> None:
    """`MessageContext.guild_id` is the logical data scope and is None in
    guild-less personal chat; `platform_guild_id` is the raw Discord location
    and carries no authority.

    Every trust, policy, and data-scope decision a tool makes (dispatch scoping,
    community banks, skill scoping, catalogs) must read the logical scope, so
    reaching for the physical guild inside tools/ is the exact mistake that let
    a personal-chat turn write into the community bank and skill store of
    whatever guild the slash command happened to be invoked from.
    """
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "tools").rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(_parse(relative)):
            if isinstance(node, ast.Attribute) and node.attr == "platform_guild_id":
                offenders.append(f"{relative}:{node.lineno}")

    assert not offenders, (
        f"tools/ must use the logical ctx.guild_id, not the physical location: {offenders}"
    )
