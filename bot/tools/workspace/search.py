from __future__ import annotations

import asyncio
import fnmatch
import json
import time
from collections import deque
from pathlib import Path

import regex

from workspace import WorkspaceKey, ENV_DIR_NAMES, WorkspaceManager
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier

from .common import (
    UserLocks,
    as_bool,
    clamped_int,
    scrub_user_paths,
    tool_error,
    workspace_activity,
)
from .config import WorkspaceToolConfig


def _in_env_dir(root: Path, candidate: Path) -> bool:
    """True if candidate lives under a regenerable env dir (.venv/.pio), which
    walks skip so their churn never floods glob/grep results."""
    try:
        parts = candidate.relative_to(root).parts
    except ValueError:
        return False
    return any(part in ENV_DIR_NAMES for part in parts)


class GrepTimeoutError(Exception):
    """A grep exceeded its regex match time budget. Distinct from the per-file
    ValueError skips so it aborts the whole walk instead of being swallowed."""


def register_search_tools(
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    config: WorkspaceToolConfig,
    locks: UserLocks,
) -> None:
    async def _grep_workspace(args: dict, ctx: MessageContext) -> str:
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return tool_error("pattern is required")
        if len(pattern) > config.max_grep_pattern_chars:
            return tool_error(
                f"pattern must be {config.max_grep_pattern_chars} characters or fewer"
            )
        try:
            max_results = clamped_int(
                args.get("max_results"),
                name="max_results",
                default=config.default_grep_results,
                minimum=1,
                maximum=config.max_grep_results,
            )
            context = clamped_int(
                args.get("context"),
                name="context",
                default=0,
                minimum=0,
                maximum=config.max_grep_context,
            )
            use_regex = as_bool(args.get("regex"), name="regex", default=False)
        except ValueError as e:
            return tool_error(str(e))
        # A member-supplied regex can trigger catastrophic backtracking. The real
        # bound is the per-match `timeout` honored by the `regex` engine during the
        # walk (it releases the GIL, so a hostile pattern cannot pin the event loop).
        # looks_catastrophic stays as a cheap fast-reject for the obvious shapes so
        # they fail instantly instead of burning the whole time budget first.
        if use_regex and looks_catastrophic(pattern):
            return tool_error(
                "Regex rejected: a group under a quantifier may not contain '|' or "
                "another quantifier (shapes like (a|b)+ or (a+)+). Drop the outer "
                "quantifier, or run one alternative per search."
            )
        try:
            matcher = regex.compile(
                pattern if use_regex else regex.escape(pattern),
                regex.IGNORECASE,
            )
        except regex.error as e:
            return tool_error(f"Invalid regex pattern: {e}")
        try:
            async with workspace_activity(locks, ctx):
                root = workspace_manager.resolve_user_file_path(
                    ctx.workspace_key,
                    str(args.get("path", "")).strip(),
                    allow_root=True,
                    must_exist=bool(args.get("path")),
                )
                # Keep resolution and the complete host-side walk in one lease;
                # another workspace tool for this owner cannot swap paths underneath it.
                result = await asyncio.to_thread(
                    run_grep_walk,
                    root,
                    matcher,
                    workspace_manager,
                    ctx.workspace_key,
                    context=context,
                    max_results=max_results,
                    max_text_chars=config.max_text_chars,
                    max_line_chars=config.max_grep_line_chars,
                    match_timeout=config.grep_timeout_seconds,
                )
            # A regex-looking pattern searched literally and matching nothing is
            # the classic silent wrong answer ("error|warning" finds neither);
            # tell the model how to get what it meant.
            if not use_regex and not result.get("matches") and regex.escape(pattern) != pattern:
                result["hint"] = (
                    "pattern was matched as literal text; pass regex: true if you "
                    "meant a regular expression"
                )
            return json.dumps(result)
        except GrepTimeoutError:
            return tool_error(
                "Search exceeded its time budget; simplify the pattern or narrow the path."
            )
        except Exception as e:
            return tool_error(scrub_user_paths(str(e), workspace_manager, ctx.workspace_key))

    registry.register(
        name="grep_workspace",
        description=(
            "Search workspace text files case-insensitively by literal text or an "
            "explicit regex. Files too large to read or that are not UTF-8 text are "
            "listed under 'skipped' with a reason rather than silently ignored."
        ),
        parameters={
            "type": "object",
            "properties": _grep_workspace_properties(),
            "required": ["pattern"],
        },
        handler=_grep_workspace,
        min_tier=TrustTier.MEMBER,
        untrusted=True,
    )

    async def _glob_workspace(args: dict, ctx: MessageContext) -> str:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return tool_error("pattern is required")
        if len(pattern) > config.max_grep_pattern_chars:
            return tool_error(
                f"pattern must be {config.max_grep_pattern_chars} characters or fewer"
            )
        try:
            max_results = clamped_int(
                args.get("max_results"),
                name="max_results",
                default=config.glob_max_results,
                minimum=1,
                maximum=config.glob_max_results,
            )
        except ValueError as e:
            return tool_error(str(e))
        try:
            async with workspace_activity(locks, ctx):
                root = workspace_manager.resolve_user_file_path(
                    ctx.workspace_key,
                    str(args.get("path", "")).strip(),
                    allow_root=True,
                    must_exist=bool(args.get("path")),
                )
                result = await asyncio.to_thread(
                    run_glob_walk,
                    root,
                    pattern,
                    workspace_manager,
                    ctx.workspace_key,
                    max_results=max_results,
                )
            return json.dumps(result)
        except Exception as e:
            return tool_error(scrub_user_paths(str(e), workspace_manager, ctx.workspace_key))

    registry.register(
        name="glob_workspace",
        description=(
            "Find files in your workspace by name using a glob pattern "
            "(e.g. '*.py', 'notes/*.md', or a bare 'config.json'). Matches the "
            "workspace-relative path and the file name case-insensitively, and "
            "returns matching file paths, not their contents. Use grep_workspace "
            "to search inside files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Glob pattern. '*' matches any characters including '/', "
                        "so '*.py' finds .py files at any depth. '**' is never "
                        "needed. A bare name like 'notes.txt' matches that file "
                        "anywhere in the workspace."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Workspace-relative directory to search within. "
                        "Defaults to the whole workspace."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of file paths to return.",
                },
            },
            "required": ["pattern"],
        },
        handler=_glob_workspace,
        min_tier=TrustTier.MEMBER,
    )


_QUANTIFIER_STARTS = frozenset("*+{")
_INNER_RISK_CHARS = frozenset("|*+?{")


def looks_catastrophic(pattern: str) -> bool:
    """Reject the exponential-backtracking regex families before compiling.

    Public because the coding subagent's private grep applies the same guard;
    workspace isolation bounds what can be *read*, not the CPU a hostile pattern
    burns, so both untrusted entry points share this check.

    The check is paren-aware: for every quantifier that applies to a group, it
    inspects that group's full body (at any nesting depth) and refuses when the
    body itself contains an alternation or another quantifier, the
    "quantifier over alternation/quantifier" shape behind both classic evil
    families ((a+)+, (.*)*, (a|aa)+, ((a|aa))+). It intentionally over-rejects
    some benign patterns ((abc)+ is allowed, but (abc|def)+ is refused): for
    untrusted input that is the safe direction. It is necessary but not
    sufficient: the walk is still offloaded to a worker thread and bounded by
    max_results/max_text_chars, so it is one layer, not the whole defense.
    """
    open_stack: list[int] = []  # index just after each unmatched '('
    escaped = False
    for index, char in enumerate(pattern):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "(":
            open_stack.append(index + 1)
        elif char == ")":
            if not open_stack:
                continue
            body_start = open_stack.pop()
            nxt = _next_significant(pattern, index + 1)
            if nxt in _QUANTIFIER_STARTS and _risky_group_body(pattern[body_start:index]):
                return True
    return False


def _next_significant(pattern: str, start: int) -> str:
    return pattern[start] if start < len(pattern) else ""


def _risky_group_body(body: str) -> bool:
    # An alternation or any quantifier inside the just-quantified group is what
    # turns the outer quantifier catastrophic. Skip escaped metachars so a
    # literal \+ or \| in the body does not trip the guard.
    escaped = False
    for char in body:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in _INNER_RISK_CHARS:
            return True
    return False


def run_grep_walk(
    root: Path,
    matcher: regex.Pattern[str],
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
    *,
    context: int,
    max_results: int,
    max_text_chars: int,
    max_line_chars: int,
    match_timeout: float,
) -> dict[str, object]:
    """Synchronous walk+read worker, run in a thread off the event loop.

    Public so the coding subagent's private grep tool reuses the same bounded
    walk instead of growing a second implementation.

    match_timeout bounds the total regex matching wall-clock across the whole walk;
    a single catastrophic pattern raises GrepTimeoutError instead of pinning the
    thread (and, because the regex engine holds the GIL, the whole event loop).
    """
    deadline = time.monotonic() + match_timeout
    candidates = [root] if root.is_file() else sorted(root.rglob("*"))
    matches: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    output_chars = 0
    truncated = False
    for candidate in candidates:
        if len(matches) >= max_results or output_chars >= max_text_chars:
            truncated = True
            break
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if _in_env_dir(root, candidate):
            continue
        rel = workspace_manager.relative_user_file_path(workspace_key, candidate)
        try:
            file_matches, capped = _grep_file(
                candidate,
                matcher,
                context=context,
                max_matches=max_results - len(matches),
                max_line_chars=max_line_chars,
                deadline=deadline,
            )
        except ValueError as e:
            skipped.append({"file": rel, "reason": str(e)})
            continue
        truncated = truncated or capped
        for item in file_matches:
            item["file"] = rel
            matches.append(item)
            output_chars += _grep_item_chars(item)
            if output_chars >= max_text_chars:
                truncated = True
                break
    result: dict[str, object] = {"matches": matches, "count": len(matches)}
    if truncated:
        result["truncated"] = True
    if skipped:
        result["skipped"] = skipped
    return result


def run_glob_walk(
    root: Path,
    pattern: str,
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
    *,
    max_results: int,
) -> dict[str, object]:
    """Synchronous name-glob walk, run in a thread off the event loop.

    Mirrors run_grep_walk's bounded rglob + per-entry symlink skip, but matches
    file *names* (case-insensitive fnmatch over the workspace-relative posix
    path, and over the bare basename) instead of reading file contents. rglob
    does not descend into symlinked directories, and relative_user_file_path
    re-checks containment on every result; the per-entry symlink skip is
    defense-in-depth so a symlinked file entry never surfaces a path that read
    tools would refuse anyway.
    """
    pattern_lower = pattern.lower()
    # fnmatch's '*' already spans '/', but the ubiquitous '**/*.py' habit would
    # silently miss root-level files (their relative path has no '/'); strip the
    # redundant prefix instead of punishing it.
    if pattern_lower.startswith("**/"):
        pattern_lower = pattern_lower[3:]
    candidates = (root,) if root.is_file() else root.rglob("*")
    matches: list[str] = []
    truncated = False
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if _in_env_dir(root, candidate):
            continue
        rel = workspace_manager.relative_user_file_path(workspace_key, candidate)
        rel_lower = rel.lower()
        if fnmatch.fnmatchcase(rel_lower, pattern_lower) or fnmatch.fnmatchcase(
            Path(rel_lower).name, pattern_lower
        ):
            matches.append(rel)
            if len(matches) >= max_results:
                truncated = True
                break
    result: dict[str, object] = {"matches": matches, "count": len(matches)}
    if truncated:
        result["truncated"] = True
    return result


def _grep_workspace_properties() -> dict[str, dict[str, object]]:
    return {
        "pattern": {
            "type": "string",
            "description": "Text or regex pattern to search for.",
        },
        "path": {
            "type": "string",
            "description": "Workspace-relative file or directory.",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum matches to return.",
        },
        "context": {
            "type": "integer",
            "description": "Lines of surrounding context to include before and after each match.",
        },
        "regex": {
            "type": "boolean",
            "description": "Treat pattern as a regex when true.",
        },
    }


def _clip_line(text: str, max_line_chars: int) -> str:
    if len(text) <= max_line_chars:
        return text
    return text[:max_line_chars] + f"…[+{len(text) - max_line_chars} chars]"


def _grep_item_chars(item: dict[str, object]) -> int:
    total = len(str(item.get("text", "")))
    for key in ("before", "after"):
        lines = item.get(key)
        if isinstance(lines, list):
            total += sum(len(str(line)) for line in lines)
    return total


def _search_bounded(
    matcher: regex.Pattern[str], line: str, deadline: float
) -> regex.Match[str] | None:
    """Run one search under the remaining walk budget. The regex engine honors the
    timeout mid-match and releases the GIL, so a catastrophic line cannot pin the
    thread; an exhausted budget or a timed-out match aborts the whole grep."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise GrepTimeoutError("grep match budget exhausted")
    try:
        return matcher.search(line, timeout=remaining)
    except TimeoutError as exc:
        raise GrepTimeoutError("grep match timed out") from exc


def _grep_file(
    path: Path,
    matcher: regex.Pattern[str],
    *,
    context: int,
    max_matches: int,
    max_line_chars: int,
    deadline: float,
) -> tuple[list[dict[str, object]], bool]:
    results: list[dict[str, object]] = []
    before: deque[str] = deque(maxlen=context)
    open_after: list[tuple[dict[str, object], int]] = []
    capped = False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if "\x00" in raw:
                    raise ValueError("Binary or non-UTF-8 file cannot be searched")
                line = raw.rstrip("\r\n")
                if open_after:
                    still_open: list[tuple[dict[str, object], int]] = []
                    for pending, remaining in open_after:
                        pending_after = pending["after"]
                        assert isinstance(pending_after, list)
                        pending_after.append(_clip_line(line, max_line_chars))
                        if remaining > 1:
                            still_open.append((pending, remaining - 1))
                    open_after = still_open
                if not capped and _search_bounded(matcher, line, deadline):
                    item: dict[str, object] = {
                        "line_number": line_number,
                        "text": _clip_line(line, max_line_chars),
                    }
                    if context:
                        item["before"] = [_clip_line(b, max_line_chars) for b in before]
                        item["after"] = []
                        open_after.append((item, context))
                    results.append(item)
                    if len(results) >= max_matches:
                        capped = True
                before.append(line)
                if capped and not open_after:
                    break
    except UnicodeDecodeError as e:
        raise ValueError("Binary or non-UTF-8 file cannot be searched") from e
    except OSError as e:
        raise ValueError(f"Could not read file: {e}") from e
    return results, capped
