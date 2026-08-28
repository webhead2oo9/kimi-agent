from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import regex

from skills import loader, manager
from skills.admin import SkillAdminError, SkillAdminService
from tools._common import tool_error
from tools.learn import (
    SCOPE_ALL_GUILDS,
    SCOPE_THIS_GUILD,
    SINK_SKILL,
    LearnEvent,
    LearnHook,
    emit_learn_event,
    jump_url,
)
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier

log = logging.getLogger(__name__)

_on_skills_changed: Callable[[], None] | None = None
_registry: ToolRegistry | None = None
_skill_admin_service: SkillAdminService | None = None
_skill_catalog: loader.SharedSkillCatalog | None = None
_on_learn: LearnHook | None = None

_SKILL_FILE_TOOL = "skill_file"
_SKILL_FILE_MAX_READ_CHARS = 120_000
_SKILL_FILE_MAX_MATCHES = 40
_SKILL_FILE_MAX_LINE_CHARS = 300
_SKILL_FILE_MAX_PATTERN_CHARS = 256
_SKILL_FILE_GREP_TIMEOUT_SECONDS = 2.0
_AUDIT_MAX_EDITS = 5


def init_skill_tools(
    registry: ToolRegistry,
    *,
    on_skills_changed: Callable[[], None] | None = None,
    skill_admin_service: SkillAdminService | None = None,
    skill_catalog: loader.SharedSkillCatalog | None = None,
    on_learn: LearnHook | None = None,
) -> None:
    global _on_skills_changed, _registry, _skill_admin_service, _skill_catalog, _on_learn
    _registry = registry
    _on_skills_changed = on_skills_changed
    _on_learn = on_learn
    # The store the service points at is settled here and never re-derived:
    # SKILLS_DIR can be relocated outside the checkout, so rebuilding a service
    # from the module global later would silently retarget a different store.
    _skill_admin_service = skill_admin_service or SkillAdminService(
        manager.SKILLS_DIR,
        on_skills_changed=on_skills_changed,
    )
    _skill_catalog = skill_catalog or loader.SharedSkillCatalog(
        _skill_admin_service.skills_dir,
    )

    registry.register(
        name="skill_list",
        description=(
            "List all available skills the bot knows. Each skill teaches the bot "
            "how to handle a specific type of request."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=_skill_list,
        min_tier=TrustTier.MEMBER,
    )

    registry.register(
        name="load_skill",
        description=(
            "Load the full content of a skill by name. Use this when you need "
            "detailed instructions for handling a specific type of request. "
            "Call skill_list first to see available skills."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name (kebab-case, e.g. 'troubleshooting-guide').",
                },
            },
            "required": ["name"],
        },
        handler=_load_skill,
        min_tier=TrustTier.MEMBER,
    )

    registry.register(
        name=_SKILL_FILE_TOOL,
        description=(
            "Read or search the reference files bundled with a skill. Works only "
            "for skills whose load_skill output lists reference files. Pass "
            "pattern to search across the skill's reference files (add path to "
            "limit the search to one file), or pass path alone to read a file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "The skill name, as passed to load_skill.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Reference file path from the manifest, e.g. 'reference/signatures.md'."
                    ),
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Case-insensitive regex (invalid regex is searched as "
                        "literal text) to grep for."
                    ),
                },
            },
            "required": ["skill"],
        },
        handler=_skill_file,
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Skills",
    )

    registry.register(
        name="skill_create",
        description=(
            "Create a new skill. A skill is a markdown document that teaches the bot "
            "how to handle a specific type of request. Only Staff can create private "
            "skills; built-in names are reserved."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name in kebab-case (e.g. 'headset-comparison').",
                },
                "description": {
                    "type": "string",
                    "description": "One-line description of what this skill does.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The skill content in markdown. Should include: "
                        "when to use it, the approach/steps, and any key knowledge."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization.",
                },
            },
            "required": ["name", "description", "content"],
        },
        handler=_skill_create,
        min_tier=TrustTier.STAFF,
    )

    registry.register(
        name="skill_edit",
        description=(
            "Edit an existing private skill. Built-in skills are read-only. Only "
            "Staff can edit skills. Provide at most "
            "one of: content (replace the whole body; risks dropping unrelated "
            "content on long skills), edits (surgical old_string/new_string "
            "patches, like multi_edit, so prefer this for small changes), or "
            "append (add text to the end, e.g. a new section). Description can "
            "be updated by itself."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name to edit.",
                },
                "content": {
                    "type": "string",
                    "description": "Full replacement skill content in markdown.",
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "Surgical patches applied in order to the current body. Each "
                        "old_string must match exactly once unless replace_all is set."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {
                                "type": "string",
                                "description": "Exact text to replace.",
                            },
                            "new_string": {
                                "type": "string",
                                "description": "Replacement text.",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": (
                                    "Replace every occurrence instead of requiring a unique match."
                                ),
                            },
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
                "append": {
                    "type": "string",
                    "description": "Text to add to the end of the current body.",
                },
                "description": {
                    "type": "string",
                    "description": "Updated description (optional, keeps existing if not set).",
                },
            },
            "required": ["name"],
        },
        handler=_skill_edit,
        min_tier=TrustTier.STAFF,
    )

    registry.register(
        name="skill_delete",
        description=(
            "Delete a private skill permanently. Built-in skills are read-only. "
            "Only Staff can delete skills."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name to delete.",
                },
            },
            "required": ["name"],
        },
        handler=_skill_delete,
        min_tier=TrustTier.STAFF,
    )


def _active_registry() -> ToolRegistry:
    if _registry is None:
        raise RuntimeError("Skill tools are not initialized")
    return _registry


def _active_skill_admin() -> SkillAdminService:
    if _skill_admin_service is None:
        raise RuntimeError("Skill tools are not initialized")
    return _skill_admin_service


def _active_skill_catalog() -> loader.SharedSkillCatalog:
    if _skill_catalog is None:
        raise RuntimeError("Skill tools are not initialized")
    return _skill_catalog


async def _call_skill_admin(
    service_method: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a shared SkillAdminService mutation off the bot event loop."""
    _active_registry().bind_event_loop(asyncio.get_running_loop())
    return await asyncio.to_thread(service_method, *args, **kwargs)


async def _skill_list(args: dict, ctx: MessageContext) -> str:
    skills = [
        skill
        for skill in _active_skill_catalog().scan().values()
        if loader.skill_visible_in_guild(skill, ctx.guild_id)
    ]
    if not skills:
        return json.dumps({"result": "No skills available yet."})

    items = []
    for s in skills:
        item: dict = {"name": s.name, "description": s.description}
        item["source"] = s.origin.value
        item["read_only"] = s.origin is loader.SkillOrigin.BUILTIN or not _skill_mutable_in_guild(
            s, ctx.guild_id
        )
        if s.tags:
            item["tags"] = s.tags
        items.append(item)
    return json.dumps({"skills": items, "count": len(items)})


def _load_manageable_skill(name: object, ctx: MessageContext) -> loader.Skill | None:
    if not isinstance(name, str) or not name:
        return None
    skill = loader.load_skill(name, skills_dir=_active_skill_admin().skills_dir)
    if skill is None or not _skill_mutable_in_guild(skill.meta, ctx.guild_id):
        return None
    return skill


def _skill_mutable_in_guild(skill: loader.SkillMeta, guild_id: str | None) -> bool:
    """Limit Discord-side mutation to a skill owned by exactly this guild."""

    return guild_id is not None and skill.guild_ids == (guild_id,)


async def _load_skill(args: dict, ctx: MessageContext) -> str:
    name = args.get("name", "")
    if not name:
        return tool_error("Skill name is required")

    skill = _active_skill_catalog().load(name)
    # Mask guild-scoped skills outside their guilds (no existence leak), the same
    # way the index hides them; otherwise the prose loads cross-guild by name.
    if skill is None or not loader.skill_visible_in_guild(skill.meta, ctx.guild_id):
        return tool_error(f"Skill '{name}' not found")

    rendered = _render_skill_for_model(skill)
    manifest = _reference_manifest(skill, ctx)
    if manifest:
        rendered = f"{rendered}\n\n{manifest}"
    return rendered


def _reference_manifest(skill: loader.Skill, ctx: MessageContext) -> str:
    """Manifest of the skill's reference/ files, appended to load_skill output.

    Also activates the searchable skill_file tool for the conversation, the
    same rail browse_tools rides (agent/core.py picks up the activated_tools
    diff mid-turn and persists explicit loads). Omitted entirely when
    skill_file is not visible to this caller (e.g. operator-blocked), so the
    model is never pointed at a tool it cannot call.
    """
    files = loader.list_reference_files(skill.meta.path)
    if not files:
        return ""
    if (
        _registry is None
        or _registry.get_searchable_entry(
            _SKILL_FILE_TOOL, ctx.trust_tier, ctx.guild_id, ctx.blocked_tools
        )
        is None
    ):
        return ""
    ctx.activated_tools.add(_SKILL_FILE_TOOL)
    ctx.explicitly_loaded_tools.add(_SKILL_FILE_TOOL)
    lines = [f"- {path} ({_format_size(size)})" for path, size in files]
    return (
        "## Reference files\n\n"
        + "\n".join(lines)
        + "\n\nThe skill_file tool is now enabled for this conversation: call it "
        + f'with skill:"{skill.meta.name}" plus pattern to search these files, '
        + "or path to read one."
    )


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KB"


async def _skill_file(args: dict, ctx: MessageContext) -> str:
    skill_name = args.get("skill", "")
    if not isinstance(skill_name, str) or not skill_name:
        return tool_error("skill is required")

    skill = _active_skill_catalog().load(skill_name)
    # Same guild mask as load_skill: no existence leak outside the skill's guilds.
    if skill is None or not loader.skill_visible_in_guild(skill.meta, ctx.guild_id):
        return tool_error(f"Skill '{skill_name}' not found")

    files = loader.list_reference_files(skill.meta.path)
    if not files:
        return tool_error(f"Skill '{skill_name}' has no reference files")

    raw_path = args.get("path")
    raw_pattern = args.get("pattern")
    path = raw_path.strip() if isinstance(raw_path, str) else ""
    pattern = raw_pattern if isinstance(raw_pattern, str) else ""

    if pattern:
        if len(pattern) > _SKILL_FILE_MAX_PATTERN_CHARS:
            return tool_error(f"pattern must be at most {_SKILL_FILE_MAX_PATTERN_CHARS} characters")
        return await asyncio.to_thread(_grep_reference, skill, skill_name, files, path, pattern)
    if path:
        return await asyncio.to_thread(_read_reference, skill, skill_name, path)
    listing = ", ".join(name for name, _ in files)
    return tool_error(f"Provide pattern to search or path to read. Reference files: {listing}")


def _grep_reference(
    skill: loader.Skill,
    skill_name: str,
    files: list[tuple[str, int]],
    path: str,
    pattern: str,
) -> str:
    if path:
        resolved = loader.resolve_reference_file(skill.meta.path, path)
        if resolved is None:
            return tool_error(f"Reference file '{path}' not found in skill '{skill_name}'")
        targets = [(resolved.relative_to(skill.meta.path.parent).as_posix(), resolved)]
    else:
        targets = []
        for rel, _size in files:
            resolved = loader.resolve_reference_file(skill.meta.path, rel)
            if resolved is not None:
                targets.append((rel, resolved))

    try:
        matcher = regex.compile(pattern, regex.IGNORECASE)
    except regex.error:
        matcher = regex.compile(regex.escape(pattern), regex.IGNORECASE)

    matches: list[str] = []
    truncated = False
    deadline = time.monotonic() + _SKILL_FILE_GREP_TIMEOUT_SECONDS
    for rel, resolved in targets:
        try:
            stream = resolved.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line_no, line in enumerate(stream, start=1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return tool_error(
                        "Reference search timed out; use a simpler or more specific pattern."
                    )
                try:
                    matched = matcher.search(line, timeout=remaining)
                except TimeoutError:
                    return tool_error(
                        "Reference search timed out; use a simpler or more specific pattern."
                    )
                if not matched:
                    continue
                if len(matches) >= _SKILL_FILE_MAX_MATCHES:
                    truncated = True
                    break
                matches.append(f"{rel}:{line_no}: {line.strip()[:_SKILL_FILE_MAX_LINE_CHARS]}")
        if truncated:
            break

    if not matches:
        scope = f" in '{path}'" if path else ""
        return f"No matches for {pattern!r}{scope}."
    note = (
        f"\n(stopped at {_SKILL_FILE_MAX_MATCHES} matches, so narrow the pattern)"
        if truncated
        else ""
    )
    return f"Matches for {pattern!r} in skill '{skill_name}':\n" + "\n".join(matches) + note


def _read_reference(skill: loader.Skill, skill_name: str, path: str) -> str:
    resolved = loader.resolve_reference_file(skill.meta.path, path)
    if resolved is None:
        return tool_error(f"Reference file '{path}' not found in skill '{skill_name}'")
    try:
        with resolved.open(encoding="utf-8", errors="replace") as stream:
            text = stream.read(_SKILL_FILE_MAX_READ_CHARS + 1)
    except OSError:
        return tool_error(f"Failed to read reference file '{path}'")
    rel = resolved.relative_to(skill.meta.path.parent).as_posix()
    header = f"# {skill_name}: {rel}\n\n"
    if len(text) > _SKILL_FILE_MAX_READ_CHARS:
        return (
            header
            + text[:_SKILL_FILE_MAX_READ_CHARS]
            + f"\n\n[truncated at {_SKILL_FILE_MAX_READ_CHARS} characters; "
            + "use pattern to search the rest]"
        )
    return header + text


def _render_skill_for_model(skill: loader.Skill) -> str:
    sections = [
        f"# Skill: {skill.meta.name}",
        "",
        f"Description: {skill.meta.description}",
        f"Source: {skill.meta.origin.value}"
        + (" (read-only)" if skill.meta.origin is loader.SkillOrigin.BUILTIN else ""),
    ]
    if skill.meta.tags:
        sections.extend(["", f"Tags: {', '.join(skill.meta.tags)}"])
    sections.extend(["", "---", "", skill.content])
    return "\n".join(sections)


async def _skill_create(args: dict, ctx: MessageContext) -> str:
    name = args.get("name", "")
    description = args.get("description", "")
    content = args.get("content", "")
    tags = args.get("tags", [])
    # Scoped to the creating guild, transparently: the model gets no scoping
    # argument and stays unaware that other guilds exist. Created from a DM
    # there is no guild to scope to, so the skill stays global.
    #
    # Personal chat is guild-less by design, so it would fall into that global
    # branch and let a tier granted outside every guild publish a skill into all
    # of them. Refuse instead of widening scope. _PERSONAL_CHAT_BLOCKED_TOOLS
    # already hides this tool there; this is the fail-closed second layer, since
    # a shared skill is persistent injection if it is ever created wrongly.
    if ctx.personal_chat:
        return tool_error("Shared skills can only be managed from a server conversation.")
    guild_ids = [ctx.guild_id] if ctx.guild_id else None

    if _active_skill_catalog().is_builtin(name):
        return tool_error(f"Built-in skill '{name}' is read-only")

    try:
        service = _active_skill_admin()
        await _call_skill_admin(
            service.create,
            name=name,
            description=description,
            body=content,
            tags=tags,
            created_by=ctx.user_id,
            guild_ids=guild_ids,
        )
    except SkillAdminError as exc:
        return tool_error(exc.message)

    scope = SCOPE_THIS_GUILD if guild_ids else SCOPE_ALL_GUILDS
    await emit_learn_event(
        _on_learn,
        lambda: LearnEvent(
            sink=SINK_SKILL,
            action="created",
            guild_id=ctx.guild_id,
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            subject=name,
            # The body is what the bot will actually follow later, so the card
            # carries it rather than just the one-line description.
            summary=f"{description}\n\n{content}" if content else description,
            scope=scope,
            source_url=jump_url(ctx.guild_id, ctx.channel_id, ctx.trigger_discord_message_id),
        ),
    )
    return json.dumps({"result": f"Skill '{name}' created successfully."})


def _edit_summary(args: dict) -> str:
    """Describe the mutation an edit is about to make, for the audit card.

    A card saying only "Skill updated" is worthless for review: the whole point
    of the log is that someone can see what the bot was told to do without
    diffing the store by hand.
    """
    append = args.get("append")
    if isinstance(append, str) and append.strip():
        return f"Appended:\n{append.strip()}"

    edits = args.get("edits")
    if isinstance(edits, list) and edits:
        parts = []
        for edit in edits[:_AUDIT_MAX_EDITS]:
            if not isinstance(edit, dict):
                continue
            old = str(edit.get("old_string", ""))
            new = str(edit.get("new_string", ""))
            parts.append(f"- {old!r}\n  → {new!r}")
        if len(edits) > _AUDIT_MAX_EDITS:
            parts.append(f"- ... and {len(edits) - _AUDIT_MAX_EDITS} more")
        if parts:
            return "Patched:\n" + "\n".join(parts)

    content = args.get("content")
    if isinstance(content, str):
        return f"Replaced the whole body with:\n{content.strip()}"

    description = args.get("description")
    if isinstance(description, str) and description.strip():
        return f"Changed the description to: {description.strip()}"
    return ""


async def _skill_edit(args: dict, ctx: MessageContext) -> str:
    name = args.get("name", "")
    if not name:
        return tool_error("Skill name is required")
    if _active_skill_catalog().is_builtin(name):
        return tool_error(f"Built-in skill '{name}' is read-only")
    existing = _load_manageable_skill(name, ctx)
    if existing is None:
        return tool_error(f"Skill '{name}' not found")

    content = args.get("content")
    edits = args.get("edits")
    append = args.get("append")
    description = args.get("description")

    # Some tool-schema consumers materialize optional fields with empty values.
    # Treat those placeholders as omitted while preserving malformed, non-empty
    # values for the admin service to validate normally.
    if isinstance(content, str) and not content.strip():
        content = None
    if edits == []:
        edits = None
    if isinstance(append, str) and not append.strip():
        append = None
    if isinstance(description, str) and not description.strip():
        description = None

    try:
        service = _active_skill_admin()
        await _call_skill_admin(
            service.edit,
            name,
            body=content,
            edits=edits,
            append=append,
            description=description,
        )
    except SkillAdminError as exc:
        return tool_error(exc.message)

    await emit_learn_event(
        _on_learn,
        lambda: LearnEvent(
            sink=SINK_SKILL,
            action="updated",
            guild_id=ctx.guild_id,
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            subject=name,
            summary=_edit_summary(args),
            scope=SCOPE_THIS_GUILD if existing.meta.guild_ids else SCOPE_ALL_GUILDS,
            source_url=jump_url(ctx.guild_id, ctx.channel_id, ctx.trigger_discord_message_id),
        ),
    )
    return json.dumps({"result": f"Skill '{name}' updated successfully."})


async def _skill_delete(args: dict, ctx: MessageContext) -> str:
    name = args.get("name", "")
    if not name:
        return tool_error("Skill name is required")
    if _active_skill_catalog().is_builtin(name):
        return tool_error(f"Built-in skill '{name}' is read-only")
    if _load_manageable_skill(name, ctx) is None:
        return tool_error(f"Skill '{name}' not found")

    try:
        service = _active_skill_admin()
        await _call_skill_admin(service.delete, name)
    except SkillAdminError as exc:
        return tool_error(exc.message)
    return json.dumps({"result": f"Skill '{name}' deleted."})
