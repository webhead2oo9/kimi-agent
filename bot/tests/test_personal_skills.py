from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills.loader import SKILL_FILENAME
from skills.personal import PersonalSkillManager
from tools.browse import init_browse_tools
from tools.personal_skills import init_personal_skill_tools
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


def _ctx(
    *,
    user_id: str = "123",
    trust_tier: TrustTier = TrustTier.MEMBER,
    activated: set[str] | None = None,
) -> MessageContext:
    return MessageContext(
        user_id=user_id,
        user_name="Alice",
        guild_id="999",
        channel_id="100",
        thread_id=None,
        trust_tier=trust_tier,
        activated_tools=activated or set(),
    )


def test_personal_skill_manager_round_trip_and_read_paths_do_not_create_dirs(
    tmp_path: Path,
) -> None:
    manager = PersonalSkillManager(tmp_path / "personal")

    assert manager.index("123") == ""
    assert manager.get("123", "missing") is None
    assert not (tmp_path / "personal" / "123").exists()

    err = manager.create(
        "123",
        name="quest-setup",
        description="Remember my Quest setup steps.",
        content="Use these steps when I ask about my headset.",
        tags=["vr"],
    )
    assert err is None

    skill = manager.get("123", "quest-setup")
    assert skill is not None
    assert skill.meta.name == "quest-setup"
    assert skill.meta.description == "Remember my Quest setup steps."
    assert skill.meta.tags == ["vr"]
    assert "Use these steps" in skill.content
    assert "- **quest-setup**: Remember my Quest setup steps. [vr]" in manager.index("123")

    err = manager.edit(
        "123",
        name="quest-setup",
        description="Updated setup steps.",
        content="Updated content.",
    )
    assert err is None
    updated = manager.get("123", "quest-setup")
    assert updated is not None
    assert updated.meta.description == "Updated setup steps."
    assert updated.content == "Updated content."

    err = manager.delete("123", "quest-setup")
    assert err is None
    assert manager.get("123", "quest-setup") is None


def test_personal_skill_manager_isolates_users_and_rejects_invalid_user_ids(
    tmp_path: Path,
) -> None:
    manager = PersonalSkillManager(tmp_path / "personal")

    err = manager.create(
        "123",
        name="quest-setup",
        description="A user's setup.",
        content="Private user instructions.",
    )
    assert err is None

    assert manager.get("456", "quest-setup") is None
    assert manager.index("456") == ""
    assert "quest-setup" in manager.index("123")

    err = manager.create(
        "../456",
        name="evil",
        description="Bad",
        content="Bad",
    )
    assert err == "User id must be a Discord snowflake"
    assert not (tmp_path / "456").exists()


def test_personal_skill_manager_rejects_trailing_newline_in_user_id(tmp_path: Path) -> None:
    manager = PersonalSkillManager(tmp_path / "personal")
    error = manager.create("123\n", name="notes", description="Notes", content="Body")
    assert error == "User id must be a Discord snowflake"
    assert not manager.base_dir.exists()
    with pytest.raises(ValueError, match="Discord snowflake"):
        manager.get("123\n", "notes")


def test_personal_skill_manager_reuses_skill_content_guards(tmp_path: Path) -> None:
    manager = PersonalSkillManager(tmp_path / "personal")

    err = manager.create(
        "123",
        name="tooled",
        description="Bad tool metadata.",
        content="---\ntools: []\n---\nbody",
    )

    assert err is not None
    assert "Executable tool metadata" in err


@pytest.mark.asyncio
async def test_personal_skill_tools_are_user_scoped_and_hidden_until_loaded(
    tmp_path: Path,
) -> None:
    manager = PersonalSkillManager(tmp_path / "personal")
    registry = ToolRegistry()
    init_browse_tools(registry)
    init_personal_skill_tools(registry, manager)

    member_core = {schema["name"] for schema in registry.get_tool_schemas(TrustTier.MEMBER)}
    assert "my_skill_get" in member_core
    assert "my_skill_create" not in member_core

    member_catalog = {entry.name for entry in registry.catalog(TrustTier.MEMBER)}
    assert {"my_skill_create", "my_skill_edit", "my_skill_delete"} <= member_catalog
    assert "my_skill_get" not in member_catalog

    ctx = _ctx()
    blocked = json.loads(
        await registry.dispatch(
            "my_skill_create",
            {
                "name": "quest-setup",
                "description": "Setup steps",
                "content": "Pair controllers, then check Wi-Fi.",
            },
            ctx,
        )
    )
    assert "browse_tools" in blocked["error"]

    loaded = json.loads(await registry.dispatch("browse_tools", {"load": ["my_skill_create"]}, ctx))
    assert loaded["loaded"] == ["my_skill_create"]

    created = json.loads(
        await registry.dispatch(
            "my_skill_create",
            {
                "name": "quest-setup",
                "description": "Setup steps",
                "content": "Pair controllers, then check Wi-Fi.",
                "tags": ["vr"],
            },
            ctx,
        )
    )
    assert created["result"] == "Personal skill 'quest-setup' created."

    loaded_skill = await registry.dispatch(
        "my_skill_get",
        {"name": "quest-setup"},
        _ctx(user_id="123"),
    )
    assert "# Personal Skill: quest-setup" in loaded_skill
    assert "Pair controllers" in loaded_skill

    other_user = json.loads(
        await registry.dispatch(
            "my_skill_get",
            {"name": "quest-setup"},
            _ctx(user_id="456"),
        )
    )
    assert other_user["error"] == "Personal skill 'quest-setup' not found"

    assert (tmp_path / "personal" / "123" / "quest-setup" / SKILL_FILENAME).is_file()
    assert not (tmp_path / "personal" / "456").exists()
