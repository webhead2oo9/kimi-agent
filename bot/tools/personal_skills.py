from __future__ import annotations

import json

from skills.loader import Skill
from skills.personal import PersonalSkillManager
from tools._common import tool_error
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


def init_personal_skill_tools(
    registry: ToolRegistry,
    manager: PersonalSkillManager,
) -> None:
    registry.register(
        name="my_skill_get",
        description=(
            "Load one of your own personal instruction skills by name. Use this "
            "after the Your Personal Skills prompt section shows a relevant skill."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The personal skill name in kebab-case.",
                },
            },
            "required": ["name"],
        },
        handler=lambda args, ctx: _my_skill_get(args, ctx, manager),
        min_tier=TrustTier.MEMBER,
    )

    registry.register(
        name="my_skill_create",
        description=(
            "Create your own personal instruction skill, saved only for the "
            "current user, when they explicitly ask you to save a reusable "
            "procedure or preference as a personal skill."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Personal skill name in kebab-case.",
                },
                "description": {
                    "type": "string",
                    "description": "One-line description of what this personal skill teaches.",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown instructions to save for this user's future turns.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization.",
                },
            },
            "required": ["name", "description", "content"],
        },
        handler=lambda args, ctx: _my_skill_create(args, ctx, manager),
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Personal Skills",
    )

    registry.register(
        name="my_skill_edit",
        description=(
            "Edit one of your own personal instruction skills when the current "
            "user explicitly asks to update it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The personal skill name to edit.",
                },
                "content": {
                    "type": "string",
                    "description": "Replacement markdown instructions.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional updated one-line description.",
                },
            },
            "required": ["name", "content"],
        },
        handler=lambda args, ctx: _my_skill_edit(args, ctx, manager),
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Personal Skills",
    )

    registry.register(
        name="my_skill_delete",
        description=(
            "Delete one of your own personal instruction skills when the current "
            "user explicitly asks to remove it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The personal skill name to delete.",
                },
            },
            "required": ["name"],
        },
        handler=lambda args, ctx: _my_skill_delete(args, ctx, manager),
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Personal Skills",
    )


async def _my_skill_create(
    args: dict,
    ctx: MessageContext,
    manager: PersonalSkillManager,
) -> str:
    name = str(args.get("name", ""))
    description = str(args.get("description", ""))
    content = str(args.get("content", ""))
    tags = args.get("tags")
    if tags is not None and not isinstance(tags, list):
        return tool_error("tags must be an array of strings")
    normalized_tags = [str(tag) for tag in tags] if tags is not None else None

    err = manager.create(
        ctx.user_id,
        name=name,
        description=description,
        content=content,
        tags=normalized_tags,
    )
    if err:
        return tool_error(err)
    return json.dumps({"result": f"Personal skill '{name}' created."})


async def _my_skill_edit(
    args: dict,
    ctx: MessageContext,
    manager: PersonalSkillManager,
) -> str:
    name = str(args.get("name", ""))
    content = str(args.get("content", ""))
    description = args.get("description")
    err = manager.edit(
        ctx.user_id,
        name=name,
        content=content,
        description=str(description) if description is not None else None,
    )
    if err:
        return tool_error(err)
    return json.dumps({"result": f"Personal skill '{name}' updated."})


async def _my_skill_delete(
    args: dict,
    ctx: MessageContext,
    manager: PersonalSkillManager,
) -> str:
    name = str(args.get("name", ""))
    err = manager.delete(ctx.user_id, name)
    if err:
        return tool_error(err)
    return json.dumps({"result": f"Personal skill '{name}' deleted."})


async def _my_skill_get(
    args: dict,
    ctx: MessageContext,
    manager: PersonalSkillManager,
) -> str:
    name = str(args.get("name", ""))
    if not name:
        return tool_error("Personal skill name is required")
    try:
        skill = manager.get(ctx.user_id, name)
    except ValueError as exc:
        return tool_error(str(exc))
    if skill is None:
        return tool_error(f"Personal skill '{name}' not found")
    return _render_personal_skill_for_model(skill)


def _render_personal_skill_for_model(skill: Skill) -> str:
    sections = [
        f"# Personal Skill: {skill.meta.name}",
        "",
        f"Description: {skill.meta.description}",
    ]
    if skill.meta.tags:
        sections.extend(["", f"Tags: {', '.join(skill.meta.tags)}"])
    sections.extend(["", "---", "", skill.content])
    return "\n".join(sections)
