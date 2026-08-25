from __future__ import annotations

import logging
import re
import stat
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from branding import DEFAULT_BOT_NAME
from utils.format import sanitize_author_name
from utils.frontmatter import FrontmatterError, find_frontmatter

log = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent / "store"
BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin"
SKILL_FILENAME = "SKILL.md"
REFERENCE_DIR = "reference"
VALID_TOOL_AVAILABILITY = {"always", "search"}


class SkillOrigin(str, Enum):
    PRIVATE = "private"
    BUILTIN = "builtin"


@dataclass(frozen=True)
class SkillToolParameter:
    type: str = "string"
    description: str = ""


@dataclass(frozen=True)
class SkillToolDeclaration:
    name: str
    description: str
    availability: str
    script: str
    parameters: dict[str, SkillToolParameter] = field(default_factory=dict)
    min_tier: str | None = None
    timeout: int | None = None
    network: bool = False
    # Guild ids this tool is restricted to (a skill tool scoped to one
    # community). None means every guild. Stored as a tuple so the frozen
    # dataclass stays hashable; registration converts it to a frozenset.
    guild_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    created_by: str = ""
    created_at: str = ""
    path: Path = field(default=Path())
    tools: list[SkillToolDeclaration] = field(default_factory=list)
    requires_secrets: list[str] = field(default_factory=list)
    # Guild ids this instruction doc is scoped to (a guild-specific skill, e.g.
    # an "about you" doc written for one community). None means every guild.
    # Independent of any per-tool ``guild_ids`` above: this gates the prose's
    # visibility in the <skills> index and load_skill; tool registration keeps
    # its own per-tool scope. Stored as a tuple so the frozen dataclass stays
    # hashable.
    guild_ids: tuple[str, ...] | None = None
    origin: SkillOrigin = SkillOrigin.PRIVATE


@dataclass(frozen=True)
class Skill:
    meta: SkillMeta
    content: str


_INDEX_WHITESPACE_RE = re.compile(r"\s+")
_BUILTIN_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_BUILTIN_PLACEHOLDERS = frozenset({"bot_name"})
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x0400,
)


def is_link_like(path: Path) -> bool:
    """Reject symlinks and Windows reparse points, including junctions."""

    try:
        if path.is_symlink():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def parse_skill_document(raw: str, path: Path | None = None) -> tuple[object, str]:
    frontmatter: object = {}
    body = raw

    try:
        found = find_frontmatter(raw)
    except FrontmatterError:
        # An unclosed block is not a header; the whole document is markdown.
        found = None

    if found is not None:
        frontmatter_text, body = found
        try:
            frontmatter = yaml.safe_load(frontmatter_text)
            # Deliberately narrower than the shared parser: only a genuinely
            # empty block becomes {}. A comment-only block stays None so the
            # caller's isinstance check rejects the skill, rather than loading
            # it with defaults, which for a commented-out `guild_ids:` would
            # silently widen the skill from one guild to every guild.
            if frontmatter is None and not frontmatter_text.strip():
                frontmatter = {}
        except yaml.YAMLError:
            if path:
                log.warning("Invalid YAML frontmatter in %s, treating as plain markdown", path)
            frontmatter = {}

    return frontmatter, body


def _parse_guild_ids(
    raw: object,
    ref: str,
    *,
    strict_tools: bool,
) -> tuple[str, ...] | None:
    """Parse an optional ``guild_ids`` list of numeric Discord ids.

    Shared by per-tool scoping (``skills/registration.py``) and the skill-level
    instruction-doc scoping (``skill_visible_in_guild``). Absent (``None``) means
    global. A present-but-malformed value raises in strict mode (registration
    with ``strict_tools``). In non-strict mode it fails *closed*: this is a
    visibility/privilege restriction, so a parse error must never silently widen
    scope to every guild. We return an empty tuple (the "no guilds" sentinel,
    visible nowhere) and log a warning so the operator notices and fixes the
    fragment.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        if strict_tools:
            raise ValueError(f"Invalid guild_ids for {ref}: must be a list of ids")
        log.warning("Malformed guild_ids for %s (not a list); restricting to no guilds", ref)
        return ()
    ids: list[str] = []
    for entry in raw:
        token = str(entry).strip()
        if token.isdigit():
            ids.append(token)
        elif strict_tools:
            raise ValueError(f"Invalid guild_ids entry for {ref}: {entry!r} is not numeric")
        else:
            log.warning("Dropping non-numeric guild_ids entry %r for %s", entry, ref)
    if ids:
        return tuple(dict.fromkeys(ids))
    # Present but yielded no valid id: fail closed (nowhere), not open (everywhere).
    log.warning("guild_ids for %s has no valid ids; restricting to no guilds", ref)
    return ()


def _parse_tool_parameters(
    params_raw: object,
    tool_ref: str,
    *,
    strict_tools: bool,
) -> dict[str, SkillToolParameter]:
    if params_raw is None:
        return {}
    if not isinstance(params_raw, dict):
        if strict_tools:
            raise ValueError(f"Invalid parameters for {tool_ref}: must be an object")
        return {}

    parameters: dict[str, SkillToolParameter] = {}
    for name, param_def in params_raw.items():
        if not isinstance(param_def, dict):
            if strict_tools:
                raise ValueError(f"Invalid parameter definition for {tool_ref}.{name}")
            continue

        type_value = param_def.get("type", "string")
        description = param_def.get("description", "")
        if strict_tools and not isinstance(type_value, str):
            raise ValueError(f"Invalid parameter type for {tool_ref}.{name}: must be a string")
        if strict_tools and not isinstance(description, str):
            raise ValueError(
                f"Invalid parameter description for {tool_ref}.{name}: must be a string"
            )

        parameters[str(name)] = SkillToolParameter(
            type=str(type_value),
            description=str(description),
        )

    return parameters


def _parse_tool_declarations(
    tools_raw: object,
    skill_name: str,
    *,
    strict_tools: bool,
) -> list[SkillToolDeclaration]:
    if tools_raw is None:
        return []
    if not isinstance(tools_raw, list):
        if strict_tools:
            raise ValueError(f"Invalid tools for {skill_name}: must be a list")
        return []

    tools: list[SkillToolDeclaration] = []
    for index, tool_raw in enumerate(tools_raw):
        tool_ref = f"{skill_name}.tools[{index}]"
        if not isinstance(tool_raw, dict):
            if strict_tools:
                raise ValueError(f"Invalid tool declaration for {tool_ref}: must be an object")
            continue

        name = tool_raw.get("name")
        script = tool_raw.get("script")
        if not isinstance(name, str) or not name.strip():
            if strict_tools:
                raise ValueError(f"Invalid tool declaration for {tool_ref}: missing name")
            continue
        if not isinstance(script, str) or not script.strip():
            if strict_tools:
                raise ValueError(
                    f"Invalid tool declaration for {skill_name}.{name}: missing script"
                )
            continue

        description = tool_raw.get("description", "")
        if strict_tools and not isinstance(description, str):
            raise ValueError(f"Invalid description for {skill_name}.{name}: must be a string")

        availability = tool_raw.get("availability", "search")
        if not isinstance(availability, str) or availability not in VALID_TOOL_AVAILABILITY:
            if strict_tools:
                raise ValueError(f"Invalid availability for {skill_name}.{name}: {availability!r}")
            availability = "search"

        min_tier_raw = tool_raw.get("min_tier")
        if min_tier_raw is not None and not isinstance(min_tier_raw, str):
            if strict_tools:
                raise ValueError(f"Invalid min_tier for {skill_name}.{name}: must be a string")
            min_tier_raw = str(min_tier_raw)

        timeout_raw = tool_raw.get("timeout")
        timeout: int | None = None
        if timeout_raw is not None:
            if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
                if strict_tools:
                    raise ValueError(f"Invalid timeout for {skill_name}.{name}: must be an integer")
            elif timeout_raw <= 0:
                # A non-positive timeout would register a tool that immediately
                # fails every call via asyncio.wait_for(timeout=0). Reject it
                # (strict) or fall back to the default (non-strict, leave None).
                if strict_tools:
                    raise ValueError(
                        f"Invalid timeout for {skill_name}.{name}: must be a positive integer"
                    )
            else:
                timeout = timeout_raw

        network_raw = tool_raw.get("network", False)
        if not isinstance(network_raw, bool):
            if strict_tools:
                raise ValueError(f"Invalid network for {skill_name}.{name}: must be a boolean")
            network_raw = False

        guild_ids = _parse_guild_ids(
            tool_raw.get("guild_ids"), f"{skill_name}.{name}", strict_tools=strict_tools
        )

        parameters = _parse_tool_parameters(
            tool_raw.get("parameters", {}),
            f"{skill_name}.{name}",
            strict_tools=strict_tools,
        )

        tools.append(
            SkillToolDeclaration(
                name=name,
                description=str(description),
                availability=availability,
                script=script,
                parameters=parameters,
                min_tier=min_tier_raw,
                timeout=timeout,
                network=network_raw,
                guild_ids=guild_ids,
            )
        )

    return tools


def _parse_skill_file(
    path: Path,
    *,
    strict_tools: bool = False,
    origin: SkillOrigin = SkillOrigin.PRIVATE,
) -> Skill | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        log.exception("Failed to read skill file: %s", path)
        return None

    frontmatter, body = parse_skill_document(raw, path)
    if not isinstance(frontmatter, dict):
        log.warning("Skill frontmatter in %s must be an object", path)
        return None
    name = frontmatter.get("name", path.parent.name)
    description = frontmatter.get("description", "")
    tags = frontmatter.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    created_by = str(frontmatter.get("created_by", ""))
    created_at = str(frontmatter.get("created_at", ""))
    tools = _parse_tool_declarations(
        frontmatter.get("tools", []),
        str(name),
        strict_tools=strict_tools,
    )
    requires_secrets_raw = frontmatter.get("requires_secrets", [])
    if isinstance(requires_secrets_raw, str):
        requires_secrets = [s.strip() for s in requires_secrets_raw.split(",") if s.strip()]
    elif isinstance(requires_secrets_raw, list):
        requires_secrets = [str(s) for s in requires_secrets_raw]
    else:
        requires_secrets = []

    guild_ids = _parse_guild_ids(
        frontmatter.get("guild_ids"), f"{name} (skill)", strict_tools=strict_tools
    )

    meta = SkillMeta(
        name=name,
        description=description,
        tags=tags,
        created_by=created_by,
        created_at=created_at,
        path=path,
        tools=tools,
        requires_secrets=requires_secrets,
        guild_ids=guild_ids,
        origin=origin,
    )
    return Skill(meta=meta, content=body.strip())


def scan_skills(
    skills_dir: Path | None = None,
    *,
    origin: SkillOrigin = SkillOrigin.PRIVATE,
) -> dict[str, SkillMeta]:
    root = skills_dir or SKILLS_DIR
    skills: dict[str, SkillMeta] = {}

    if is_link_like(root) or not root.is_dir():
        return skills

    for skill_dir in sorted(root.iterdir()):
        if is_link_like(skill_dir) or not skill_dir.is_dir():
            continue
        skill_file = skill_dir / SKILL_FILENAME
        if is_link_like(skill_file) or not skill_file.is_file():
            continue
        skill = _parse_skill_file(skill_file, origin=origin)
        if skill:
            skills[skill.meta.name] = skill.meta

    return skills


def load_skill(
    name: str,
    skills_dir: Path | None = None,
    *,
    origin: SkillOrigin = SkillOrigin.PRIVATE,
) -> Skill | None:
    root = skills_dir or SKILLS_DIR
    skill_dir = root / name

    # Reject path traversal (the name reaches here from a MEMBER-tier tool): the
    # skill directory must be a direct child of the skills store.
    if is_link_like(root) or is_link_like(skill_dir):
        return None
    try:
        if skill_dir.resolve().parent != root.resolve():
            return None
    except OSError:
        return None

    skill_file = skill_dir / SKILL_FILENAME

    if is_link_like(skill_file) or not skill_file.is_file():
        return None

    return _parse_skill_file(skill_file, origin=origin)


def list_reference_files(skill_file_path: Path) -> list[tuple[str, int]]:
    """Regular files under a skill's ``reference/`` dir as (relative path, size).

    Paths are relative to the skill directory (``reference/foo.md``) so they can
    be echoed into load_skill's manifest and passed back to ``skill_file``.
    Symlinks (files or dirs, including ``reference/`` itself) are skipped:
    reference reads must never follow a link out of the skill directory.
    """
    skill_dir = skill_file_path.parent
    ref_dir = skill_dir / REFERENCE_DIR
    if is_link_like(ref_dir) or not ref_dir.is_dir():
        return []
    files: list[tuple[str, int]] = []
    stack = [ref_dir]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if is_link_like(child):
                continue
            if child.is_dir():
                stack.append(child)
            elif child.is_file():
                try:
                    size = child.stat().st_size
                except OSError:
                    continue
                files.append((child.relative_to(skill_dir).as_posix(), size))
    return sorted(files)


def resolve_reference_file(skill_file_path: Path, relative: str) -> Path | None:
    """Resolve a model-supplied path inside the skill's ``reference/`` dir.

    Accepts ``reference/foo.md`` or bare ``foo.md``. Rejects absolute paths,
    traversal, symlinks, and anything resolving outside ``reference/``.
    """
    skill_dir = skill_file_path.parent
    ref_dir = skill_dir / REFERENCE_DIR
    if is_link_like(ref_dir):
        return None
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    parts = rel.parts[1:] if rel.parts and rel.parts[0] == REFERENCE_DIR else rel.parts
    if not parts:
        return None
    candidate = ref_dir.joinpath(*parts)
    current = ref_dir
    for part in parts:
        current = current / part
        if is_link_like(current):
            return None
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(ref_dir.resolve()):
            return None
    except OSError:
        return None
    if not resolved.is_file():
        return None
    return resolved


def validate_builtin_skills(
    builtin_dir: Path = BUILTIN_SKILLS_DIR,
) -> dict[str, SkillMeta]:
    """Validate and return the code-owned, instruction-only skill catalog."""

    root = Path(builtin_dir)
    if is_link_like(root) or not root.is_dir():
        raise ValueError(f"Built-in skills directory is unavailable: {root}")

    skills: dict[str, SkillMeta] = {}
    for entry in sorted(root.iterdir()):
        if entry.name == "README.md" and entry.is_file() and not is_link_like(entry):
            continue
        if is_link_like(entry) or not entry.is_dir():
            raise ValueError(f"Unexpected entry in built-in skills directory: {entry}")

        skill_path = entry / SKILL_FILENAME
        if is_link_like(skill_path) or not skill_path.is_file():
            raise ValueError(f"Built-in skill '{entry.name}' is missing {SKILL_FILENAME}")
        allowed = {SKILL_FILENAME, REFERENCE_DIR}
        unexpected = sorted(child.name for child in entry.iterdir() if child.name not in allowed)
        if unexpected:
            raise ValueError(
                f"Built-in skill '{entry.name}' contains unsupported entries: "
                + ", ".join(unexpected)
            )

        raw = skill_path.read_text(encoding="utf-8")
        frontmatter, _body = parse_skill_document(raw, skill_path)
        if not isinstance(frontmatter, dict):
            raise ValueError(f"Built-in skill '{entry.name}' frontmatter must be an object")
        forbidden = sorted(
            key for key in ("tools", "requires_secrets", "guild_ids") if key in frontmatter
        )
        if forbidden:
            raise ValueError(
                f"Built-in skill '{entry.name}' cannot declare: " + ", ".join(forbidden)
            )
        tags = frontmatter.get("tags", [])
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",")]
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"Built-in skill '{entry.name}' tags must be strings")

        try:
            skill = _parse_skill_file(
                skill_path,
                strict_tools=True,
                origin=SkillOrigin.BUILTIN,
            )
        except ValueError as exc:
            raise ValueError(f"Invalid built-in skill '{entry.name}': {exc}") from exc
        if skill is None:
            raise ValueError(f"Failed to parse built-in skill '{entry.name}'")
        if skill.meta.name != entry.name:
            raise ValueError(
                f"Built-in skill directory '{entry.name}' must match name '{skill.meta.name}'"
            )
        if not isinstance(skill.meta.description, str) or not skill.meta.description.strip():
            raise ValueError(f"Built-in skill '{entry.name}' requires a description")
        _validate_builtin_placeholders(skill)
        _validate_builtin_reference_tree(entry)
        skills[skill.meta.name] = skill.meta

    return skills


def _validate_builtin_placeholders(skill: Skill) -> None:
    names: set[str] = set()
    malformed = False
    for text in (skill.meta.description, skill.content):
        names.update(token.strip() for token in _BUILTIN_PLACEHOLDER_RE.findall(text))
        remainder = _BUILTIN_PLACEHOLDER_RE.sub("", text)
        malformed = malformed or "{{" in remainder or "}}" in remainder
    if malformed:
        raise ValueError(f"Built-in skill '{skill.meta.name}' contains malformed placeholders")
    unsupported = sorted(names.difference(_BUILTIN_PLACEHOLDERS))
    if unsupported:
        labels = [name or "<empty>" for name in unsupported]
        raise ValueError(
            f"Built-in skill '{skill.meta.name}' uses unsupported placeholders: "
            + ", ".join(labels)
        )


def _render_builtin_skill(skill: Skill, *, bot_name: str) -> Skill:
    return Skill(
        meta=replace(
            skill.meta,
            description=_render_builtin_text(skill.meta.description, bot_name=bot_name),
        ),
        content=_render_builtin_text(skill.content, bot_name=bot_name),
    )


def _render_builtin_text(text: str, *, bot_name: str) -> str:
    values = {"bot_name": bot_name}
    return _BUILTIN_PLACEHOLDER_RE.sub(
        lambda match: values.get(match.group(1).strip(), match.group(0)),
        text,
    )


def _validate_builtin_reference_tree(skill_dir: Path) -> None:
    ref_dir = skill_dir / REFERENCE_DIR
    if is_link_like(ref_dir):
        raise ValueError(f"Built-in skill '{skill_dir.name}' cannot contain links")
    if not ref_dir.exists():
        return
    if not ref_dir.is_dir():
        raise ValueError(f"Built-in skill '{skill_dir.name}' reference must be a directory")
    stack = [ref_dir]
    while stack:
        current = stack.pop()
        for child in current.iterdir():
            if is_link_like(child):
                raise ValueError(f"Built-in skill '{skill_dir.name}' cannot contain links")
            if child.is_dir():
                stack.append(child)
            elif not child.is_file():
                raise ValueError(
                    f"Built-in skill '{skill_dir.name}' contains an unsupported reference"
                )


class SharedSkillCatalog:
    """Read-only view combining shipped built-ins with the private skill store."""

    def __init__(
        self,
        private_dir: Path,
        builtin_dir: Path = BUILTIN_SKILLS_DIR,
        *,
        bot_name: str = DEFAULT_BOT_NAME,
    ) -> None:
        self.private_dir = Path(private_dir)
        self.builtin_dir = Path(builtin_dir)
        configured_name = bot_name.strip() or DEFAULT_BOT_NAME
        self.bot_name = sanitize_author_name(configured_name)
        self._warned_collisions: set[str] = set()

    def validate_builtin(self) -> dict[str, SkillMeta]:
        return validate_builtin_skills(self.builtin_dir)

    def reserved_names(self) -> frozenset[str]:
        return frozenset(self.validate_builtin())

    def scan(self) -> dict[str, SkillMeta]:
        builtins = {
            name: replace(
                meta,
                description=_render_builtin_text(meta.description, bot_name=self.bot_name),
            )
            for name, meta in self.validate_builtin().items()
        }
        private = scan_skills(self.private_dir)
        collisions = set(builtins).intersection(private)
        new_collisions = collisions.difference(self._warned_collisions)
        if new_collisions:
            log.warning(
                "Ignoring private skills whose names are reserved by built-ins: %s",
                ", ".join(sorted(new_collisions)),
            )
            self._warned_collisions.update(new_collisions)
        merged = {name: meta for name, meta in private.items() if name not in builtins}
        merged.update(builtins)
        return dict(sorted(merged.items()))

    def load(self, name: str) -> Skill | None:
        if name in self.validate_builtin():
            skill = load_skill(name, self.builtin_dir, origin=SkillOrigin.BUILTIN)
            if skill is None:
                return None
            return _render_builtin_skill(skill, bot_name=self.bot_name)
        return load_skill(name, self.private_dir)

    def is_builtin(self, name: object) -> bool:
        return isinstance(name, str) and name in self.validate_builtin()

    def signature(
        self,
    ) -> tuple[
        tuple[tuple[str, int, int], ...],
        tuple[tuple[str, int, int], ...],
    ]:
        return (_store_signature(self.builtin_dir), _store_signature(self.private_dir))


def _store_signature(root: Path) -> tuple[tuple[str, int, int], ...]:
    """Stat-only fingerprint of the store: (dir name, mtime_ns, size) per SKILL.md."""
    if is_link_like(root) or not root.is_dir():
        return ()
    try:
        children = sorted(root.iterdir())
    except OSError:
        return ()
    entries: list[tuple[str, int, int]] = []
    for skill_dir in children:
        skill_path = skill_dir / SKILL_FILENAME
        if is_link_like(skill_dir) or is_link_like(skill_path):
            continue
        try:
            skill_stat = skill_path.stat()
        except OSError:
            continue
        entries.append((skill_dir.name, skill_stat.st_mtime_ns, skill_stat.st_size))
    return tuple(entries)


class SkillsIndexCache:
    """Caches the rendered skills index used in the system prompt.

    Catalog scans read and YAML-parse every SKILL.md, which is wasteful to
    repeat on every turn. The cache revalidates with a stat-only signature of
    both stores, so private edits, including manual on-disk ones, still show up
    on the next turn, while unchanged stores skip the read+parse entirely.
    """

    def __init__(
        self,
        skills_dir: Path | None = None,
        *,
        catalog: SharedSkillCatalog | None = None,
    ) -> None:
        self._catalog = catalog or SharedSkillCatalog(skills_dir or SKILLS_DIR)
        self._signature: (
            tuple[
                tuple[tuple[str, int, int], ...],
                tuple[tuple[str, int, int], ...],
            ]
            | None
        ) = None
        self._skills: dict[str, SkillMeta] = {}

    def index(self, guild_id: str | None = None) -> str:
        # Cache the parsed store (the expensive part) by stat signature; the
        # per-guild filter+render is cheap, so re-run it each call so the same
        # cached store can serve different guilds.
        signature = self._catalog.signature()
        if signature != self._signature:
            self._skills = self._catalog.scan()
            self._signature = signature
        return build_skills_index(self._skills, guild_id=guild_id)


def skill_visible_in_guild(meta: SkillMeta, guild_id: str | None) -> bool:
    """Whether a skill's instruction doc is listed/loadable in a guild.

    Mirrors guild-scoped tool semantics (see ``tools/registry.py``):
    ``guild_ids is None`` means every guild (the default); a tuple restricts to
    exactly those guilds; the empty tuple (a present-but-malformed ``guild_ids``)
    is visible nowhere (fail closed). A ``None`` ``guild_id`` (DMs / no-guild
    surfaces) never matches a guild-scoped skill.
    """
    if meta.guild_ids is None:
        return True
    return guild_id is not None and guild_id in meta.guild_ids


def build_skills_index(skills: dict[str, SkillMeta], *, guild_id: str | None = None) -> str:
    lines: list[str] = []
    for meta in skills.values():
        if not skill_visible_in_guild(meta, guild_id):
            continue
        name = _index_text(meta.name)
        description = _index_text(meta.description)
        tags = [_index_text(tag) for tag in meta.tags]
        tags = [tag for tag in tags if tag]
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        origin = " (built-in, read-only)" if meta.origin is SkillOrigin.BUILTIN else ""
        lines.append(f"- **{name}**: {description}{tag_str}{origin}")

    return "\n".join(lines)


def _index_text(value: object) -> str:
    return _INDEX_WHITESPACE_RE.sub(" ", str(value)).strip()
