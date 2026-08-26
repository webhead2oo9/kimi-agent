"""Guards that the documentation tree stays consistent with the code.

The docs are hand-maintained mirrors of things the code owns: relative links
between pages, and env-var names quoted in prose. Both rot silently: a doc
naming a setting that does not exist reads as authoritative and sends an
operator to configure a no-op. These turn that discipline into checked
invariants.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from config.settings import Settings
from tests.helpers import PROJECT_ROOT

REPO_ROOT = PROJECT_ROOT.parent

# Pages describing current behavior. Both guards apply to these.
_LIVE_DOC_PAGES: tuple[Path, ...] = (
    *sorted((REPO_ROOT / "docs").glob("*.md")),
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "config" / "prompts" / "README.md",
    PROJECT_ROOT / "deploy" / "README.md",
    PROJECT_ROOT / "deploy" / "hindsight" / "README.md",
    PROJECT_ROOT / "skills" / "README.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "CLAUDE.md",
)

_DOC_PAGES: tuple[Path, ...] = _LIVE_DOC_PAGES

_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# A backticked all-caps token in prose reads as an env var. Five characters
# avoids matching short prose acronyms while still covering every real setting.
_ENV_TOKEN_RE = re.compile(r"`([A-Z][A-Z0-9_]{4,})`")

# Tokens that look like settings but are deliberately not `Settings` fields.
_EXTERNAL_ENV_TOKENS: frozenset[str] = frozenset(
    {
        # Read directly from the process environment, before/outside Settings.
        "ENV_FILE",
        # Env vars the skill runner scrubs from subprocess environments.
        "BASH_ENV",
        "DYLD_INSERT_LIBRARIES",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PYTHONPATH",
        # Scratch-home env vars the skill runner sets per run (skills/runner.py).
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "MPLCONFIGDIR",
        # Module-level declaration a plugin exports; lives in plugin code.
        "PLUGIN_SETTINGS",
        # Documented in providers.md precisely as a value that is no longer
        # supported, so it must not resolve.
        "DEEP_RESEARCH_API_KEY",
        # Brave's own error code, quoted in configuration.md so an operator can
        # recognize it. It is a provider response value, not a setting.
        "OPTION_NOT_IN_PLAN",
    }
)


def _code_symbols() -> set[str]:
    """Every name bound or referenced as an attribute in non-test source.

    Docs legitimately quote module constants (`SCHEMA_VERSION`,
    `MAX_PLAN_STEPS`) and enum members (`STAFF`) alongside env vars, so the
    guard resolves against real symbols rather than a hand-kept list.
    """

    symbols: set[str] = set()
    for path in PROJECT_ROOT.rglob("*.py"):
        parts = set(path.parts)
        if parts & {"tests", ".venv", "workspaces", "data"}:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except OSError, SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                symbols.add(node.id)
            elif isinstance(node, ast.Attribute):
                symbols.add(node.attr)
    return symbols


def test_relative_doc_links_resolve() -> None:
    broken: list[str] = []
    for page in _DOC_PAGES:
        if not page.exists():
            broken.append(f"{page}: page listed in _DOC_PAGES does not exist")
            continue
        for target in _MARKDOWN_LINK_RE.findall(page.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            resolved = (page.parent / target.split("#", 1)[0]).resolve()
            if not resolved.exists():
                broken.append(f"{page.relative_to(REPO_ROOT)} -> {target}")

    assert not broken, "Broken relative links in documentation:\n" + "\n".join(broken)


def test_env_vars_named_in_docs_exist() -> None:
    """A backticked all-caps token must be a real setting, symbol, or allowlisted.

    This is the guard that catches a documented setting which does not exist.
    The failure mode is an operator configuring a no-op while believing a
    fence is up.
    """

    known = {name.upper() for name in Settings.model_fields}
    known |= _code_symbols()
    known |= _EXTERNAL_ENV_TOKENS

    unresolved: dict[str, list[str]] = {}
    for page in _LIVE_DOC_PAGES:
        if not page.exists():
            continue
        for token in sorted(set(_ENV_TOKEN_RE.findall(page.read_text(encoding="utf-8")))):
            if token not in known:
                unresolved.setdefault(token, []).append(page.name)

    assert not unresolved, (
        "Docs name env vars/symbols that do not exist (fix the name, or add a "
        "genuinely external one to _EXTERNAL_ENV_TOKENS): "
        + "; ".join(f"{token} in {', '.join(pages)}" for token, pages in sorted(unresolved.items()))
    )


def test_nothing_references_the_removed_spec_tree() -> None:
    """`docs/superpowers/` does not exist; nothing may point a reader at it.

    Scans source as well as markdown, because the last stale reference lived in a
    module docstring, which a markdown-only check cannot see.
    """

    offenders: list[str] = []
    for pattern in ("*.md", "*.py"):
        for path in REPO_ROOT.rglob(pattern):
            if set(path.parts) & {".venv", "workspaces", "data"}:
                continue
            if path.name == Path(__file__).name:
                continue
            if "docs/superpowers" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, f"References to the removed docs/superpowers tree: {offenders}"
