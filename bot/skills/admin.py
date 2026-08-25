from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from skills import loader, manager
from utils.files import atomic_write_bytes
from utils.frontmatter import FrontmatterError, find_frontmatter
from skills.loader import REFERENCE_DIR, SKILL_FILENAME

MAX_DESCRIPTION_CHARS = 1_000
MAX_TAGS = 50
MAX_TAG_CHARS = 80
MAX_GUILD_IDS = 100
MAX_DISCORD_ID_CHARS = 32
MAX_REFERENCE_BYTES = 200_000

_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")

log = logging.getLogger(__name__)


class SkillAdminError(Exception):
    """A safe, user-facing skill administration failure.

    Carries only ``message``: the sole consumer is `tools/skills.py`, which
    surfaces it to the model verbatim. The HTTP status and machine-readable
    code this used to carry served the removed staff console.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SkillAdminService:
    """Transactional administration for shared instruction and executable skills.

    All mutations are serialized by a per-service lock. Instruction documents
    and reference files use replace-on-close writes. Operations that trigger the
    executable-tool reload callback restore their filesystem snapshot if the
    callback fails.
    """

    def __init__(
        self,
        skills_dir: Path,
        *,
        on_skills_changed: Callable[[], object] | None = None,
        reserved_names: frozenset[str] = frozenset(),
    ) -> None:
        self.skills_dir = Path(skills_dir)
        self._on_skills_changed = on_skills_changed
        self._reserved_names = reserved_names
        self._lock = threading.RLock()
        self._require_safe_store(allow_missing=True)

    def list_skills(self) -> list[dict[str, Any]]:
        with self._lock:
            self._require_safe_store(allow_missing=True)
            if not self.skills_dir.is_dir():
                return []
            details: list[dict[str, Any]] = []
            for skill_dir in sorted(self.skills_dir.iterdir()):
                if self._is_link(skill_dir) or not skill_dir.is_dir():
                    continue
                if manager.validate_name(skill_dir.name):
                    # Keep list/get contracts aligned: a manually created directory
                    # names outside the management grammar remain runtime-owned
                    # and are not presented as editable Web UI resources.
                    continue
                skill_path = skill_dir / SKILL_FILENAME
                if self._is_link(skill_path) or not skill_path.is_file():
                    continue
                try:
                    details.append(self._detail_from_path(skill_path))
                except SkillAdminError:
                    # A malformed file, or one that disappears between directory
                    # enumeration and read, should not make the entire operator
                    # listing fail.
                    continue
            return details

    def get(self, name: str) -> dict[str, Any]:
        with self._lock:
            return self._detail_from_path(self._skill_path(name))

    def create(
        self,
        *,
        name: str,
        description: str,
        body: str,
        tags: list[str] | None = None,
        guild_ids: list[str] | None = None,
        created_by: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            self._require_safe_store(allow_missing=True)
            self._validate_name(name)
            description = self._validate_description(description)
            body = self._validate_body(body)
            normalized_tags = self._normalize_tags(tags or [])
            normalized_guild_ids = self._normalize_guild_ids(guild_ids)

            try:
                self.skills_dir.mkdir(parents=True, exist_ok=True)
                self._require_safe_store()
            except OSError as exc:
                raise SkillAdminError(f"Failed to prepare skills store: {exc}") from exc
            skill_dir = self.skills_dir / name
            if skill_dir.exists() or self._is_link(skill_dir):
                raise SkillAdminError(f"Skill '{name}' already exists")

            frontmatter: dict[str, object] = {
                "name": name,
                "description": description,
            }
            if normalized_tags:
                frontmatter["tags"] = normalized_tags
            if normalized_guild_ids:
                frontmatter["guild_ids"] = [int(value) for value in normalized_guild_ids]
            if created_by:
                frontmatter["created_by"] = created_by
            frontmatter["created_at"] = datetime.now(UTC).strftime("%Y-%m-%d")
            raw = self._render_document(frontmatter, body).encode("utf-8")

            try:
                staging = Path(
                    tempfile.mkdtemp(
                        prefix=f".{self.skills_dir.name}-{name}.",
                        dir=str(self.skills_dir.parent),
                    )
                )
            except OSError as exc:
                raise SkillAdminError(f"Failed to stage skill creation: {exc}") from exc
            published = False
            try:
                self._write_atomic(staging / SKILL_FILENAME, raw)
                os.replace(staging, skill_dir)
                published = True

                def rollback_create() -> None:
                    shutil.rmtree(skill_dir)

                self._reload_or_rollback(rollback_create)
            except SkillAdminError:
                raise
            except OSError as exc:
                raise SkillAdminError(f"Failed to create skill: {exc}") from exc
            finally:
                if not published:
                    shutil.rmtree(staging, ignore_errors=True)

            return self._detail_from_path(skill_dir / SKILL_FILENAME)

    def edit(
        self,
        name: str,
        *,
        body: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        guild_ids: list[str] | None = None,
        edits: list[dict] | None = None,
        append: str | None = None,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            skill_path = self._skill_path(name)
            original = self._read_bytes(skill_path)
            self._check_revision(original, expected_revision)
            raw = self._decode_skill(original, skill_path)
            frontmatter, existing_body = self._parse_document(raw, skill_path)

            body_modes = sum(value is not None for value in (body, edits, append))
            if body_modes > 1:
                raise SkillAdminError("Provide at most one of body, edits, or append")
            if body is not None:
                updated_body = self._validate_body(body)
            elif edits is not None:
                updated_body, edit_error = manager.apply_skill_edits(existing_body.strip(), edits)
                if edit_error:
                    raise SkillAdminError(edit_error)
                updated_body = self._validate_body(updated_body)
            elif append is not None:
                if not isinstance(append, str) or not append.strip():
                    raise SkillAdminError("append must not be empty")
                base = existing_body.strip().rstrip("\n")
                updated_body = f"{base}\n\n{append.strip()}\n" if base else f"{append.strip()}\n"
                updated_body = self._validate_body(updated_body)
            else:
                updated_body = existing_body.strip()

            changed = body_modes > 0
            # The directory is the authoritative lookup key. Repair any
            # divergent hand-edited frontmatter instead of preserving an entry
            # that cannot subsequently be addressed by its advertised name.
            frontmatter["name"] = name
            if description is not None:
                frontmatter["description"] = self._validate_description(description)
                changed = True
            elif "description" not in frontmatter:
                frontmatter["description"] = ""
            if tags is not None:
                normalized_tags = self._normalize_tags(tags)
                if normalized_tags:
                    frontmatter["tags"] = normalized_tags
                else:
                    frontmatter.pop("tags", None)
                changed = True
            if guild_ids is not None:
                normalized_guild_ids = self._normalize_guild_ids(guild_ids)
                if normalized_guild_ids:
                    frontmatter["guild_ids"] = [int(value) for value in normalized_guild_ids]
                else:
                    frontmatter.pop("guild_ids", None)
                changed = True
            if not changed:
                raise SkillAdminError("No skill changes were provided")

            updated = self._render_document(frontmatter, updated_body).encode("utf-8")
            self._write_atomic(skill_path, updated)
            self._reload_or_rollback(lambda: self._write_atomic(skill_path, original))
            return self._detail_from_path(skill_path)

    def delete(
        self,
        name: str,
        *,
        expected_revision: str | None = None,
        expected_delete_revision: str | None = None,
        allow_executable: bool = True,
    ) -> None:
        with self._lock:
            skill_path = self._skill_path(name)
            original = self._read_bytes(skill_path)
            self._check_revision(original, expected_revision)
            if expected_delete_revision is not None:
                expected_delete = self._validate_revision(
                    expected_delete_revision,
                    field="expected_delete_revision",
                )
                if self._delete_revision(skill_path, skill_data=original) != expected_delete:
                    raise SkillAdminError(
                        "Skill or reference files changed since they were loaded",
                    )
            raw = self._decode_skill(original, skill_path)
            frontmatter, _body = self._parse_document(raw, skill_path)
            raw_tools = frontmatter.get("tools")
            if raw_tools not in (None, []) and not allow_executable:
                raise SkillAdminError(
                    "Skills with executable tools cannot be deleted from the Web UI",
                )

            skill_dir = skill_path.parent
            trash = (
                self.skills_dir.parent
                / f".{self.skills_dir.name}-{name}-deleted-{uuid.uuid4().hex}"
            )
            try:
                os.replace(skill_dir, trash)
            except OSError as exc:
                raise SkillAdminError(f"Failed to delete skill: {exc}") from exc
            try:
                self._reload_or_rollback(lambda: os.replace(trash, skill_dir))
            except SkillAdminError:
                raise
            try:
                shutil.rmtree(trash)
            except OSError:
                # The requested mutation and reload already succeeded. Returning
                # an error would invite a destructive retry, so leave the private
                # tombstone outside the scanned store and report it operationally.
                log.exception("Failed to clean deleted skill tombstone %s", trash)

    def _detail_from_path(self, skill_path: Path) -> dict[str, Any]:
        data = self._read_bytes(skill_path)
        raw = self._decode_skill(data, skill_path)
        self._parse_document(raw, skill_path)
        skill = loader.load_skill(skill_path.parent.name, skills_dir=self.skills_dir)
        if skill is None:
            raise SkillAdminError(f"Skill '{skill_path.parent.name}' not found")
        meta = skill.meta
        references = self._reference_manifest(skill_path)
        return {
            # Directory names are the authoritative API lookup key. Returning
            # it also keeps a hand-edited, divergent frontmatter name repairable.
            "name": skill_path.parent.name,
            "description": meta.description,
            "body": skill.content,
            "tags": list(meta.tags),
            "guild_ids": list(meta.guild_ids) if meta.guild_ids is not None else None,
            "created_by": meta.created_by,
            "created_at": meta.created_at,
            "requires_secrets": list(meta.requires_secrets),
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "availability": tool.availability,
                    "min_tier": tool.min_tier,
                    "script": tool.script,
                    "parameters": {
                        param_name: asdict(parameter)
                        for param_name, parameter in tool.parameters.items()
                    },
                    "timeout": tool.timeout,
                    "guild_ids": (list(tool.guild_ids) if tool.guild_ids is not None else None),
                }
                for tool in meta.tools
            ],
            "references": references,
            "revision": self._revision(data),
            "delete_revision": self._delete_revision(
                skill_path,
                skill_data=data,
                references=references,
            ),
        }

    def _skill_path(self, name: str) -> Path:
        self._require_safe_store(allow_missing=True)
        self._validate_name(name)
        skill_dir = self.skills_dir / name
        if self._is_link(skill_dir):
            raise SkillAdminError("Symlinked skill directories are not supported")
        try:
            root = self.skills_dir.resolve()
            if skill_dir.resolve().parent != root:
                raise SkillAdminError("Skill path escapes the skills store")
        except OSError as exc:
            raise SkillAdminError("Invalid skill path") from exc
        skill_path = skill_dir / SKILL_FILENAME
        if self._is_link(skill_path):
            raise SkillAdminError("Symlinked skill documents are not supported")
        if not skill_path.is_file():
            raise SkillAdminError(f"Skill '{name}' not found")
        return skill_path

    def _require_safe_store(self, *, allow_missing: bool = False) -> None:
        if self._is_link(self.skills_dir):
            raise SkillAdminError("Symlinked or junction-backed skills stores are not supported")
        try:
            if self.skills_dir.exists():
                if not self.skills_dir.is_dir():
                    raise SkillAdminError("Skills store is not a directory")
                return
        except OSError as exc:
            raise SkillAdminError("Invalid skills store") from exc
        if not allow_missing:
            raise SkillAdminError("Skills store is unavailable")

    def _validate_name(self, name: object) -> None:
        if not isinstance(name, str):
            raise SkillAdminError("Name is required")
        error = manager.validate_name(name)
        if error:
            raise SkillAdminError(error)
        if name in self._reserved_names:
            raise SkillAdminError(f"Built-in skill '{name}' is read-only")

    @staticmethod
    def _validate_description(description: object) -> str:
        if not isinstance(description, str) or not description.strip():
            raise SkillAdminError("Description is required")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise SkillAdminError(f"Description exceeds max size ({MAX_DESCRIPTION_CHARS} chars)")
        return description

    @staticmethod
    def _validate_body(body: object) -> str:
        if not isinstance(body, str):
            raise SkillAdminError("body must be a string")
        if len(body) > manager.MAX_CONTENT_SIZE:
            raise SkillAdminError(f"Content exceeds max size ({manager.MAX_CONTENT_SIZE} chars)")
        error = manager.validate_skill_content(body)
        if error:
            raise SkillAdminError(error)
        return body

    @staticmethod
    def _normalize_tags(tags: object) -> list[str]:
        if not isinstance(tags, list):
            raise SkillAdminError("tags must be an array of strings")
        if len(tags) > MAX_TAGS:
            raise SkillAdminError(f"tags accepts at most {MAX_TAGS} entries")
        normalized: list[str] = []
        for value in tags:
            if not isinstance(value, str) or not value.strip():
                raise SkillAdminError("tags entries must be non-empty strings")
            token = value.strip()
            if len(token) > MAX_TAG_CHARS:
                raise SkillAdminError(f"tags entries must be at most {MAX_TAG_CHARS} characters")
            if token not in normalized:
                normalized.append(token)
        return normalized

    @staticmethod
    def _normalize_guild_ids(guild_ids: object) -> list[str]:
        if guild_ids is None:
            return []
        if not isinstance(guild_ids, list):
            raise SkillAdminError("guild_ids must be an array of numeric Discord guild ids")
        if len(guild_ids) > MAX_GUILD_IDS:
            raise SkillAdminError(f"guild_ids accepts at most {MAX_GUILD_IDS} entries")
        normalized: list[str] = []
        for value in guild_ids:
            if isinstance(value, bool):
                raise SkillAdminError("guild_ids entries must be numeric Discord guild ids")
            token = str(value).strip()
            if not token.isdigit() or len(token) > MAX_DISCORD_ID_CHARS:
                raise SkillAdminError(
                    f"guild_ids entry {value!r} is not a numeric Discord guild id"
                )
            if token not in normalized:
                normalized.append(token)
        return normalized

    @staticmethod
    def _render_document(frontmatter: dict[str, object], body: str) -> str:
        yaml_text = yaml.safe_dump(
            frontmatter,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ).strip()
        return f"---\n{yaml_text}\n---\n\n{body}"

    @staticmethod
    def _parse_document(raw: str, path: Path) -> tuple[dict[str, Any], str]:
        try:
            found = find_frontmatter(raw)
        except FrontmatterError as exc:
            raise SkillAdminError(f"{path.name} has invalid YAML frontmatter: {exc}") from exc
        if found is None:
            raise SkillAdminError(f"{path.name} is missing YAML frontmatter")
        frontmatter_text, body = found
        try:
            parsed = yaml.safe_load(frontmatter_text)
        except yaml.YAMLError as exc:
            raise SkillAdminError(f"{path.name} has invalid YAML frontmatter") from exc
        # Matches skills/loader.py: only a genuinely empty block is {}, so an
        # edit that comments out every key is rejected rather than silently
        # dropping the skill's guild scoping.
        if parsed is None and not frontmatter_text.strip():
            parsed = {}
        if not isinstance(parsed, dict):
            raise SkillAdminError(f"{path.name} frontmatter must be an object")
        return parsed, body

    @staticmethod
    def _decode_skill(data: bytes, path: Path) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillAdminError(f"{path.name} is not valid UTF-8") from exc

    def _reference_manifest(self, skill_path: Path) -> list[dict[str, Any]]:
        manifest: list[dict[str, Any]] = []
        for loader_path, size in loader.list_reference_files(skill_path):
            relative = loader_path.removeprefix(f"{REFERENCE_DIR}/")
            if size > MAX_REFERENCE_BYTES:
                # Keep the skill visible without reading an arbitrarily large
                # hand-authored file into the Web UI process. Direct reads
                # return 413 until an operator fixes the file on disk.
                manifest.append(
                    {
                        "path": relative,
                        "size": size,
                        "revision": None,
                    }
                )
                continue
            resolved = loader.resolve_reference_file(skill_path, loader_path)
            if resolved is None:
                continue
            data = self._read_bytes(resolved)
            manifest.append(
                {
                    "path": relative,
                    "size": len(data),
                    "revision": self._revision(data),
                }
            )
        return manifest

    def _delete_revision(
        self,
        skill_path: Path,
        *,
        skill_data: bytes | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> str:
        """Revision covering the document and reference tree deleted together.

        Normal reference files contribute their content revision. For an
        oversized hand-authored file beyond the read cap, size and
        nanosecond mtime still make ordinary replacement visible without loading
        unbounded content into the process.
        """
        data = skill_data if skill_data is not None else self._read_bytes(skill_path)
        manifest = references if references is not None else self._reference_manifest(skill_path)
        digest = hashlib.sha256()
        digest.update(b"skill\0")
        digest.update(self._revision(data).encode("ascii"))
        for entry in sorted(manifest, key=lambda item: str(item["path"])):
            relative = str(entry["path"])
            digest.update(b"\0reference\0")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(entry["size"]).encode("ascii"))
            revision = entry.get("revision")
            if isinstance(revision, str):
                digest.update(b"\0")
                digest.update(revision.encode("ascii"))
                continue
            resolved = loader.resolve_reference_file(
                skill_path,
                f"{REFERENCE_DIR}/{relative}",
            )
            if resolved is not None:
                try:
                    digest.update(b"\0")
                    digest.update(str(resolved.stat().st_mtime_ns).encode("ascii"))
                except OSError:
                    digest.update(b"\0unavailable")
        return digest.hexdigest()

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise SkillAdminError(f"File '{path.name}' not found") from exc
        except OSError as exc:
            raise SkillAdminError(f"Failed to read '{path.name}': {exc}") from exc

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        try:
            atomic_write_bytes(path, data)
        except OSError as exc:
            raise SkillAdminError(f"Failed to write '{path.name}': {exc}") from exc

    def _reload_or_rollback(self, rollback: Callable[[], None]) -> None:
        if self._on_skills_changed is None:
            return
        try:
            self._on_skills_changed()
        except Exception as exc:
            try:
                rollback()
            except Exception:
                log.exception("Failed to roll back skill files after reload failure")
                self._restore_reload_best_effort()
                raise SkillAdminError(
                    "Executable skill tool reload failed and the filesystem rollback "
                    "also failed; operator intervention is required.",
                ) from exc
            self._restore_reload_best_effort()
            raise SkillAdminError(
                f"Executable skill tool reload failed: {exc}; rolled back skill changes.",
            ) from exc

    def _restore_reload_best_effort(self) -> None:
        if self._on_skills_changed is None:
            return
        with contextlib.suppress(Exception):
            self._on_skills_changed()

    @staticmethod
    def _revision(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _validate_revision(
        revision: object,
        *,
        field: str = "expected_revision",
    ) -> str:
        if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
            raise SkillAdminError(f"{field} must be a SHA-256 revision")
        return revision

    def _check_revision(self, data: bytes, expected_revision: str | None) -> None:
        if expected_revision is None:
            return
        expected = self._validate_revision(expected_revision)
        if self._revision(data) != expected:
            raise SkillAdminError(
                "Resource changed since it was loaded",
            )

    @staticmethod
    def _is_link(path: Path) -> bool:
        """Treat Windows junctions like symlinks."""

        return loader.is_link_like(path)
