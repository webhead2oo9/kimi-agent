from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, UTC
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from skills.loader import (
    SKILLS_DIR,
    SKILL_FILENAME,
    SkillMeta,
    load_skill,
    parse_skill_document,
    scan_skills,
)
from utils.files import atomic_write_text
from utils.parsing import as_bool

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOOL_METADATA_KEYS = {"tools", "tool_name", "script"}
MAX_CONTENT_SIZE = 100_000


def validate_name(name: str) -> str | None:
    if not name:
        return "Name is required"
    if not _NAME_RE.match(name):
        return "Name must be kebab-case (lowercase letters, numbers, hyphens)"
    if len(name) > 80:
        return "Name must be 80 characters or fewer"
    return None


def validate_skill_content(content: str) -> str | None:
    """Reject body content that looks like misplaced executable-tool metadata."""
    frontmatter, _body = parse_skill_document(content)
    # Manager input is nested beneath manager-owned frontmatter. An unmatched
    # leading thematic break is ordinary Markdown body in the saved document.
    if frontmatter is None:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        return "Skill frontmatter must be a YAML object"
    if any(key in frontmatter for key in _TOOL_METADATA_KEYS):
        return (
            "Executable tool metadata belongs in the skill's frontmatter, "
            "authored on disk in the skills store, not embedded in skill "
            "markdown content."
        )
    return None


def create_skill(
    name: str,
    description: str,
    content: str,
    tags: list[str] | None = None,
    created_by: str = "",
    guild_id: str | None = None,
    skills_dir: Path | None = None,
) -> str | None:
    """Create a new skill. Returns error message on failure, None on success.

    ``guild_id``, when given, scopes the new skill's prose to that guild
    (top-level ``guild_ids`` frontmatter, see ``skills/loader.py``) so a skill
    created from inside a guild defaults to that guild instead of going global.
    The model never supplies this; callers pass ``ctx.guild_id`` transparently.
    """
    root = skills_dir or SKILLS_DIR

    err = validate_name(name)
    if err:
        return err
    if not description:
        return "Description is required"
    if len(content) > MAX_CONTENT_SIZE:
        return f"Content exceeds max size ({MAX_CONTENT_SIZE} chars)"
    content_err = validate_skill_content(content)
    if content_err:
        return content_err

    skill_dir = root / name
    if skill_dir.exists():
        return f"Skill '{name}' already exists"

    frontmatter: dict[str, object] = {
        "name": name,
        "description": description,
    }
    if tags:
        frontmatter["tags"] = tags
    if guild_id:
        guild_token = str(guild_id).strip()
        if not guild_token.isdigit():
            return "guild_id must be a numeric Discord guild id"
        frontmatter["guild_ids"] = [int(guild_token)]
    if created_by:
        frontmatter["created_by"] = created_by
    frontmatter["created_at"] = datetime.now(UTC).strftime("%Y-%m-%d")

    fm_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip()
    full_content = f"---\n{fm_str}\n---\n\n{content}"

    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(skill_dir / SKILL_FILENAME, full_content)
    except Exception as e:
        log.exception("Failed to create skill %s", name)
        return f"Failed to write skill: {e}"

    log.info("Created skill: %s", name)
    return None


MAX_SKILL_EDITS = 50


def apply_skill_edits(content: str, edits: list[dict]) -> tuple[str, str | None]:
    """Apply ordered old_string/new_string patches to skill body content.

    Returns ``(content, error_message)`` unchanged on validation failure and
    ``(new_body, None)`` on success. Mirrors ``tools/workspace/files.py``'s
    ``multi_edit`` semantics: every edit's shape is validated up front, then
    edits apply in order against the progressively-updated text. Each
    ``old_string`` must match exactly once unless that edit sets
    ``replace_all``. Any failure aborts before any edit is applied, so a
    caller never sees a half-patched result.
    """
    if not isinstance(edits, list) or not edits:
        return content, "edits must be a non-empty list"
    if len(edits) > MAX_SKILL_EDITS:
        return content, f"edits accepts at most {MAX_SKILL_EDITS} entries per call"

    parsed: list[tuple[str, str, bool]] = []
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return content, f"edit {index}: must be an object"
        old_string = edit.get("old_string")
        new_string = edit.get("new_string")
        try:
            replace_all = as_bool(edit.get("replace_all"), name="replace_all", default=False)
        except ValueError as exc:
            # This function reports failures as a message, never by raising.
            return content, f"edit {index}: {exc}"
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return content, f"edit {index}: old_string and new_string must be strings"
        if old_string == "":
            return content, f"edit {index}: old_string must not be empty"
        if old_string == new_string:
            return (
                content,
                f"edit {index}: old_string and new_string are identical, so nothing would change",
            )
        parsed.append((old_string, new_string, replace_all))

    working = content
    for index, (old_string, new_string, replace_all) in enumerate(parsed, start=1):
        count = working.count(old_string)
        if count == 0:
            return content, f"edit {index}: old_string not found"
        if count > 1 and not replace_all:
            return content, (
                f"edit {index}: old_string found {count} times; "
                "make it unique or pass replace_all=true"
            )
        replacements = count if replace_all else 1
        projected_size = len(working) + replacements * (len(new_string) - len(old_string))
        if projected_size > MAX_CONTENT_SIZE:
            return content, (
                f"edit {index}: result would exceed the {MAX_CONTENT_SIZE} char content limit"
            )
        working = (
            working.replace(old_string, new_string)
            if replace_all
            else working.replace(old_string, new_string, 1)
        )
    return working, None


def edit_skill(
    name: str,
    content: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    edits: list[dict] | None = None,
    append: str | None = None,
    skills_dir: Path | None = None,
) -> str | None:
    """Edit an existing skill. Returns error message on failure, None on success.

    Exactly one of three modes produces the new body: ``content`` (full
    replacement), ``edits`` (atomic old_string/new_string patches; see
    ``apply_skill_edits``), or ``append`` (text added to the end of the current
    body). Frontmatter (name/tags/guild_ids/tools/...) always round-trips
    unchanged except for ``description``/``tags`` when explicitly passed here.
    """
    root = skills_dir or SKILLS_DIR
    skill_path = root / name / SKILL_FILENAME

    existing = load_skill(name, skills_dir=root)
    if existing is None:
        return f"Skill '{name}' not found"

    modes_given = sum(m is not None for m in (content, edits, append))
    if modes_given != 1:
        return "Provide exactly one of content, edits, or append"

    if content is not None:
        body = content
    elif edits is not None:
        body, edit_err = apply_skill_edits(existing.content, edits)
        if edit_err:
            return edit_err
    elif append is not None:
        if not append.strip():
            return "append must not be empty"
        base = existing.content.rstrip("\n")
        body = f"{base}\n\n{append.strip()}\n" if base else f"{append.strip()}\n"
    else:
        return "Provide exactly one of content, edits, or append"

    if len(body) > MAX_CONTENT_SIZE:
        return f"Content exceeds max size ({MAX_CONTENT_SIZE} chars)"
    content_err = validate_skill_content(body)
    if content_err:
        return content_err

    existing_fm: dict = {
        "name": existing.meta.name,
        "description": existing.meta.description,
    }
    try:
        raw = skill_path.read_text(encoding="utf-8")
        parsed, _body = parse_skill_document(raw, skill_path)
        if isinstance(parsed, dict):
            existing_fm = parsed
    except Exception:
        log.exception("Failed to read frontmatter for skill %s", name)
        return "Failed to read existing skill frontmatter"

    existing_fm["name"] = existing.meta.name
    if description is not None:
        existing_fm["description"] = description
    elif "description" not in existing_fm:
        existing_fm["description"] = existing.meta.description
    if tags is not None:
        existing_fm["tags"] = tags

    fm_str = yaml.dump(existing_fm, default_flow_style=False, sort_keys=False).strip()
    full_content = f"---\n{fm_str}\n---\n\n{body}"

    try:
        atomic_write_text(skill_path, full_content)
    except Exception as e:
        log.exception("Failed to edit skill %s", name)
        return f"Failed to write skill: {e}"

    log.info("Edited skill: %s", name)
    return None


def delete_skill(name: str, skills_dir: Path | None = None) -> str | None:
    """Delete a skill. Returns error message on failure, None on success."""
    root = skills_dir or SKILLS_DIR

    err = validate_name(name)
    if err:
        return err

    skill_dir = root / name

    if not skill_dir.exists():
        return f"Skill '{name}' not found"

    try:
        shutil.rmtree(skill_dir)
    except Exception as e:
        log.exception("Failed to delete skill %s", name)
        return f"Failed to delete skill: {e}"

    log.info("Deleted skill: %s", name)
    return None


def list_skills(skills_dir: Path | None = None) -> list[SkillMeta]:
    """List all available skills."""
    root = skills_dir or SKILLS_DIR
    return list(scan_skills(root).values())
