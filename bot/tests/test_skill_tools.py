import json
import threading
from pathlib import Path

import pytest

import skills.registration as registration
from workspace import WorkspaceManager
from skills.loader import SkillToolDeclaration
from skills.admin import SkillAdminError, SkillAdminService
from skills.registration import build_script_tool_handler, reload_all_skill_tools
from skills.runner import ScriptResult
from tools.learn import LearnEvent
from tools.registry import MessageContext, ToolRegistry
import tools.skills as skill_tools
from trust.tiers import TrustTier

SHIPPED_BUILTIN_NAMES = {
    "bot-info",
    "browser",
    "coding-work",
    "embed",
    "start-thread",
    "workspace",
}


def _staff_ctx(guild_id: str | None = "g1") -> MessageContext:
    return MessageContext(
        user_id="123",
        user_name="Tester",
        guild_id=guild_id,
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.STAFF,
    )


def _init_skill_registry(
    on_skills_changed=None,
) -> ToolRegistry:
    registry = ToolRegistry()
    skill_tools.init_skill_tools(registry, on_skills_changed=on_skills_changed)
    return registry


def _use_skill_store(store: Path, *, builtin_dir: Path | None = None) -> ToolRegistry:
    """Point the skill tools at `store`, the way the runtime does.

    Production injects the service in `app/tools.py`; tests used to instead
    monkeypatch `manager.SKILLS_DIR` and rely on `_active_skill_admin` noticing
    and rebuilding. Injecting here means the tests exercise the same wiring
    production uses.
    """

    catalog = skill_tools.loader.SharedSkillCatalog(
        store,
        builtin_dir or skill_tools.loader.BUILTIN_SKILLS_DIR,
    )
    registry = ToolRegistry()
    skill_tools.init_skill_tools(
        registry,
        skill_admin_service=SkillAdminService(
            store,
            reserved_names=catalog.reserved_names(),
        ),
        skill_catalog=catalog,
    )
    return registry


def _reload_skill_tools(store: Path, registry: ToolRegistry, secrets: dict[str, str]) -> None:
    reload_all_skill_tools(store, registry, secrets=secrets)


@pytest.mark.asyncio
async def test_shared_skill_mutations_run_admin_service_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread_id = threading.get_ident()
    service_thread_ids: dict[str, int] = {}

    class _TrackingRegistry(ToolRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.replacement_thread_ids: list[int] = []

        def replace_skill_tools(self, entries) -> None:
            self.replacement_thread_ids.append(threading.get_ident())
            super().replace_skill_tools(entries)

    registry = _TrackingRegistry()
    skill_tools.init_skill_tools(registry)

    class _TrackingService:
        @staticmethod
        def _record(method: str) -> None:
            service_thread_ids[method] = threading.get_ident()
            registry.replace_skill_tools_threadsafe([])

        def create(self, **kwargs) -> dict:
            self._record("create")
            return {}

        def edit(self, name: str, **kwargs) -> dict:
            self._record("edit")
            return {}

        def delete(self, name: str, **kwargs) -> None:
            self._record("delete")

    service = _TrackingService()
    monkeypatch.setattr(skill_tools, "_active_skill_admin", lambda: service)
    monkeypatch.setattr(
        skill_tools,
        "_load_manageable_skill",
        lambda name, ctx: object(),
    )

    await skill_tools._skill_create(
        {"name": "created", "description": "desc", "content": "Body."},
        _staff_ctx(),
    )
    await skill_tools._skill_edit(
        {"name": "edited", "content": "Updated body."},
        _staff_ctx(),
    )
    await skill_tools._skill_delete({"name": "deleted"}, _staff_ctx())

    assert set(service_thread_ids) == {"create", "edit", "delete"}
    assert all(thread_id != loop_thread_id for thread_id in service_thread_ids.values())
    assert registry.replacement_thread_ids == [loop_thread_id] * 3


@pytest.mark.asyncio
async def test_model_facing_skill_tools_use_the_injected_relocated_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Production wiring (app/tools.py): the runtime store is the operator
    # instance directory (SKILLS_DIR, outside the checkout because instance skills
    # are not repo-committed), injected via SkillAdminService. The model-facing
    # tools must scan THAT store, not the loader's packaged default; when they
    # scanned the default, instance-only skills appeared in the prompt index
    # but load_skill/skill_list reported them "not found" (2026-08-10).
    repo_store = tmp_path / "repo-store"
    (repo_store / "generic").mkdir(parents=True)
    (repo_store / "generic" / "SKILL.md").write_text(
        "---\nname: generic\ndescription: Repo-store skill\n---\n\nGeneric body.\n",
        encoding="utf-8",
    )
    instance_store = tmp_path / "instance-skills"
    (instance_store / "instance-only").mkdir(parents=True)
    (instance_store / "instance-only" / "SKILL.md").write_text(
        "---\nname: instance-only\ndescription: Operator instance skill\n---\n\nInstance body.\n",
        encoding="utf-8",
    )
    # The loader default points at the packaged repo store, the wrong answer
    # for any model-facing lookup in a relocated deployment.
    monkeypatch.setattr(skill_tools.loader, "SKILLS_DIR", repo_store)
    monkeypatch.setattr(skill_tools.manager, "SKILLS_DIR", repo_store)

    registry = ToolRegistry()
    skill_tools.init_skill_tools(
        registry,
        skill_admin_service=SkillAdminService(instance_store),
    )

    loaded = await skill_tools._load_skill({"name": "instance-only"}, _staff_ctx())
    assert "Instance body." in loaded

    listing = json.loads(await skill_tools._skill_list({}, _staff_ctx()))
    names = {item["name"] for item in listing["skills"]}
    assert names == SHIPPED_BUILTIN_NAMES | {"instance-only"}
    assert "generic" not in names

    # init_skill_tools keeps module-level state; hand back a default
    # (non-injected) service so later tests that never call init_skill_tools
    # do not inherit this test's relocated store.
    skill_tools.init_skill_tools(ToolRegistry())


@pytest.mark.asyncio
async def test_model_facing_tools_merge_and_protect_builtin_skills(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    shipped = builtin / "shipped"
    references = shipped / "reference"
    references.mkdir(parents=True)
    (shipped / "SKILL.md").write_text(
        "---\nname: shipped\ndescription: Built-in guidance\n---\n\nShipped body.\n",
        encoding="utf-8",
    )
    (references / "notes.md").write_text("Reference body.\n", encoding="utf-8")

    private = tmp_path / "external-private"
    local = private / "local"
    collision = private / "shipped"
    local.mkdir(parents=True)
    collision.mkdir()
    (local / "SKILL.md").write_text(
        "---\nname: local\ndescription: Private guidance\nguild_ids: [111]\n---\n\nLocal body.\n",
        encoding="utf-8",
    )
    (collision / "SKILL.md").write_text(
        "---\nname: shipped\ndescription: Shadow\n---\n\nShadow body.\n",
        encoding="utf-8",
    )
    registry = _use_skill_store(private, builtin_dir=builtin)

    listing = json.loads(await skill_tools._skill_list({}, _staff_ctx("111")))
    items = {item["name"]: item for item in listing["skills"]}
    assert items["shipped"]["source"] == "builtin"
    assert items["shipped"]["read_only"] is True
    assert items["local"]["source"] == "private"
    assert items["local"]["read_only"] is False

    ctx = _staff_ctx()
    loaded = await skill_tools._load_skill({"name": "shipped"}, ctx)
    assert "Source: builtin (read-only)" in loaded
    assert "Shipped body." in loaded
    assert "Shadow body." not in loaded
    assert "skill_file" in ctx.activated_tools
    reference = await skill_tools._skill_file(
        {"skill": "shipped", "path": "reference/notes.md"},
        ctx,
    )
    assert "Reference body." in reference

    create = await skill_tools._skill_create(
        {"name": "shipped", "description": "Replacement", "content": "Body."},
        _staff_ctx(),
    )
    edit = await skill_tools._skill_edit(
        {"name": "shipped", "append": "Change."},
        _staff_ctx(),
    )
    delete = await skill_tools._skill_delete({"name": "shipped"}, _staff_ctx())
    assert all("read-only" in result for result in (create, edit, delete))
    assert (collision / "SKILL.md").is_file()

    active = {t.name for t in registry.get_tools_for_tier(TrustTier.MEMBER, {"skill_file"})}
    assert "skill_file" in active


def test_skill_admin_service_rejects_reserved_names(tmp_path: Path) -> None:
    store = tmp_path / "private"
    skill_dir = store / "shipped"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: shipped\ndescription: Shadow\n---\n\nBody.\n",
        encoding="utf-8",
    )
    service = SkillAdminService(store, reserved_names=frozenset({"shipped"}))

    with pytest.raises(SkillAdminError, match="read-only"):
        service.create(name="shipped", description="Description", body="Body.")
    with pytest.raises(SkillAdminError, match="read-only"):
        service.edit("shipped", append="Change.")
    with pytest.raises(SkillAdminError, match="read-only"):
        service.delete("shipped")
    assert (skill_dir / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_load_skill_success_returns_model_readable_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "skills"
    skill_dir = store / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: test-skill
description: Test skill instructions
tags: [alpha, beta]
---

# Test Skill

Use real line breaks.
""",
        encoding="utf-8",
    )

    _use_skill_store(store)

    result = await skill_tools._load_skill({"name": "test-skill"}, _staff_ctx())

    with pytest.raises(json.JSONDecodeError):
        json.loads(result)
    assert result.startswith("# Skill: test-skill\n\n")
    assert "Description: Test skill instructions\n" in result
    assert "Tags: alpha, beta\n" in result
    assert "---\n\n# Test Skill\n\nUse real line breaks." in result


@pytest.mark.asyncio
async def test_skill_list_filters_guild_scoped_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "skills"
    home = store / "home-only"
    other = store / "other-only"
    global_skill = store / "global-skill"
    home.mkdir(parents=True)
    other.mkdir()
    global_skill.mkdir()
    (home / "SKILL.md").write_text(
        "---\nname: home-only\ndescription: Home\nguild_ids: [111]\n---\n\nHome.",
        encoding="utf-8",
    )
    (other / "SKILL.md").write_text(
        "---\nname: other-only\ndescription: Other\nguild_ids: [222]\n---\n\nOther.",
        encoding="utf-8",
    )
    (global_skill / "SKILL.md").write_text(
        "---\nname: global-skill\ndescription: Global\n---\n\nGlobal.",
        encoding="utf-8",
    )
    _use_skill_store(store)

    result = json.loads(await skill_tools._skill_list({}, _staff_ctx("111")))

    names = {item["name"] for item in result["skills"]}
    assert names == SHIPPED_BUILTIN_NAMES | {"home-only", "global-skill"}
    assert result["count"] == len(SHIPPED_BUILTIN_NAMES) + 2
    by_name = {item["name"]: item for item in result["skills"]}
    assert by_name["home-only"]["read_only"] is False
    assert by_name["global-skill"]["read_only"] is True


@pytest.mark.asyncio
async def test_skill_create_handler_defaults_guild_id_from_ctx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "skills"
    store.mkdir()
    _use_skill_store(store)

    ctx = MessageContext(
        user_id="123",
        user_name="Tester",
        guild_id="700000000000000100",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.STAFF,
    )
    result = await skill_tools._skill_create(
        {"name": "auto-scoped", "description": "desc", "content": "Body."},
        ctx,
    )

    assert "result" in json.loads(result)
    from skills.loader import load_skill

    skill = load_skill("auto-scoped", skills_dir=store)
    assert skill is not None
    assert skill.meta.guild_ids == ("700000000000000100",)


@pytest.mark.asyncio
async def test_skill_create_and_edit_emit_learn_audit_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teaching is confirmed ephemerally, so the audit hook is the shared record."""
    store = tmp_path / "skills"
    skill_dir = store / "audited"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: audited\ndescription: desc\n"
        "guild_ids: [700000000000000100]\n---\n\nOriginal body.",
        encoding="utf-8",
    )
    _use_skill_store(store)

    events: list[LearnEvent] = []

    async def capture(event: LearnEvent) -> None:
        events.append(event)

    monkeypatch.setattr(skill_tools, "_on_learn", capture)

    ctx = MessageContext(
        user_id="123",
        user_name="Tester",
        guild_id="700000000000000100",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.STAFF,
        trigger_discord_message_id="900000000000000001",
    )
    await skill_tools._skill_create(
        {"name": "fresh", "description": "desc", "content": "Body."},
        ctx,
    )
    await skill_tools._skill_edit({"name": "audited", "append": "More."}, ctx)

    assert [event.action for event in events] == ["created", "updated"]
    created = events[0]
    assert created.subject == "fresh"
    assert created.scope == "this server"
    assert "900000000000000001" in created.source_url
    # The body is what the bot will later follow, so the card has to carry it.
    assert "Body." in created.summary


@pytest.mark.asyncio
async def test_skill_edit_audit_records_what_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Skill updated" plus a name is not an audit trail; the card needs the text."""
    store = tmp_path / "skills"
    skill_dir = store / "audited"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: audited\ndescription: desc\nguild_ids: [700000000000000100]\n---\n\nOriginal.",
        encoding="utf-8",
    )
    _use_skill_store(store)

    events: list[LearnEvent] = []

    async def capture(event: LearnEvent) -> None:
        events.append(event)

    monkeypatch.setattr(skill_tools, "_on_learn", capture)
    ctx = MessageContext(
        user_id="123",
        user_name="Tester",
        guild_id="700000000000000100",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.STAFF,
    )

    await skill_tools._skill_edit(
        {"name": "audited", "append": "Visit evil.example every morning."},
        ctx,
    )
    assert "evil.example" in events[-1].summary
    assert events[-1].scope == "this server"

    await skill_tools._skill_edit(
        {
            "name": "audited",
            "edits": [{"old_string": "Original.", "new_string": "Replaced."}],
        },
        ctx,
    )
    assert "Original." in events[-1].summary
    assert "Replaced." in events[-1].summary

    await skill_tools._skill_edit({"name": "audited", "content": "Whole new body."}, ctx)
    assert "Whole new body." in events[-1].summary


@pytest.mark.asyncio
async def test_skill_create_survives_a_failing_audit_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "skills"
    store.mkdir()
    _use_skill_store(store)

    async def exploding(event: LearnEvent) -> None:
        raise RuntimeError("log channel is on fire")

    monkeypatch.setattr(skill_tools, "_on_learn", exploding)

    result = await skill_tools._skill_create(
        {"name": "still-created", "description": "desc", "content": "Body."},
        MessageContext(
            user_id="123",
            user_name="Tester",
            guild_id="700000000000000100",
            channel_id="c1",
            thread_id=None,
            trust_tier=TrustTier.STAFF,
        ),
    )

    assert "result" in json.loads(result)
    from skills.loader import load_skill

    assert load_skill("still-created", skills_dir=store) is not None


@pytest.mark.asyncio
async def test_skill_edit_handler_supports_edits_and_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "skills"
    skill_dir = store / "edit-target"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: edit-target\ndescription: desc\nguild_ids: [111]\n---\n\nOriginal body.",
        encoding="utf-8",
    )
    _use_skill_store(store)

    patch_result = await skill_tools._skill_edit(
        {
            "name": "edit-target",
            "edits": [{"old_string": "Original body.", "new_string": "Patched body."}],
        },
        _staff_ctx("111"),
    )
    assert "result" in json.loads(patch_result)

    append_result = await skill_tools._skill_edit(
        {"name": "edit-target", "append": "More info."},
        _staff_ctx("111"),
    )
    assert "result" in json.loads(append_result)

    from skills.loader import load_skill

    skill = load_skill("edit-target", skills_dir=store)
    assert skill is not None
    assert skill.content == "Patched body.\n\nMore info."


@pytest.mark.asyncio
async def test_skill_edit_ignores_empty_schema_placeholders(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    skill_dir = store / "edit-target"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: edit-target\ndescription: desc\nguild_ids: [111]\n---\n\nOriginal body.",
        encoding="utf-8",
    )
    _use_skill_store(store)

    patch_result = await skill_tools._skill_edit(
        {
            "name": "edit-target",
            "content": "",
            "edits": [{"old_string": "Original body.", "new_string": "Patched body."}],
            "append": "  ",
            "description": "",
        },
        _staff_ctx("111"),
    )
    assert "result" in json.loads(patch_result)

    replace_result = await skill_tools._skill_edit(
        {
            "name": "edit-target",
            "content": "Replacement body.",
            "edits": [],
            "append": "",
            "description": " ",
        },
        _staff_ctx("111"),
    )
    assert "result" in json.loads(replace_result)

    append_result = await skill_tools._skill_edit(
        {
            "name": "edit-target",
            "content": "\t",
            "edits": [],
            "append": "Appended body.",
            "description": "",
        },
        _staff_ctx("111"),
    )
    assert "result" in json.loads(append_result)

    from skills.loader import load_skill

    skill = load_skill("edit-target", skills_dir=store)
    assert skill is not None
    assert skill.content == "Replacement body.\n\nAppended body."
    assert skill.meta.description == "desc"


@pytest.mark.asyncio
async def test_skill_edit_placeholder_modes_allow_description_only_update(
    tmp_path: Path,
) -> None:
    store = tmp_path / "skills"
    skill_dir = store / "edit-target"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\nname: edit-target\ndescription: old\nguild_ids: [111]\n---\n\nOriginal body.",
        encoding="utf-8",
    )
    _use_skill_store(store)

    result = await skill_tools._skill_edit(
        {
            "name": "edit-target",
            "content": "",
            "edits": [],
            "append": "",
            "description": "new description",
        },
        _staff_ctx("111"),
    )

    assert "result" in json.loads(result)
    from skills.loader import load_skill

    skill = load_skill("edit-target", skills_dir=store)
    assert skill is not None
    assert skill.content == "Original body."
    assert skill.meta.description == "new description"


@pytest.mark.asyncio
async def test_skill_edit_rejects_meaningful_mode_conflicts_and_empty_noop(
    tmp_path: Path,
) -> None:
    store = tmp_path / "skills"
    skill_dir = store / "edit-target"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    original = "---\nname: edit-target\ndescription: desc\nguild_ids: [111]\n---\n\nOriginal body."
    skill_path.write_text(original, encoding="utf-8")
    _use_skill_store(store)

    conflict = await skill_tools._skill_edit(
        {
            "name": "edit-target",
            "content": "Replacement.",
            "edits": [],
            "append": "Appendix.",
        },
        _staff_ctx("111"),
    )
    assert json.loads(conflict) == {"error": "Provide at most one of content, edits, or append"}
    assert skill_path.read_text(encoding="utf-8") == original

    noop = await skill_tools._skill_edit(
        {
            "name": "edit-target",
            "content": "",
            "edits": [],
            "append": " ",
            "description": "",
        },
        _staff_ctx("111"),
    )
    assert json.loads(noop) == {"error": "No skill changes were provided"}
    assert skill_path.read_text(encoding="utf-8") == original


def test_skill_edit_schema_keeps_only_name_required() -> None:
    registry = _init_skill_registry()
    schema = next(
        schema
        for schema in registry.get_tool_schemas(TrustTier.STAFF)
        if schema["name"] == "skill_edit"
    )

    assert schema["parameters"]["required"] == ["name"]
    assert "Provide at most one" in schema["description"]


@pytest.mark.asyncio
async def test_skill_edit_masks_cross_guild_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "skills"
    skill_dir = store / "other-skill"
    skill_dir.mkdir(parents=True)
    original = "---\nname: other-skill\ndescription: Other\nguild_ids: [222]\n---\n\nOriginal body."
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(original, encoding="utf-8")
    _use_skill_store(store)

    result = await skill_tools._skill_edit(
        {"name": "other-skill", "content": "Patched."},
        _staff_ctx("111"),
    )

    assert json.loads(result) == {"error": "Skill 'other-skill' not found"}
    assert skill_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_skill_delete_masks_cross_guild_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "skills"
    skill_dir = store / "other-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: other-skill\ndescription: Other\nguild_ids: [222]\n---\n\nBody.",
        encoding="utf-8",
    )
    _use_skill_store(store)

    result = await skill_tools._skill_delete({"name": "other-skill"}, _staff_ctx("111"))

    assert json.loads(result) == {"error": "Skill 'other-skill' not found"}
    assert skill_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guild_ids",
    [None, "[111, 222]"],
    ids=["global", "multi-guild"],
)
async def test_skill_mutations_mask_non_guild_owned_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guild_ids: str | None,
) -> None:
    store = tmp_path / "skills"
    skill_dir = store / "shared-skill"
    skill_dir.mkdir(parents=True)
    scope = f"guild_ids: {guild_ids}\n" if guild_ids is not None else ""
    original = f"---\nname: shared-skill\ndescription: Shared\n{scope}---\n\nOriginal body."
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(original, encoding="utf-8")
    _use_skill_store(store)

    edit_result = await skill_tools._skill_edit(
        {"name": "shared-skill", "content": "Patched."},
        _staff_ctx("111"),
    )
    delete_result = await skill_tools._skill_delete(
        {"name": "shared-skill"},
        _staff_ctx("111"),
    )

    expected = {"error": "Skill 'shared-skill' not found"}
    assert json.loads(edit_result) == expected
    assert json.loads(delete_result) == expected
    assert skill_path.read_text(encoding="utf-8") == original


def _write_exec_skill(
    store: Path,
    dir_name: str,
    *,
    skill_name: str,
    tool_name: str,
    min_tier: str | None = None,
    bad_timeout: bool = False,
) -> None:
    skill_dir = store / dir_name
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    lines = [
        "---",
        f"name: {skill_name}",
        "description: desc",
        "tools:",
        f"  - name: {tool_name}",
        f"    description: does {tool_name}",
        "    availability: always",
        "    script: scripts/run.py",
    ]
    if min_tier is not None:
        lines.append(f"    min_tier: {min_tier}")
    if bad_timeout:
        lines.append("    timeout: not-a-number")
    lines += ["---", "", "# body", ""]
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


def test_register_all_skips_malformed_skill_keeps_valid(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _write_exec_skill(store, "a-good", skill_name="a-good", tool_name="good_tool")
    _write_exec_skill(store, "b-bad", skill_name="b-bad", tool_name="bad_tool", bad_timeout=True)
    registry = ToolRegistry()

    reload_all_skill_tools(store, registry, secrets={})  # must not raise

    assert registry.has_tool("good_tool")
    assert not registry.has_tool("bad_tool")


def test_register_all_skips_cross_skill_name_collision(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _write_exec_skill(store, "a-first", skill_name="a-first", tool_name="honk")
    _write_exec_skill(store, "b-second", skill_name="b-second", tool_name="honk")
    _write_exec_skill(store, "c-third", skill_name="c-third", tool_name="beep")
    registry = ToolRegistry()

    reload_all_skill_tools(store, registry, secrets={})  # must not raise

    assert registry.has_tool("honk")  # first by sorted dir order wins
    assert registry.has_tool("beep")  # a later distinct skill still registers


@pytest.mark.asyncio
async def test_script_output_files_respect_attachment_cap_and_report_drops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)

    async def _fake_run_script(**kwargs):
        workspace_dir = Path(kwargs["workspace_dir"])
        outputs = []
        for name in ("a.png", "b.png"):
            f = workspace_dir / name
            f.write_bytes(b"x")
            outputs.append(str(f))
        return ScriptResult(
            stdout="ok",
            stderr="",
            return_code=0,
            output_files=outputs,
        )

    monkeypatch.setattr(registration, "run_script", _fake_run_script)
    monkeypatch.setattr(registration.settings, "workspace_tool_max_attachments", 1)
    handler = build_script_tool_handler(
        SkillToolDeclaration(
            name="chart",
            description="makes charts",
            availability="always",
            script="scripts/run.py",
        ),
        source_dir=tmp_path,
        resolved_secrets={},
        workspace_manager=manager,
    )
    ctx = _staff_ctx()

    payload = json.loads(await handler({}, ctx))

    assert payload["attached_files"] == ["a.png"]
    assert payload["attached_file_refs"] == [{"file": "a.png", "remove_id": "attachment:1"}]
    assert [d["file"] for d in payload["files_not_attached"]] == ["b.png"]
    assert "attachment limit" in payload["files_not_attached"][0]["reason"]
    assert len(ctx.output_files) == 1
    assert ctx.output_files[0].endswith("a.png")


@pytest.mark.asyncio
async def test_script_handler_passes_explicit_network_policy_and_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def _fake_run_script(**kwargs):
        seen.update(kwargs)
        return ScriptResult(stdout="ok", stderr="", return_code=0)

    monkeypatch.setattr(registration, "run_script", _fake_run_script)
    handler = build_script_tool_handler(
        SkillToolDeclaration(
            name="fetch",
            description="fetches data",
            availability="always",
            script="scripts/run.py",
            network=True,
        ),
        source_dir=tmp_path,
        resolved_secrets={},
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "workspaces"),
    )

    assert await handler({}, _staff_ctx()) == "ok"
    assert seen["allow_network"] is True
    limits = seen["sandbox_limits"]
    assert isinstance(limits, registration.ScriptSandboxLimits)
    assert limits.memory_bytes == registration.settings.script_sandbox_memory_max_mb * 1024 * 1024


@pytest.mark.asyncio
async def test_script_duplicate_output_basenames_expose_unique_remove_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)

    async def _fake_run_script(**kwargs):
        workspace_dir = Path(kwargs["workspace_dir"])
        outputs = []
        for parent in ("first", "second"):
            output = workspace_dir / parent / "chart.png"
            output.parent.mkdir()
            output.write_bytes(b"x")
            outputs.append(str(output))
        return ScriptResult(
            stdout="ok",
            stderr="",
            return_code=0,
            output_files=outputs,
        )

    monkeypatch.setattr(registration, "run_script", _fake_run_script)
    monkeypatch.setattr(registration.settings, "workspace_tool_max_attachments", 2)
    handler = build_script_tool_handler(
        SkillToolDeclaration(
            name="chart",
            description="makes charts",
            availability="always",
            script="scripts/run.py",
        ),
        source_dir=tmp_path,
        resolved_secrets={},
        workspace_manager=manager,
    )
    ctx = _staff_ctx()

    raw_payload = await handler({}, ctx)
    payload = json.loads(raw_payload)

    assert payload["attached_files"] == ["chart.png", "chart.png"]
    assert [ref["file"] for ref in payload["attached_file_refs"]] == [
        "chart.png",
        "chart.png",
    ]
    assert len({ref["remove_id"] for ref in payload["attached_file_refs"]}) == 2
    assert str(tmp_path) not in raw_payload


def test_register_all_skips_bad_min_tier(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    _write_exec_skill(store, "a-good", skill_name="a-good", tool_name="good_tool")
    _write_exec_skill(store, "b-bad", skill_name="b-bad", tool_name="bad_tool", min_tier="wizard")
    registry = ToolRegistry()

    reload_all_skill_tools(store, registry, secrets={})  # must not raise

    assert registry.has_tool("good_tool")
    assert not registry.has_tool("bad_tool")


def _write_reference_skill(
    store: Path, name: str = "diag-skill", guild_ids: str | None = None
) -> Path:
    skill_dir = store / name
    ref_dir = skill_dir / "reference"
    ref_dir.mkdir(parents=True)
    frontmatter = f"---\nname: {name}\ndescription: Diag skill\n"
    if guild_ids:
        frontmatter += f"guild_ids: [{guild_ids}]\n"
    frontmatter += "---\n\n# Diag workflow\n"
    (skill_dir / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    (ref_dir / "signatures.md").write_text(
        "## Signatures\nCore Not Found: missing core file\nBIOS hash mismatch detected\n",
        encoding="utf-8",
    )
    (ref_dir / "sub").mkdir()
    (ref_dir / "sub" / "deep.md").write_text("deep doc: bios notes\n", encoding="utf-8")
    return skill_dir


@pytest.mark.asyncio
async def test_load_skill_appends_manifest_and_activates_skill_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    _write_reference_skill(store)
    registry = _use_skill_store(store)
    ctx = _staff_ctx()

    result = await skill_tools._load_skill({"name": "diag-skill"}, ctx)

    assert "## Reference files" in result
    assert "- reference/signatures.md" in result
    assert "- reference/sub/deep.md" in result
    assert "skill_file" in ctx.activated_tools
    assert "skill_file" in ctx.explicitly_loaded_tools
    # Searchable: hidden from the ambient tool list until activated.
    ambient = {t.name for t in registry.get_tools_for_tier(TrustTier.MEMBER)}
    assert "skill_file" not in ambient
    active = {t.name for t in registry.get_tools_for_tier(TrustTier.MEMBER, {"skill_file"})}
    assert "skill_file" in active


@pytest.mark.asyncio
async def test_load_skill_without_reference_files_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    skill_dir = store / "plain"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: plain\ndescription: Plain\n---\n\nBody\n", encoding="utf-8"
    )
    _use_skill_store(store)
    _init_skill_registry()
    ctx = _staff_ctx()

    result = await skill_tools._load_skill({"name": "plain"}, ctx)

    assert "## Reference files" not in result
    assert "skill_file" not in ctx.activated_tools


@pytest.mark.asyncio
async def test_load_skill_skips_manifest_when_skill_file_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    _write_reference_skill(store)
    _use_skill_store(store)
    _init_skill_registry()
    ctx = _staff_ctx()
    ctx.blocked_tools = frozenset({"skill_file"})

    result = await skill_tools._load_skill({"name": "diag-skill"}, ctx)

    assert "## Reference files" not in result
    assert "skill_file" not in ctx.activated_tools


@pytest.mark.asyncio
async def test_skill_file_reads_reference_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    _write_reference_skill(store)
    _use_skill_store(store)

    result = await skill_tools._skill_file(
        {"skill": "diag-skill", "path": "reference/signatures.md"}, _staff_ctx()
    )

    assert result.startswith("# diag-skill: reference/signatures.md\n\n")
    assert "BIOS hash mismatch detected" in result

    # Bare paths (no reference/ prefix) resolve too.
    bare = await skill_tools._skill_file(
        {"skill": "diag-skill", "path": "sub/deep.md"}, _staff_ctx()
    )
    assert "bios notes" in bare


def test_skill_file_reference_read_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    _write_reference_skill(store)
    skill = skill_tools.loader.load_skill("diag-skill", skills_dir=store)
    assert skill is not None
    read_sizes: list[int] = []

    class TrackingStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size: int) -> str:
            read_sizes.append(size)
            return "x" * size

    def tracking_open(self: Path, *args, **kwargs):
        del self, args, kwargs
        return TrackingStream()

    monkeypatch.setattr(Path, "open", tracking_open)

    result = skill_tools._read_reference(
        skill,
        "diag-skill",
        "reference/signatures.md",
    )

    assert read_sizes == [skill_tools._SKILL_FILE_MAX_READ_CHARS + 1]
    assert f"[truncated at {skill_tools._SKILL_FILE_MAX_READ_CHARS} characters" in result


@pytest.mark.asyncio
async def test_skill_file_greps_across_reference_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    _write_reference_skill(store)
    _use_skill_store(store)

    result = await skill_tools._skill_file({"skill": "diag-skill", "pattern": "BIOS"}, _staff_ctx())

    assert "reference/signatures.md:3: BIOS hash mismatch detected" in result
    assert "reference/sub/deep.md:1: deep doc: bios notes" in result

    # Invalid regex falls back to a literal search instead of erroring.
    literal = await skill_tools._skill_file(
        {"skill": "diag-skill", "pattern": "Core Not Found ["}, _staff_ctx()
    )
    assert "No matches" in literal

    scoped = await skill_tools._skill_file(
        {"skill": "diag-skill", "pattern": "bios", "path": "reference/sub/deep.md"},
        _staff_ctx(),
    )
    assert "signatures.md" not in scoped
    assert "deep.md:1" in scoped


@pytest.mark.asyncio
async def test_skill_file_bounds_member_supplied_regex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    skill_dir = _write_reference_skill(store)
    (skill_dir / "reference" / "signatures.md").write_text(
        ("a" * 20_000) + "!\n",
        encoding="utf-8",
    )
    _use_skill_store(store)
    monkeypatch.setattr(skill_tools, "_SKILL_FILE_GREP_TIMEOUT_SECONDS", 0.01)

    result = await skill_tools._skill_file(
        {"skill": "diag-skill", "pattern": "(a|aa)+$"}, _staff_ctx()
    )
    too_long = await skill_tools._skill_file(
        {"skill": "diag-skill", "pattern": "x" * 257}, _staff_ctx()
    )

    assert "timed out" in result
    assert "at most 256 characters" in too_long


@pytest.mark.asyncio
async def test_skill_file_rejects_traversal_and_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    skill_dir = _write_reference_skill(store)
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        (skill_dir / "reference" / "link.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    _use_skill_store(store)
    _init_skill_registry()

    ctx = _staff_ctx()
    manifest = await skill_tools._load_skill({"name": "diag-skill"}, ctx)
    assert "link.md" not in manifest

    for bad in ("../SKILL.md", "reference/../SKILL.md", "/etc/passwd", "reference/link.md"):
        result = await skill_tools._skill_file({"skill": "diag-skill", "path": bad}, ctx)
        assert "not found" in result, bad


@pytest.mark.asyncio
async def test_skill_file_masks_guild_scoped_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    _write_reference_skill(store, name="scoped", guild_ids="111")
    _use_skill_store(store)

    wrong_guild = await skill_tools._skill_file(
        {"skill": "scoped", "path": "reference/signatures.md"}, _staff_ctx(guild_id="222")
    )
    assert "not found" in wrong_guild

    home_guild = await skill_tools._skill_file(
        {"skill": "scoped", "path": "reference/signatures.md"}, _staff_ctx(guild_id="111")
    )
    assert "BIOS hash mismatch" in home_guild


@pytest.mark.asyncio
async def test_skill_file_requires_pattern_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "skills"
    _write_reference_skill(store)
    _use_skill_store(store)

    result = await skill_tools._skill_file({"skill": "diag-skill"}, _staff_ctx())

    assert "Provide pattern to search or path to read" in result
    assert "reference/signatures.md" in result


@pytest.mark.asyncio
async def test_missing_private_skills_store_is_valid_and_created_lazily(tmp_path: Path) -> None:
    """A clean clone has no deployment-owned store until it is provisioned or used."""

    store = tmp_path / "private-skills"
    registry = _use_skill_store(store)

    assert not store.exists()
    assert reload_all_skill_tools(store, registry, secrets={}) == 0
    listing = json.loads(await skill_tools._skill_list({}, _staff_ctx()))
    assert {item["name"] for item in listing["skills"]} == SHIPPED_BUILTIN_NAMES
    assert all(item["source"] == "builtin" for item in listing["skills"])
    assert all(item["read_only"] is True for item in listing["skills"])
    assert "not found" in await skill_tools._load_skill({"name": "missing"}, _staff_ctx())
    assert not store.exists(), "Read-only operations must not materialize an empty private store"

    result = json.loads(
        await skill_tools._skill_create(
            {
                "name": "first-skill",
                "description": "Deployment-owned skill",
                "content": "Private instructions.",
            },
            _staff_ctx(guild_id="111"),
        )
    )

    assert result == {"result": "Skill 'first-skill' created successfully."}
    assert (store / "first-skill" / "SKILL.md").is_file()
