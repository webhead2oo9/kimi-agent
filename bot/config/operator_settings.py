"""Operator-editable settings, layered over the environment at startup.

Scalar application preferences in :class:`config.settings.Settings` are
generated into :data:`SETTINGS_SPEC`, each with the validation rules its value
must satisfy. ``<CONFIG_DIR>/settings.md`` is a hand-edited, frontmatter-only
file read once at startup.

Eligibility is fail-closed around deployment boundaries. Secrets and credentials,
filesystem paths and binaries, database/encryption controls, arbitrary plugin
imports, and service endpoint URLs stay
environment-only. Adding an ordinary bool/int/float/text setting automatically
puts it in the catalog instead of silently leaving a second configuration surface.

**Precedence: the file wins over the environment.** The file is the
operator's deliberate edit: if a value also sits in ``.env`` and the
environment won, an edit to the file would silently do nothing. Anything the
file does not mention is untouched, so a key only ever set in ``.env`` keeps
working exactly as before.

**Live versus restart.** Most settings are captured into frozen config objects
while ``build_app`` wires the application, so changing them afterwards has no
effect until the process restarts. Nothing in this overlay is re-read after
boot: every field here is restart-required, because telling an operator a
change is live when it is not is worse than telling them to restart when they
did not have to. Live per-call values belong to per-tool config fragments.

**Fail closed when present.** A missing or empty overlay means "inherit
everything." Any unreadable, malformed, unknown, or invalid present setting
stops startup before one field is applied, so a broken trust allowlist can never
silently widen access.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import NoneType
from typing import Any, get_args

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError as PydanticValidationError

from config import field_kinds, paths
from config.field_kinds import coerce_scalar
from config.settings import Settings
from providers.types import REASONING_EFFORT_ORDER
from utils.frontmatter import FrontmatterError, find_frontmatter

log = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.md"


class OperatorSettingsError(RuntimeError):
    """The present operator overlay is unsafe to apply."""


# Value shapes the file and the Settings model agree on. The scalar kinds are
# shared with the plugin and per-tool config surfaces (config/field_kinds.py);
# id_list is specific to this one.
KIND_INT = field_kinds.KIND_INT
KIND_FLOAT = field_kinds.KIND_FLOAT
KIND_BOOL = field_kinds.KIND_BOOL
KIND_TEXT = field_kinds.KIND_TEXT
KIND_CHOICE = field_kinds.KIND_CHOICE
KIND_ID_LIST = "id_list"  # YAML list in the file, comma-joined str on Settings


@dataclass(frozen=True)
class SettingSpec:
    """One overlay-eligible Settings field and the rules its value must satisfy.

    Every attribute here is load-bearing for validation. Presentation metadata
    (labels, help text, form grouping) is deliberately absent: ``SettingSpec``
    stays validation-only, and a future console should carry its own.
    """

    field: str
    kind: str
    choices: tuple[str, ...] = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    nullable: bool = False


_EXCLUDED_FIELDS = frozenset(
    {
        "plugin_modules",
        "kimi_modules",
        "code_exec_extra_ro_binds",
        "code_exec_netns_resolv_conf",
        "browser_netns_resolv_conf",
        # Captured content can include prompts, memories, tool payloads, and
        # secrets, so choosing how much to keep is an environment decision. The
        # overlay can still start and stop the metadata-only writer through
        # tool_event_log_enabled.
        "tool_event_log_content_mode",
        # Importable module names are code execution: letting a data file pick
        # them would make the settings overlay an RCE surface. Plugin and
        # application-module selection are environment-only, like the bind
        # host, port, and auth.
    }
)
_EXCLUDED_SUFFIXES = ("_dir", "_path", "_bin", "_file", "_url", "_base")

# Only vocabularies some consumer in this repo enforces or maps. A value passed
# verbatim to a third-party API is left as free text: rejecting a value the
# endpoint accepts is worse than accepting one it does not.
_CHOICES: dict[str, tuple[str, ...]] = {
    "image_detail": ("auto", "low", "high", "original"),
    "memory_recall_budget": ("low", "mid", "high"),
    "moderation_output_exempt_tier": ("", "member", "regular", "staff"),
    "internet_search_safesearch": ("off", "moderate", "strict"),
    "browser_network_mode": ("host", "netns"),
    # config/model_config.py validates the per-profile reasoning_effort against
    # exactly this ladder; blank falls back to the provider default.
    "codex_reasoning_effort": ("", *REASONING_EFFORT_ORDER),
}

# 0 means "this value legitimately turns something off or has no lower bound"
# (caps where 0 = unlimited/disabled, thresholds, USD rates, temperature). 1 (or
# a documented floor) means "0 breaks the feature". Floors are deliberately
# minimal: this rejects nonsense, it does not opine on tuning.
_FLOAT_TIMEOUT_FLOOR = 0.1  # positive, but never rejects a sub-second budget

_MINIMUMS: dict[str, int | float] = {
    # ── Conversation ─────────────────────────────────────────────────────────
    "react_max_iterations": 1,
    "react_max_tokens": 1024,
    "react_turn_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "user_app_chat_timeout_seconds": 1.0,
    "react_temperature": 0,
    "new_user_onboarding_turns": 0,  # 0 disables the onboarding note
    "thread_handoff_suggest_after_tool_calls": 0,  # 0 disables the suggestion
    "compaction_trigger_tokens": 1,
    "compaction_keep_recent_iterations": 1,
    "compaction_keep_recent_tokens": 1,
    "compaction_max_tokens": 1,
    "compaction_max_iteration_tool_output_tokens": 1,
    "recent_image_lookback": 0,  # 0 disables image lookback
    "max_turn_images": 0,  # 0 disables image input
    "attachment_max_bytes": 1,
    "attachment_max_total_bytes": 1,
    # ── Discord scope ────────────────────────────────────────────────────────
    "discord_search_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "internet_search_backend_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "internet_search_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "internet_search_max_results": 1,
    "internet_search_max_backend_calls_per_turn": 1,
    "internet_search_max_output_chars": 1,
    "video_understanding_max_concurrency": 1,
    "image_gen_max_concurrency": 1,
    "image_gen_timeout_seconds": 30.0,
    "exa_search_cost_usd": 0,
    "exa_contents_cost_usd": 0,
    "brave_search_cost_usd": 0,
    "wolfram_alpha_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "wolfram_alpha_max_calls_per_turn": 1,
    "wolfram_alpha_max_output_chars": 500,
    "wolfram_alpha_call_cost_usd": 0,
    # ── Providers ────────────────────────────────────────────────────────────
    "provider_stream_stall_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "codex_ws_idle_timeout": 1,
    "codex_ws_read_timeout": _FLOAT_TIMEOUT_FLOOR,
    # ── Memory ───────────────────────────────────────────────────────────────
    "memory_recall_max_tokens": 0,
    "memory_max_writes_per_turn": 0,  # 0 disables proactive writes
    "memory_auto_retain_idle_minutes": 1,
    "memory_auto_retain_sweep_interval_seconds": 1,
    "memory_auto_retain_min_user_chars": 0,  # 0 skips nothing
    "memory_auto_retain_max_content_chars": 1,
    "memory_auto_retain_backfill_horizon_hours": 0,  # 0 backfills nothing
    "memory_auto_retain_max_flushes_per_sweep": 1,
    "user_persona_max_chars": 1,
    "user_persona_request_max_chars": 1,
    "user_persona_compiler_max_tokens": 1,
    # ── Moderation ───────────────────────────────────────────────────────────
    "moderation_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    # ── Code execution ───────────────────────────────────────────────────────
    "code_exec_wall_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "code_exec_max_cpu_seconds": 1,
    "code_exec_max_memory_mb": 1,
    "code_exec_max_tasks": 1,
    "code_exec_max_total_memory_mb": 1,
    "code_exec_cpu_quota_percent": 1,
    "code_exec_tmp_size_mb": 1,
    "code_exec_max_fsize_mb": 1,
    "code_exec_max_open_files": 1,
    "code_exec_max_workspace_files": 1,
    "code_exec_workspace_quota_poll_seconds": _FLOAT_TIMEOUT_FLOOR,
    "code_exec_workspace_quota_scan_retries": 1,
    "code_exec_max_output_bytes": 1,
    "code_exec_max_concurrency": 1,
    "code_exec_env_dir_max_mb": 1,
    "code_exec_env_dir_max_files": 1,
    "code_exec_network_weekly_limit": 0,
    "coding_task_max_concurrency": 1,
    "coding_task_max_queued_per_workspace": 1,
    "coding_task_max_queued_per_user": 1,
    "coding_task_max_seconds": _FLOAT_TIMEOUT_FLOOR,
    "coding_provider_call_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "coding_job_max_seconds": _FLOAT_TIMEOUT_FLOOR,
    "coding_job_max_cpu_seconds": 1,
    "coding_worker_stall_seconds": _FLOAT_TIMEOUT_FLOOR,
    "coding_status_min_interval_seconds": _FLOAT_TIMEOUT_FLOOR,
    "coding_stop_cleanup_wait_seconds": _FLOAT_TIMEOUT_FLOOR,
    "coding_task_max_iterations": 1,
    "browser_call_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "browser_start_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "browser_idle_ttl_seconds": _FLOAT_TIMEOUT_FLOOR,
    "browser_worker_max_lifetime_seconds": 1,
    "browser_profile_ttl_seconds": 1,
    "browser_max_profile_mb": 1,
    "browser_max_screenshot_bytes": 1,
    "browser_max_total_memory_mb": 1,
    "browser_max_tasks": 1,
    "browser_cpu_quota_percent": 1,
    "browser_tmp_size_mb": 1,
    "browser_max_fsize_mb": 1,
    "browser_max_open_files": 1,
    # ── Workspace ────────────────────────────────────────────────────────────
    "workspace_tool_max_file_bytes": 1,
    "workspace_tool_max_user_bytes": 1,
    "workspace_tool_max_read_bytes": 1,
    "workspace_tool_max_pdf_pages": 1,
    "workspace_tool_max_text_chars": 1,
    "workspace_tool_max_attachments": 0,  # 0 attaches nothing automatically
    "workspace_tool_max_import_bytes": 1,
    "workspace_tool_max_zip_entries": 1,
    "workspace_tool_max_extract_total_bytes": 1,
    "workspace_tool_fetch_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "workspace_tool_max_redirects": 0,  # 0 follows no redirects
    "workspace_tool_default_grep_results": 1,
    "workspace_tool_max_grep_results": 1,
    "workspace_tool_max_grep_context": 0,  # 0 is plain grep, no context lines
    "workspace_tool_max_grep_line_chars": 1,
    "workspace_tool_max_grep_pattern_chars": 1,
    "workspace_tool_grep_timeout_seconds": _FLOAT_TIMEOUT_FLOOR,
    "workspace_tool_glob_max_results": 1,
    "workspace_tool_multi_edit_max_ops": 1,
    "workspace_tool_view_image_max_bytes": 1,
    "workspace_tool_view_image_max_per_turn": 1,
    "workspace_tool_max_entries": 1,
    # ── Scripts ──────────────────────────────────────────────────────────────
    "script_default_timeout": 1,
    "script_max_timeout": 1,
    "script_max_concurrency": 1,
    "script_output_max_chars": 1,
    "script_output_max_files": 1,
    "script_output_max_file_bytes": 1,
    "script_output_max_scan_entries": 1,
    "script_sandbox_memory_max_mb": 1,
    "script_sandbox_cpu_seconds": 1,
    "script_sandbox_max_file_bytes": 1,
    "script_sandbox_max_open_files": 1,
    "script_sandbox_max_processes": 1,
    "script_sandbox_tmpfs_max_mb": 1,
    # ── Limits & quotas ──────────────────────────────────────────────────────
    "llm_max_concurrency": 1,
    "turn_max_concurrency": 1,
    "turn_max_concurrency_per_user": 1,
    # ── Retention ────────────────────────────────────────────────────────────
    "transcript_retention_days": 0,  # 0 disables the sweep
    "transcript_retention_sweep_interval_seconds": 1,
    "workspace_file_ttl": 60,
    "workspace_max_size_mb": 1,
    "workspace_sweep_interval": 1,
    "attachment_orphan_ttl_seconds": 1,
    "attachment_orphan_sweep_interval_seconds": 1,
    "attachment_orphan_sweep_max_files": 1,
    # ── Features ─────────────────────────────────────────────────────────────
    "privacy_consent_timeout": _FLOAT_TIMEOUT_FLOOR,
    "tool_event_log_max_field_bytes": 0,  # 0 disables field truncation
}


def _is_eligible(field: str, annotation: Any) -> bool:
    if field in _EXCLUDED_FIELDS or field.startswith("database_"):
        return False
    if field.endswith(_EXCLUDED_SUFFIXES):
        return False
    args = get_args(annotation)
    members = set(args) if args else {annotation}
    members.discard(NoneType)
    return len(members) == 1 and next(iter(members)) in {bool, int, float, str}


def _kind_for(field: str, annotation: Any) -> tuple[str, bool]:
    args = get_args(annotation)
    nullable = NoneType in args
    scalar = next(member for member in (args or (annotation,)) if member is not NoneType)
    if field in _CHOICES:
        return KIND_CHOICE, nullable
    if scalar is bool:
        return KIND_BOOL, nullable
    if scalar is int:
        return KIND_INT, nullable
    if scalar is float:
        return KIND_FLOAT, nullable
    if field.endswith("_ids"):
        return KIND_ID_LIST, nullable
    return KIND_TEXT, nullable


def _build_settings_spec() -> tuple[SettingSpec, ...]:
    """Every overlay-eligible field, in config/settings.py declaration order."""
    specs: list[SettingSpec] = []
    for field, model_field in Settings.model_fields.items():
        annotation = model_field.annotation
        if not _is_eligible(field, annotation):
            continue
        kind, nullable = _kind_for(field, annotation)
        specs.append(
            SettingSpec(
                field=field,
                kind=kind,
                choices=_CHOICES.get(field, ()),
                minimum=_MINIMUMS.get(field),
                nullable=nullable,
            )
        )
    return tuple(specs)


SETTINGS_SPEC = _build_settings_spec()
_BY_FIELD = {spec.field: spec for spec in SETTINGS_SPEC}


def spec_for(field: str) -> SettingSpec | None:
    """The validation spec for one field, or None if it is not overlay-eligible."""
    return _BY_FIELD.get(field)


def settings_file_path(config_dir: Path | None = None) -> Path:
    return (config_dir or paths.default_config_dir()) / SETTINGS_FILENAME


def coerce_value(spec: SettingSpec, raw: Any) -> Any:
    """Convert one form/frontmatter value into the corresponding Settings value."""
    if raw is None:
        if spec.nullable:
            return None
        raise ValueError("null is not allowed")
    if spec.kind == KIND_ID_LIST:
        if isinstance(raw, str):
            entries = [token.strip() for token in raw.split(",")]
        elif isinstance(raw, (list, tuple)):
            entries = [str(entry).strip() for entry in raw]
        else:
            raise ValueError("expected a list of ids")
        kept = [entry for entry in entries if entry]
        for entry in kept:
            if not entry.isdigit():
                raise ValueError("Discord ids must be numeric")
        return ",".join(kept)
    return coerce_scalar(
        spec.kind,
        raw,
        choices=spec.choices,
        minimum=spec.minimum,
        maximum=spec.maximum,
    )


def settings_values(settings: Any) -> dict[str, Any]:
    """Return every managed field in the JSON/form representation."""
    values: dict[str, Any] = {}
    for spec in SETTINGS_SPEC:
        current = getattr(settings, spec.field)
        if spec.kind == KIND_ID_LIST:
            values[spec.field] = [
                token.strip() for token in str(current or "").split(",") if token.strip()
            ]
        else:
            values[spec.field] = current
    return values


def _parse_operator_document(text: str, path: Path) -> dict[str, Any]:
    """Parse one required-valid frontmatter-only document."""
    if not text.strip():
        return {}
    try:
        found = find_frontmatter(text)
    except FrontmatterError as exc:
        log.error("Invalid operator settings %s: %s", path, exc)
        raise OperatorSettingsError(f"Invalid operator settings {path}: {exc}") from exc
    if found is None:
        message = "expected a frontmatter-only settings document"
        log.error("Invalid operator settings %s: %s", path, message)
        raise OperatorSettingsError(f"Invalid operator settings {path}: {message}")
    raw_frontmatter, body = found
    if body.strip():
        message = "content after settings frontmatter is not allowed"
        log.error("Invalid operator settings %s: %s", path, message)
        raise OperatorSettingsError(f"Invalid operator settings {path}: {message}")
    try:
        # Parsed here rather than through the shared helper so the operator gets
        # the line and column of their mistake.
        parsed = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        message = f"invalid YAML frontmatter{location}"
        log.error("Invalid operator settings %s: %s", path, message)
        raise OperatorSettingsError(f"Invalid operator settings {path}: {message}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        message = "frontmatter is not a mapping"
        log.error("Invalid operator settings %s: %s", path, message)
        raise OperatorSettingsError(f"Invalid operator settings {path}: {message}")
    return parsed


def load_operator_settings(*, config_dir: Path | None = None) -> dict[str, Any]:
    """Load the optional overlay, rejecting every present invalid document."""
    path = settings_file_path(config_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        log.error("Unable to read operator settings %s: %s", path, exc)
        raise OperatorSettingsError(f"Unable to read operator settings {path}") from exc
    meta = _parse_operator_document(text, path)

    resolved: dict[str, Any] = {}
    for key, raw in meta.items():
        spec = _BY_FIELD.get(str(key))
        if spec is None:
            message = f"unknown setting {key!r}"
            log.error("Invalid operator settings %s: %s", path, message)
            raise OperatorSettingsError(f"Invalid operator settings {path}: {message}")
        try:
            resolved[spec.field] = coerce_value(spec, raw)
        except ValueError as exc:
            log.error("Invalid operator setting %r in %s: %s", key, path, exc)
            raise OperatorSettingsError(
                f"Invalid operator setting {key!r} in {path}: {exc}"
            ) from exc
    return resolved


def apply_operator_settings(settings: Settings, *, config_dir: Path | None = None) -> list[str]:
    """Validate the complete overlay candidate, then apply it without partial state."""
    overlay = load_operator_settings(config_dir=config_dir)
    if not overlay:
        return []
    candidate_data = settings.model_dump(mode="python")
    candidate_data.update(overlay)
    try:
        candidate = type(settings).model_validate(candidate_data)
    except PydanticValidationError as exc:
        path = settings_file_path(config_dir)
        log.error("Invalid complete operator settings candidate from %s", path)
        raise OperatorSettingsError(
            f"Invalid complete operator settings candidate from {path}"
        ) from exc

    settings.__dict__.update({field: getattr(candidate, field) for field in overlay})
    settings.__pydantic_fields_set__.update(overlay)
    applied = sorted(overlay)
    log.info(
        "Applied %d operator setting(s) from %s: %s",
        len(applied),
        settings_file_path(config_dir),
        ", ".join(applied),
    )
    return applied
