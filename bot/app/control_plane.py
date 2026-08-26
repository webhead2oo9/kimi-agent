"""Managed configuration revisions and built-in proposal executors."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config.model_config import ModelConfig, parse_model_config_text
from config.settings import Settings
from kimi_agent_module_api import (
    ConfigSnapshot,
    ProposalActor,
    ProposalApplyResult,
    ProposalDraft,
    ProposalPreview,
    ProposalRecord,
)
from utils.frontmatter import FrontmatterError, split_frontmatter_strict

RESTART_EXIT_CODE = 75
_HANDSHAKE_ENV = "KIMI_CONTROL_REVISION"
_BOOTSTRAP_FIELDS = frozenset(
    {
        "config_dir",
        "control_plane_enabled",
        "control_plane_dir",
        "control_plane_key",
        "control_plane_auto_restart",
    }
)
_MAINTENANCE_FIELDS = frozenset({"database_path", "database_encryption_key"})
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SNOWFLAKE = re.compile(r"^[0-9]+$")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _atomic_write(path: Path, data: bytes, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if private:
        flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600 if private else 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if private and os.name != "nt":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


class ControlPlaneStore:
    """Filesystem bootstrap state that remains usable while the DB is unavailable."""

    def __init__(
        self,
        root: str | Path,
        *,
        master_key: str = "",
        base_config_dir: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.base_config_dir = (
            Path(base_config_dir).resolve() if base_config_dir is not None else None
        )
        self._master_key = self._decode_key(master_key) if master_key else None
        self._lock = asyncio.Lock()

    @staticmethod
    def _decode_key(value: str) -> bytes:
        try:
            key = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("CONTROL_PLANE_KEY must be valid base64") from exc
        if len(key) != 32:
            raise ValueError("CONTROL_PLANE_KEY must decode to exactly 32 bytes")
        return key

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"active": None, "pending": None, "previous": None}
        if not isinstance(value, dict):
            raise RuntimeError("control-plane state must be a JSON object")
        return value

    def effective_revision(self) -> str | None:
        handshake = os.environ.get(_HANDSHAKE_ENV, "").strip()
        state = self.state()
        if handshake:
            if handshake != state.get("pending"):
                raise RuntimeError("control-plane handshake names a revision that is not pending")
            return handshake
        active = state.get("active")
        return str(active) if active else None

    def revision_dir(self, revision: str) -> Path:
        if not revision or not revision.isalnum():
            raise ValueError("invalid managed configuration revision")
        return self.root / "revisions" / revision

    def effective_config_dir(self, fallback: Path | None = None) -> Path:
        revision = self.effective_revision()
        if revision is not None:
            managed = self.revision_dir(revision) / "config"
            if managed.is_dir():
                return managed
        if self.base_config_dir is not None:
            return self.base_config_dir
        if fallback is not None:
            return fallback
        raise RuntimeError("the control plane has no base configuration directory")

    def read_settings(self, revision: str | None = None) -> dict[str, Any]:
        selected = self.effective_revision() if revision is None else revision
        if selected is None:
            return {}
        path = self.revision_dir(selected) / "settings.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise RuntimeError("managed settings must be a JSON object")
        return value

    def read_models_text(self, base_path: Path, revision: str | None = None) -> str:
        selected = self.effective_revision() if revision is None else revision
        if selected is not None:
            path = self.revision_dir(selected) / "config" / "models.yaml"
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return base_path.read_text(encoding="utf-8")

    def read_document(self, relative_path: Path, revision: str | None = None) -> str:
        selected = self.effective_revision() if revision is None else revision
        root = (
            self.revision_dir(selected) / "config"
            if selected is not None
            else self.effective_config_dir()
        )
        return (root / relative_path).read_text(encoding="utf-8")

    def document_revision(self, relative_path: Path) -> str:
        try:
            content = self.read_document(relative_path).encode()
        except FileNotFoundError:
            content = b""
        return hashlib.sha256(content).hexdigest()

    def target_revision(self, target: str, *, base_models_path: Path | None = None) -> str:
        if target == "settings":
            content = _canonical_json(self.read_settings())
        elif target == "models" and base_models_path is not None:
            content = self.read_models_text(base_models_path).encode()
        else:
            raise ValueError(f"unsupported configuration target {target!r}")
        return hashlib.sha256(content).hexdigest()

    async def stage(
        self,
        *,
        proposal_id: str,
        settings: Mapping[str, Any] | None = None,
        models_text: str | None = None,
        documents: Mapping[Path, str] | None = None,
        live: bool = False,
    ) -> str:
        async with self._lock:
            state = self.state()
            if state.get("pending"):
                raise RuntimeError("another managed configuration revision is pending restart")
            active = state.get("active")
            inherited_settings = self.read_settings(str(active)) if active else {}
            files: dict[str, bytes] = {"settings.json": _canonical_json(inherited_settings)}
            if settings is not None:
                files["settings.json"] = _canonical_json(dict(settings))
            revision = uuid.uuid4().hex
            destination = self.revision_dir(revision)
            destination.mkdir(parents=True, exist_ok=True)
            source_config = (
                self.revision_dir(str(active)) / "config" if active else self.base_config_dir
            )
            if source_config is not None and source_config.is_dir():
                shutil.copytree(source_config, destination / "config", dirs_exist_ok=True)
            else:
                (destination / "config").mkdir(parents=True, exist_ok=True)
            if models_text is not None:
                _atomic_write(destination / "config" / "models.yaml", models_text.encode())
            for relative, document_content in (documents or {}).items():
                _atomic_write(
                    destination / "config" / relative,
                    document_content.encode(),
                )
            for name, file_content in files.items():
                path = destination / name
                if not path.exists():
                    _atomic_write(path, file_content)
            new_state = {
                "active": active,
                "pending": revision,
                "previous": active,
                "proposal_id": proposal_id,
                "activation": "live" if live else "restart",
            }
            _atomic_write(self.state_path, _canonical_json(new_state))
            return revision

    def mark_healthy(self, revision: str) -> None:
        state = self.state()
        if state.get("pending") != revision:
            raise RuntimeError("cannot promote a revision that is not pending")
        _atomic_write(
            self.state_path,
            _canonical_json(
                {
                    "active": revision,
                    "pending": None,
                    "previous": state.get("previous"),
                    "proposal_id": state.get("proposal_id"),
                    "healthy": True,
                }
            ),
        )

    def rollback_pending(self, *, reason: str) -> None:
        state = self.state()
        if not state.get("pending"):
            return
        _atomic_write(
            self.state_path,
            _canonical_json(
                {
                    "active": state.get("previous"),
                    "pending": None,
                    "previous": state.get("previous"),
                    "proposal_id": state.get("proposal_id"),
                    "rollback_reason": reason,
                }
            ),
        )

    async def stage_secret(self, name: str, value: str) -> str:
        if self._master_key is None:
            raise RuntimeError("CONTROL_PLANE_KEY is required to manage credentials")
        if not name.strip() or not value:
            raise ValueError("secret name and value must not be empty")
        async with self._lock:
            document = self._read_secret_document()
            secret_id = uuid.uuid4().hex
            nonce = secrets.token_bytes(12)
            associated = f"kimi-control:{secret_id}:{name.strip()}".encode()
            encrypted = AESGCM(self._master_key).encrypt(nonce, value.encode(), associated)
            document[secret_id] = {
                "name": name.strip(),
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(encrypted).decode(),
            }
            _atomic_write(self.root / "secrets.enc.json", _canonical_json(document), private=True)
            return f"secret://{secret_id}"

    def resolve_secret(self, reference: str) -> str:
        if self._master_key is None or not reference.startswith("secret://"):
            raise ValueError("invalid or unavailable managed secret reference")
        secret_id = reference.removeprefix("secret://")
        entry = self._read_secret_document().get(secret_id)
        if not isinstance(entry, dict):
            raise KeyError(f"managed secret {secret_id!r} does not exist")
        name = str(entry["name"])
        nonce = base64.b64decode(str(entry["nonce"]), validate=True)
        encrypted = base64.b64decode(str(entry["ciphertext"]), validate=True)
        associated = f"kimi-control:{secret_id}:{name}".encode()
        return AESGCM(self._master_key).decrypt(nonce, encrypted, associated).decode()

    def _read_secret_document(self) -> dict[str, Any]:
        try:
            value = json.loads((self.root / "secrets.enc.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise RuntimeError("managed secret store must be a JSON object")
        return value


def _resolved_settings(
    inherited: Mapping[str, Any], overrides: Mapping[str, Any], store: ControlPlaneStore
) -> Settings:
    values = dict(inherited)
    for field, value in overrides.items():
        if field in _BOOTSTRAP_FIELDS:
            raise ValueError(f"bootstrap field {field!r} cannot be managed")
        if isinstance(value, str) and value.startswith("secret://"):
            value = store.resolve_secret(value)
        values[field] = value
    candidate = Settings.model_validate(values)
    if "kimi_modules" in overrides:
        from app.modules import validate_module_selection

        validate_module_selection(candidate.kimi_module_list, core_settings=candidate)
    if "plugin_modules" in overrides:
        from app.plugins import validate_plugin_selection

        validate_plugin_selection(candidate.plugin_module_list)
    return candidate


def apply_managed_settings(settings: Settings, store: ControlPlaneStore) -> None:
    overrides = store.read_settings()
    if not overrides:
        return
    candidate = _resolved_settings(settings.model_dump(mode="python"), overrides, store)
    for field in Settings.model_fields:
        if field not in _BOOTSTRAP_FIELDS:
            setattr(settings, field, getattr(candidate, field))


def managed_models_path(settings: Settings, store: ControlPlaneStore | None) -> Path:
    base = Path(settings.config_dir) / "models.yaml"
    if store is None:
        return base
    revision = store.effective_revision()
    if revision is None:
        return base
    managed = store.revision_dir(revision) / "config" / "models.yaml"
    return managed if managed.is_file() else base


class RestartCoordinator:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._requested = False
        self.reason = ""
        self.revision = ""
        self._shutdown: Callable[[], Awaitable[None]] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None

    @property
    def requested(self) -> bool:
        return self._requested

    def bind(self, shutdown: Callable[[], Awaitable[None]]) -> None:
        self._shutdown = shutdown

    async def request(self, *, reason: str, revision: str) -> None:
        self._requested = True
        self.reason = reason
        self.revision = revision
        if self._enabled and self._shutdown is not None:
            self._shutdown_task = asyncio.create_task(self._delayed_shutdown())

    async def _delayed_shutdown(self) -> None:
        await asyncio.sleep(1)
        if self._shutdown is not None:
            await self._shutdown()


def _redact(values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values.items():
        lowered = str(key).casefold()
        if any(term in lowered for term in ("secret", "password", "token", "api_key")):
            result[str(key)] = "[REDACTED]"
        else:
            result[str(key)] = value
    return result


def _merge_patch(document: Any, patch: Any) -> Any:
    if not isinstance(patch, Mapping):
        return patch
    result = dict(document) if isinstance(document, Mapping) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


class SettingsProposalHandler:
    def __init__(
        self,
        store: ControlPlaneStore,
        inherited: Mapping[str, Any],
        restart: RestartCoordinator,
    ) -> None:
        self._store = store
        self._inherited = dict(inherited)
        self._restart = restart

    async def preview(self, draft: ProposalDraft) -> ProposalPreview:
        overrides = self._patched(draft.changes)
        _resolved_settings(self._inherited, overrides, self._store)
        return ProposalPreview(
            revision=self._store.target_revision("settings"),
            redacted_changes=_redact(draft.changes),
            activation="restart",
        )

    async def apply(self, proposal: ProposalRecord) -> ProposalApplyResult:
        overrides = self._patched(proposal.changes)
        _resolved_settings(self._inherited, overrides, self._store)
        revision = await self._store.stage(proposal_id=proposal.proposal_id, settings=overrides)
        await self._restart.request(reason=proposal.summary, revision=revision)
        return ProposalApplyResult(
            activation="restart",
            revision=revision,
            message="Managed settings staged; restart requested.",
        )

    def _patched(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        overrides = self._store.read_settings()
        for field, value in changes.items():
            if field in _BOOTSTRAP_FIELDS:
                raise ValueError(f"bootstrap field {field!r} cannot be managed")
            if field in _MAINTENANCE_FIELDS:
                raise ValueError(
                    f"maintenance field {field!r} is not mutable in this experimental cut"
                )
            if isinstance(value, Mapping) and value.get("$inherit") is True:
                overrides.pop(field, None)
            else:
                overrides[field] = value
        return overrides


class ModelsProposalHandler:
    def __init__(
        self,
        store: ControlPlaneStore,
        base_path: Path,
        restart: RestartCoordinator,
    ) -> None:
        self._store = store
        self._base_path = base_path
        self._restart = restart

    async def preview(self, draft: ProposalDraft) -> ProposalPreview:
        current = self._current_document()
        candidate = self._candidate(current, draft.changes)
        ModelConfig.model_validate(candidate)
        return ProposalPreview(
            revision=self._store.target_revision("models", base_models_path=self._base_path),
            redacted_changes=_redact(draft.changes),
            activation="restart",
        )

    async def apply(self, proposal: ProposalRecord) -> ProposalApplyResult:
        candidate = self._candidate(self._current_document(), proposal.changes)
        ModelConfig.model_validate(candidate)
        text = yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True)
        parse_model_config_text(text)
        revision = await self._store.stage(proposal_id=proposal.proposal_id, models_text=text)
        await self._restart.request(reason=proposal.summary, revision=revision)
        return ProposalApplyResult(
            activation="restart",
            revision=revision,
            message="Managed model configuration staged; restart requested.",
        )

    def _current_document(self) -> dict[str, Any]:
        parsed = yaml.safe_load(self._store.read_models_text(self._base_path)) or {}
        if not isinstance(parsed, dict):
            raise ValueError("models configuration must be a mapping")
        return parsed

    @staticmethod
    def _candidate(current: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
        replacement = changes.get("document")
        if replacement is not None:
            if not isinstance(replacement, Mapping):
                raise ValueError("models document replacement must be a mapping")
            return dict(replacement)
        return dict(_merge_patch(current, changes))


def _document_target(target: str) -> tuple[Path, bool]:
    kind, separator, identifier = target.partition(":")
    if not separator or not identifier:
        raise ValueError("document target must use '<kind>:<identifier>'")
    if kind in {"guild", "channel"}:
        if not _SNOWFLAKE.fullmatch(identifier) or int(identifier) <= 0:
            raise ValueError(f"{kind} target must be a positive numeric Discord id")
        return Path("servers" if kind == "guild" else "channels") / f"{identifier}.md", True
    if kind in {"tool", "module", "plugin"}:
        if not _SAFE_NAME.fullmatch(identifier):
            raise ValueError(f"invalid {kind} configuration name")
        return Path(f"{kind}s") / f"{identifier}.md", kind == "tool"
    if kind == "prompt":
        roots = {
            "root": Path("prompt.md"),
            "persona": Path("persona.md"),
            "tools": Path("tools.md"),
        }
        if identifier in roots:
            return roots[identifier], True
        pieces = Path(identifier).parts
        if not pieces or any(not _SAFE_NAME.fullmatch(piece) for piece in pieces):
            raise ValueError("invalid prompt fragment path")
        relative = Path("prompts").joinpath(*pieces)
        if relative.suffix != ".md":
            relative = relative.with_suffix(".md")
        return relative, True
    raise ValueError(f"unsupported managed document kind {kind!r}")


def _validate_document(target: str, content: str) -> None:
    """Reject malformed managed fragments before they enter a revision."""
    if len(content.encode("utf-8")) > 1_000_000:
        raise ValueError("managed configuration document exceeds 1 MB")
    kind = target.partition(":")[0]
    try:
        metadata, body = split_frontmatter_strict(content)
    except FrontmatterError as exc:
        raise ValueError(f"invalid {kind} configuration document: {exc}") from exc
    if kind in {"module", "plugin"}:
        if content.strip() and not content.lstrip().startswith("---"):
            raise ValueError(f"{kind} configuration must use YAML frontmatter")
        if body.strip():
            raise ValueError(f"{kind} configuration cannot contain a Markdown body")
    if kind == "guild" and "bot_active" in metadata:
        from config.fragments.guild_config import server_setup_activation

        if server_setup_activation(content) is None:
            raise ValueError("guild configuration has invalid activation or trust fields")


class DocumentProposalHandler:
    def __init__(
        self,
        store: ControlPlaneStore,
        activate_live: Callable[[Path], Awaitable[None]],
        restart: RestartCoordinator,
    ) -> None:
        self._store = store
        self._activate_live = activate_live
        self._restart = restart

    async def preview(self, draft: ProposalDraft) -> ProposalPreview:
        relative, live = _document_target(draft.target)
        content = draft.changes.get("content")
        if not isinstance(content, str):
            raise ValueError("document changes require a string content field")
        _validate_document(draft.target, content)
        return ProposalPreview(
            revision=self._store.document_revision(relative),
            redacted_changes={"content": content},
            activation="live" if live else "restart",
        )

    async def apply(self, proposal: ProposalRecord) -> ProposalApplyResult:
        relative, live = _document_target(proposal.target)
        content = proposal.changes.get("content")
        if not isinstance(content, str):
            raise ValueError("document changes require a string content field")
        _validate_document(proposal.target, content)
        revision = await self._store.stage(
            proposal_id=proposal.proposal_id,
            documents={relative: content},
            live=live,
        )
        if live:
            previous_config = self._store.effective_config_dir()
            try:
                await self._activate_live(self._store.revision_dir(revision) / "config")
            except Exception:
                self._store.rollback_pending(reason="live configuration activation failed")
                with contextlib.suppress(Exception):
                    await self._activate_live(previous_config)
                raise
            self._store.mark_healthy(revision)
            message = "Managed configuration document activated live."
            activation: Literal["live", "restart"] = "live"
        else:
            await self._restart.request(reason=proposal.summary, revision=revision)
            message = "Managed module/plugin configuration staged; restart requested."
            activation = "restart"
        return ProposalApplyResult(
            activation=activation,
            revision=revision,
            message=message,
        )


class ManagedConfigurationService:
    def __init__(
        self,
        *,
        proposals: Any,
        store: ControlPlaneStore,
        settings: Settings,
        inherited_settings: Mapping[str, Any],
        restart: RestartCoordinator,
    ) -> None:
        self._proposals = proposals
        self._store = store
        self._settings = settings
        self._live_activation: Callable[[Path], Awaitable[None]] | None = None
        self._base_models_path = Path(settings.config_dir) / "models.yaml"
        proposals.register_handler(
            "core",
            "config.settings.update",
            SettingsProposalHandler(store, inherited_settings, restart),
        )
        proposals.register_handler(
            "core",
            "config.models.update",
            ModelsProposalHandler(store, self._base_models_path, restart),
        )
        proposals.register_handler(
            "core",
            "config.document.update",
            DocumentProposalHandler(store, self._activate_live, restart),
        )

    def bind_live_activation(self, callback: Callable[[Path], Awaitable[None]]) -> None:
        self._live_activation = callback

    async def _activate_live(self, config_dir: Path) -> None:
        if self._live_activation is None:
            raise RuntimeError("live configuration activation is not bound")
        await self._live_activation(config_dir)

    async def snapshot(self, target: str) -> ConfigSnapshot:
        if target == "settings":
            values = _redact(self._settings.model_dump(mode="python"))
            revision = self._store.target_revision("settings")
        elif target == "models":
            model = parse_model_config_text(self._store.read_models_text(self._base_models_path))
            values = model.model_dump(mode="json")
            revision = self._store.target_revision(
                "models", base_models_path=self._base_models_path
            )
        else:
            relative, _live = _document_target(target)
            try:
                content = self._store.read_document(relative)
            except FileNotFoundError:
                content = ""
            values = {"content": content}
            revision = self._store.document_revision(relative)
        return ConfigSnapshot(target=target, revision=revision, values=values)

    async def propose(
        self,
        module_name: str,
        *,
        target: str,
        changes: Mapping[str, Any],
        summary: str,
        actor: ProposalActor,
        expected_revision: str | None = None,
    ) -> ProposalRecord:
        if target == "settings":
            action = "config.settings.update"
        elif target == "models":
            action = "config.models.update"
        else:
            _document_target(target)
            action = "config.document.update"
        return await self._proposals.create(
            module_name,
            ProposalDraft(
                action=action,
                target=target,
                summary=summary,
                changes=changes,
                actor=actor,
                expected_revision=expected_revision,
            ),
        )

    async def stage_secret(self, name: str, value: str) -> str:
        return await self._store.stage_secret(name, value)


__all__ = [
    "RESTART_EXIT_CODE",
    "ControlPlaneStore",
    "ManagedConfigurationService",
    "RestartCoordinator",
    "apply_managed_settings",
    "managed_models_path",
]
