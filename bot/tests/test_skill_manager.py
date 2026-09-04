"""Exercises skills/manager.py and skills/loader.py: skill create/edit/reload,
guild scoping, and rejecting malformed or inline tool frontmatter.
"""

import tempfile
from pathlib import Path

import pytest

from skills.loader import _parse_guild_ids, load_skill
import skills.manager as manager
from skills.registration import reload_all_skill_tools
from tests.helpers import make_settings
from tools.registry import ToolRegistry
from trust.tiers import TrustTier


def test_create_rejects_trailing_newline_in_name(tmp_path: Path) -> None:
    error = manager.create_skill(
        "my-skill\n", description="Instructions", content="Body", skills_dir=tmp_path
    )
    assert error == "Name must be kebab-case (lowercase letters, numbers, hyphens)"
    assert list(tmp_path.iterdir()) == []


def test_edit_preserves_tools_and_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        skill_dir = store / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            """---
name: my-skill
description: Original description
requires_secrets:
  - API_KEY
tools:
  - name: do_thing
    description: Does a thing
    availability: always
    script: scripts/thing.py
---

Original content""",
            encoding="utf-8",
        )

        err = manager.edit_skill("my-skill", content="Updated content", skills_dir=store)
        assert err is None

        skill = load_skill("my-skill", skills_dir=store)
        assert skill is not None
        assert "Updated content" in skill.content
        assert skill.meta.requires_secrets == ["API_KEY"]
        assert len(skill.meta.tools) == 1
        assert skill.meta.tools[0].name == "do_thing"


def test_edit_updates_description_preserves_rest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        skill_dir = store / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            """---
name: my-skill
description: Old desc
tags:
  - testing
requires_secrets:
  - SECRET
---

Body""",
            encoding="utf-8",
        )

        err = manager.edit_skill(
            "my-skill",
            content="New body",
            description="New desc",
            skills_dir=store,
        )
        assert err is None

        skill = load_skill("my-skill", skills_dir=store)
        assert skill is not None
        assert skill.meta.description == "New desc"
        assert skill.meta.requires_secrets == ["SECRET"]
        assert skill.meta.tags == ["testing"]


def test_reload_failure_preserves_existing_skill_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "skills"
        good_skill = store / "zzz-good"
        scripts_dir = good_skill / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "ok.py").write_text("print('ok')\n", encoding="utf-8")
        (good_skill / "SKILL.md").write_text(
            """---
name: zzz-good
description: Good skill
tools:
  - name: still_available
    description: Existing working tool
    availability: search
    script: scripts/ok.py
---
""",
            encoding="utf-8",
        )
        reg = ToolRegistry()
        reload_all_skill_tools(
            store,
            reg,
            secrets={},
            workspace_base_dir=store,
            settings=make_settings(),
        )
        assert reg.has_tool("still_available")


def test_edit_rejects_inline_tool_frontmatter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        skill_dir = store / "test-skill"
        skill_dir.mkdir()
        skill_path = skill_dir / "SKILL.md"
        original = """---
name: test-skill
description: Test skill
---

Original content"""
        skill_path.write_text(original, encoding="utf-8")

        err = manager.edit_skill(
            "test-skill",
            content="""---
tool_name: honk
description: Returns a silly noise
script: |
  echo "HONK"
---

# Test Skill
""",
            skills_dir=store,
        )

        assert err is not None
        assert "frontmatter" in err
        assert skill_path.read_text(encoding="utf-8") == original


def test_create_rejects_inline_tool_frontmatter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)

        err = manager.create_skill(
            "test-skill",
            description="Test skill",
            content="""---
tools:
  - name: honk
    script: scripts/honk.sh
---

# Test Skill
""",
            skills_dir=store,
        )

        assert err is not None
        assert "frontmatter" in err
        assert not (store / "test-skill").exists()


def test_edit_allows_fenced_yaml_examples() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        skill_dir = store / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            """---
name: test-skill
description: Test skill
---

Original content""",
            encoding="utf-8",
        )

        err = manager.edit_skill(
            "test-skill",
            content="""# Tool Example

```yaml
tools:
  - name: honk
    script: scripts/honk.sh
```
""",
            skills_dir=store,
        )

        assert err is None
        skill = load_skill("test-skill", skills_dir=store)
        assert skill is not None
        assert "```yaml" in skill.content


def test_create_allows_body_starting_with_thematic_break() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        content = "---\nThis starts with a Markdown thematic break.\n"

        err = manager.create_skill(
            "thematic-break",
            description="A valid Markdown body",
            content=content,
            skills_dir=store,
        )

        assert err is None
        skill = load_skill("thematic-break", skills_dir=store)
        assert skill is not None
        assert content.rstrip() in skill.content


def test_create_skill_with_guild_id_scopes_to_that_guild() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)

        err = manager.create_skill(
            name="guild-skill",
            description="desc",
            content="Body.",
            guild_id="700000000000000100",
            skills_dir=store,
        )

        assert err is None
        skill = load_skill("guild-skill", skills_dir=store)
        assert skill is not None
        assert skill.meta.guild_ids == ("700000000000000100",)


def test_create_skill_rejects_invalid_guild_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)

        err = manager.create_skill(
            name="bad-guild",
            description="desc",
            content="Body.",
            guild_id="not-a-guild",
            skills_dir=store,
        )

        assert err == "guild_id must be a numeric Discord guild id"
        assert not (store / "bad-guild").exists()


def test_create_skill_without_guild_id_stays_global() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)

        err = manager.create_skill(
            name="global-skill",
            description="desc",
            content="Body.",
            skills_dir=store,
        )

        assert err is None
        skill = load_skill("global-skill", skills_dir=store)
        assert skill is not None
        assert skill.meta.guild_ids is None
        assert "guild_ids" not in (store / "global-skill" / "SKILL.md").read_text(encoding="utf-8")


def test_edit_skill_edits_patch_body_and_preserve_frontmatter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        manager.create_skill(
            name="patchable",
            description="desc",
            content="# Title\n\nFirst paragraph.\n\nSecond paragraph.\n",
            guild_id="700000000000000100",
            skills_dir=store,
        )

        err = manager.edit_skill(
            "patchable",
            edits=[{"old_string": "First paragraph.", "new_string": "First paragraph, patched."}],
            skills_dir=store,
        )

        assert err is None
        skill = load_skill("patchable", skills_dir=store)
        assert skill is not None
        assert "First paragraph, patched." in skill.content
        assert "Second paragraph." in skill.content
        assert skill.meta.guild_ids == ("700000000000000100",)


def test_edit_skill_edits_preserve_tools_and_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        skill_dir = store / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            """---
name: my-skill
description: Original description
requires_secrets:
  - API_KEY
tools:
  - name: do_thing
    description: Does a thing
    availability: always
    script: scripts/thing.py
---

Original content""",
            encoding="utf-8",
        )

        err = manager.edit_skill(
            "my-skill",
            edits=[{"old_string": "Original content", "new_string": "Patched content"}],
            skills_dir=store,
        )
        assert err is None

        skill = load_skill("my-skill", skills_dir=store)
        assert skill is not None
        assert "Patched content" in skill.content
        assert skill.meta.requires_secrets == ["API_KEY"]
        assert len(skill.meta.tools) == 1
        assert skill.meta.tools[0].name == "do_thing"


def test_edit_skill_append_preserves_tools_and_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        skill_dir = store / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            """---
name: my-skill
description: Original description
requires_secrets:
  - API_KEY
tools:
  - name: do_thing
    description: Does a thing
    availability: always
    script: scripts/thing.py
---

Original content""",
            encoding="utf-8",
        )

        err = manager.edit_skill(
            "my-skill",
            append="New section.",
            skills_dir=store,
        )
        assert err is None

        skill = load_skill("my-skill", skills_dir=store)
        assert skill is not None
        assert "Original content" in skill.content
        assert "New section." in skill.content
        assert skill.meta.requires_secrets == ["API_KEY"]
        assert len(skill.meta.tools) == 1
        assert skill.meta.tools[0].name == "do_thing"


def test_edit_skill_edits_replace_all_handles_repeated_matches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        manager.create_skill(
            name="repeated",
            description="desc",
            content="foo foo foo",
            skills_dir=store,
        )

        err = manager.edit_skill(
            "repeated",
            edits=[{"old_string": "foo", "new_string": "bar", "replace_all": True}],
            skills_dir=store,
        )

        assert err is None
        skill = load_skill("repeated", skills_dir=store)
        assert skill is not None
        assert skill.content == "bar bar bar"


def test_edit_skill_edits_replace_all_string_false_still_requires_unique_match() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        manager.create_skill(
            name="falsey",
            description="desc",
            content="foo foo",
            skills_dir=store,
        )

        err = manager.edit_skill(
            "falsey",
            edits=[{"old_string": "foo", "new_string": "bar", "replace_all": "false"}],
            skills_dir=store,
        )

        assert err is not None and "found 2 times" in err
        skill = load_skill("falsey", skills_dir=store)
        assert skill is not None
        assert skill.content == "foo foo"


def test_edit_skill_edits_projected_size_limit_aborts_before_replace_all() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        manager.create_skill(
            name="too-big",
            description="desc",
            content="x x",
            skills_dir=store,
        )
        original = (store / "too-big" / "SKILL.md").read_text(encoding="utf-8")

        err = manager.edit_skill(
            "too-big",
            edits=[
                {
                    "old_string": "x",
                    "new_string": "y" * 60_000,
                    "replace_all": True,
                }
            ],
            skills_dir=store,
        )

        assert err is not None and "exceed" in err
        assert (store / "too-big" / "SKILL.md").read_text(encoding="utf-8") == original


def test_edit_skill_edits_fail_atomically_and_leave_file_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        manager.create_skill(
            name="atomic",
            description="desc",
            content="alpha beta",
            skills_dir=store,
        )
        original = (store / "atomic" / "SKILL.md").read_text(encoding="utf-8")

        # Second edit's old_string doesn't exist; the whole call must fail before
        # the first edit (which is otherwise valid) is ever written.
        err = manager.edit_skill(
            "atomic",
            edits=[
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "does-not-exist", "new_string": "x"},
            ],
            skills_dir=store,
        )

        assert err == "edit 2: old_string not found"
        assert (store / "atomic" / "SKILL.md").read_text(encoding="utf-8") == original


def test_edit_skill_edits_rejects_ambiguous_match_without_replace_all() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        manager.create_skill(
            name="ambiguous",
            description="desc",
            content="foo foo",
            skills_dir=store,
        )

        err = manager.edit_skill(
            "ambiguous",
            edits=[{"old_string": "foo", "new_string": "bar"}],
            skills_dir=store,
        )

        assert err is not None and "found 2 times" in err


def test_edit_skill_append_adds_to_end_of_body() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        manager.create_skill(
            name="appendable",
            description="desc",
            content="# Title\n\nIntro.",
            skills_dir=store,
        )

        err = manager.edit_skill(
            "appendable",
            append="## New Section\n\nMore info.",
            skills_dir=store,
        )

        assert err is None
        skill = load_skill("appendable", skills_dir=store)
        assert skill is not None
        assert skill.content == "# Title\n\nIntro.\n\n## New Section\n\nMore info."


def test_edit_skill_rejects_zero_or_multiple_modes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        manager.create_skill(
            name="modes",
            description="desc",
            content="Body.",
            skills_dir=store,
        )

        assert manager.edit_skill("modes", skills_dir=store) == (
            "Provide exactly one of content, edits, or append"
        )
        assert (
            manager.edit_skill("modes", content="x", append="y", skills_dir=store)
            == "Provide exactly one of content, edits, or append"
        )
        assert (
            manager.edit_skill(
                "modes",
                content="x",
                edits=[{"old_string": "a", "new_string": "b"}],
                skills_dir=store,
            )
            == "Provide exactly one of content, edits, or append"
        )


def test_parse_guild_ids_fails_closed_in_non_strict() -> None:
    # Absent => global (None).
    assert _parse_guild_ids(None, "s.t", strict_tools=False) is None
    # Valid list => that exact set.
    assert _parse_guild_ids(["1", 2], "s.t", strict_tools=False) == ("1", "2")
    # Malformed (not a list) or no valid ids => empty tuple (nowhere), NOT global.
    assert _parse_guild_ids("123", "s.t", strict_tools=False) == ()
    assert _parse_guild_ids(["nope"], "s.t", strict_tools=False) == ()


def test_parse_guild_ids_raises_in_strict() -> None:
    with pytest.raises(ValueError):
        _parse_guild_ids("123", "s.t", strict_tools=True)
    with pytest.raises(ValueError):
        _parse_guild_ids(["nope"], "s.t", strict_tools=True)


def test_reload_skips_malformed_skill_and_keeps_valid_tools() -> None:
    # Fail-soft: one malformed/colliding SKILL.md must be skipped (logged), not
    # abort the whole reload and leave the bot with zero skill tools.
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "skills"
        good_skill = store / "good"
        scripts_dir = good_skill / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "ok.py").write_text("print('ok')\n", encoding="utf-8")
        (good_skill / "SKILL.md").write_text(
            """---
name: good
description: Good skill
tools:
  - name: still_available
    description: Existing working tool
    availability: search
    script: scripts/ok.py
---
""",
            encoding="utf-8",
        )
        reg = ToolRegistry()
        reload_all_skill_tools(
            store,
            reg,
            secrets={},
            workspace_base_dir=store,
            settings=make_settings(),
        )
        assert reg.has_tool("still_available")

        bad_skill = store / "bad"
        bad_skill.mkdir()
        (bad_skill / "SKILL.md").write_text(
            """---
name: bad
description: Bad skill
tools:
  - name: broken_tool
    description: Broken tool
    availability: search
    min_tier: owner
    script: scripts/missing.py
    timeout: slow
---
""",
            encoding="utf-8",
        )

        # Does NOT raise; the bad skill is skipped, the good tool survives.
        reload_all_skill_tools(
            store,
            reg,
            secrets={},
            workspace_base_dir=store,
            settings=make_settings(),
        )
        assert reg.has_tool("still_available")
        assert not reg.has_tool("broken_tool")

        bad_skill = store / "aaa-bad"
        bad_skill.mkdir()
        (bad_skill / "SKILL.md").write_text(
            """---
name: aaa-bad
description: Bad skill
tools:
  - name: broken_tool
    description: Broken tool
    availability: serach
    script: scripts/missing.py
---
""",
            encoding="utf-8",
        )

        reload_all_skill_tools(
            store,
            reg,
            secrets={},
            workspace_base_dir=store,
            settings=make_settings(),
        )
        assert reg.has_tool("still_available")
        assert not reg.has_tool("broken_tool")


def test_load_skill_rejects_path_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = base / "store"
        store.mkdir()
        outside = base / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(
            "---\nname: outside\ndescription: secret\n---\n", encoding="utf-8"
        )

        result = load_skill("../outside", skills_dir=store)

        assert result is None


def test_skill_tool_without_min_tier_defaults_to_staff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "skills"
        skill = store / "honker"
        scripts = skill / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "honk.py").write_text("print('honk')\n", encoding="utf-8")
        (skill / "SKILL.md").write_text(
            "---\nname: honker\ndescription: Honk\ntools:\n"
            "  - name: honk\n    description: Honk a horn\n"
            "    availability: always\n    script: scripts/honk.py\n---\n",
            encoding="utf-8",
        )
        reg = ToolRegistry()
        reload_all_skill_tools(
            store,
            reg,
            secrets={},
            workspace_base_dir=store,
            settings=make_settings(),
        )

        assert reg.has_tool("honk")
        member_tools = [t.name for t in reg.get_tools_for_tier(TrustTier.MEMBER, set())]
        staff_tools = [t.name for t in reg.get_tools_for_tier(TrustTier.STAFF, set())]
        # Omitted min_tier must be fail-closed (STAFF), not callable by members.
        assert "honk" not in member_tools
        assert "honk" in staff_tools


def test_secret_backed_skill_tool_is_forced_to_staff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "skills"
        skill = store / "secret-honker"
        scripts = skill / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "honk.py").write_text("print('honk')\n", encoding="utf-8")
        (skill / "SKILL.md").write_text(
            "---\n"
            "name: secret-honker\n"
            "description: Secret honk\n"
            "requires_secrets: [API_KEY]\n"
            "tools:\n"
            "  - name: secret_honk\n"
            "    description: Honk with a secret\n"
            "    availability: always\n"
            "    min_tier: member\n"
            "    script: scripts/honk.py\n"
            "---\n",
            encoding="utf-8",
        )
        reg = ToolRegistry()

        reload_all_skill_tools(
            store,
            reg,
            secrets={"API_KEY": "test-secret"},
            workspace_base_dir=store,
            settings=make_settings(),
        )

        member_tools = [t.name for t in reg.get_tools_for_tier(TrustTier.MEMBER, set())]
        staff_tools = [t.name for t in reg.get_tools_for_tier(TrustTier.STAFF, set())]
        assert "secret_honk" not in member_tools
        assert "secret_honk" in staff_tools


def test_delete_skill_rejects_path_traversal_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = base / "store"
        store.mkdir()
        victim = base / "victim"
        victim.mkdir()
        (victim / "important.txt").write_text("keep me", encoding="utf-8")

        err = manager.delete_skill("../victim", skills_dir=store)

        assert err is not None
        assert victim.exists()
        assert (victim / "important.txt").exists()


def test_delete_skill_removes_valid_skill() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        skill_dir = store / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: A skill\n---\n", encoding="utf-8"
        )

        err = manager.delete_skill("my-skill", skills_dir=store)

        assert err is None
        assert not skill_dir.exists()
