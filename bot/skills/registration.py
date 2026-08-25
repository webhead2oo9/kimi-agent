from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from config.settings import settings
from workspace import WorkspaceManager
from skills.loader import SKILL_FILENAME, SkillToolDeclaration, _parse_skill_file, scan_skills
from skills.policy import normalize_skill_tool_min_tier
from skills.runner import ScriptResult, run_script
from skills.sandbox import ScriptSandboxLimits
from skills.secrets import resolve_secrets
from tools._common import tool_error
from tools.output_queue import AttachmentLimitError, enqueue_output_file
from tools.registry import MessageContext, ToolRegistry
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier

log = logging.getLogger(__name__)


def _parse_min_tier(value: str | None) -> TrustTier:
    try:
        return normalize_skill_tool_min_tier(value)
    except ValueError as exc:
        raise ValueError(f"Invalid min_tier: {value!r}") from exc


def _build_parameters(tool_decl: SkillToolDeclaration) -> dict:
    properties = {}
    required_params = []
    for name, param_def in tool_decl.parameters.items():
        properties[name] = {
            "type": param_def.type,
            "description": param_def.description,
        }
        required_params.append(name)

    parameters = {"type": "object", "properties": properties}
    if required_params:
        parameters["required"] = required_params
    return parameters


def build_script_tool_handler(
    tool_decl: SkillToolDeclaration,
    *,
    source_dir: Path,
    resolved_secrets: dict[str, str],
    workspace_manager: WorkspaceManager,
    script_semaphore: asyncio.Semaphore | None = None,
    workspace_locks: UserLocks | None = None,
):
    """Build the script-backed handler coroutine for a declared tool.

    Executable skills run through the mandatory Linux sandbox in skills/runner.py.
    """
    timeout = min(
        tool_decl.timeout if tool_decl.timeout is not None else settings.script_default_timeout,
        settings.script_max_timeout,
    )
    sandbox_limits = ScriptSandboxLimits(
        memory_bytes=settings.script_sandbox_memory_max_mb * 1024 * 1024,
        cpu_seconds=settings.script_sandbox_cpu_seconds,
        file_size_bytes=settings.script_sandbox_max_file_bytes,
        open_files=settings.script_sandbox_max_open_files,
        processes=settings.script_sandbox_max_processes,
        tmpfs_bytes=settings.script_sandbox_tmpfs_max_mb * 1024 * 1024,
    )

    async def _handler(
        args: dict,
        ctx: MessageContext,
        _script: str = tool_decl.script,
        _skill_dir: Path = source_dir,
        _secrets: dict[str, str] = resolved_secrets,
        _timeout: int = timeout,
        _workspace_manager: WorkspaceManager = workspace_manager,
        _script_semaphore: asyncio.Semaphore | None = script_semaphore,
        _sandbox_limits: ScriptSandboxLimits = sandbox_limits,
        _allow_network: bool = tool_decl.network,
    ) -> str:
        workspace_dir = None
        if ctx.user_id:
            workspace_dir = str(_workspace_manager.create_job_dir(ctx.workspace_key).resolve())

        async def _run() -> ScriptResult:
            return await run_script(
                script_path=_script,
                skill_dir=_skill_dir,
                arguments=args,
                secrets=_secrets,
                timeout=_timeout,
                workspace_dir=workspace_dir,
                max_output_chars=settings.script_output_max_chars,
                max_output_files=settings.script_output_max_files,
                max_output_file_bytes=settings.script_output_max_file_bytes,
                max_output_scan_entries=settings.script_output_max_scan_entries,
                allow_network=_allow_network,
                sandbox_limits=_sandbox_limits,
            )

        if _script_semaphore is None:
            result = await _run()
        else:
            async with _script_semaphore:
                result = await _run()

        if result.timed_out:
            return tool_error(f"Script timed out after {_timeout}s")
        if result.return_code != 0:
            last_line = result.stderr.split("\n")[-1] if result.stderr else "unknown error"
            return tool_error(f"Script failed: {last_line}")
        if result.output_files:
            attached: list[str] = []
            attached_refs: list[dict[str, str]] = []
            not_attached: list[dict[str, str]] = []
            if workspace_dir:
                for output_file in result.output_files:
                    name = Path(output_file).name
                    try:
                        queued = enqueue_output_file(
                            ctx,
                            Path(output_file),
                            Path(workspace_dir),
                            max_attachments=settings.workspace_tool_max_attachments,
                        )
                    except AttachmentLimitError as exc:
                        not_attached.append({"file": name, "reason": str(exc)})
                    except ValueError as exc:
                        # e.g. embed-filename collision; the script still succeeded.
                        not_attached.append({"file": name, "reason": str(exc)})
                    else:
                        attached.append(name)
                        attached_refs.append({"file": name, "remove_id": queued.remove_id})
            else:
                not_attached = [
                    {"file": Path(p).name, "reason": "no workspace for this turn"}
                    for p in result.output_files
                ]
            # Report basenames, not the absolute job-output paths: those paths
            # are not addressable by any workspace tool, and the files are already
            # attached here. Leaking them sends the model chasing queue_file dead ends.
            payload: dict[str, object] = {
                "stdout": result.stdout,
                "attached_files": attached,
            }
            if attached:
                payload["attached_file_refs"] = attached_refs
                payload["note"] = (
                    "These files are already attached to your reply. "
                    "Do not call queue_file to add them again. To remove one, call "
                    "queue_file with action=remove and pass its remove_id as path."
                )
            if not_attached:
                payload["files_not_attached"] = not_attached
            if result.output_files_omitted:
                payload["output_files_omitted"] = result.output_files_omitted
            return json.dumps(payload)
        return result.stdout or json.dumps({"result": "Script completed with no output"})

    async def handler(args: dict, ctx: MessageContext) -> str:
        if workspace_locks is None:
            return await _handler(args, ctx)
        async with workspace_locks.activity(ctx.workspace_key):
            return await _handler(args, ctx)

    return handler


def register_skill_tools(
    skill_dir: Path,
    registry: ToolRegistry,
    secrets: dict[str, str],
    workspace_base_dir: Path | None = None,
    workspace_manager: WorkspaceManager | None = None,
    script_semaphore: asyncio.Semaphore | None = None,
    workspace_locks: UserLocks | None = None,
) -> int:
    skill_file = skill_dir / SKILL_FILENAME
    if not skill_file.exists():
        return 0

    skill = _parse_skill_file(skill_file, strict_tools=True)
    if skill is None or not skill.meta.tools:
        return 0

    resolved_secrets = (
        resolve_secrets(skill.meta.requires_secrets, secrets) if skill.meta.requires_secrets else {}
    )
    # Fail closed on an unresolved secret, matching every other registration site:
    # a missing key means the tool is never registered, not a tool that registers
    # and then fails inside the script with an empty environment variable.
    missing_secrets = [name for name in skill.meta.requires_secrets if name not in resolved_secrets]
    if missing_secrets:
        log.warning(
            "Skipping skill %s: required secret(s) %s not found in the secrets store; "
            "its %d tool(s) are not registered",
            skill.meta.name,
            ", ".join(sorted(missing_secrets)),
            len(skill.meta.tools),
        )
        return 0

    count = 0
    for tool_decl in skill.meta.tools:
        min_tier = _parse_min_tier(tool_decl.min_tier)
        if skill.meta.requires_secrets and min_tier < TrustTier.STAFF:
            log.warning(
                "Raising secret-backed skill tool %s from %s to staff trust",
                tool_decl.name,
                min_tier.name.lower(),
            )
            min_tier = TrustTier.STAFF
        tool_workspace_manager = workspace_manager or WorkspaceManager(
            base_dir=workspace_base_dir or Path(settings.workspace_dir),
            file_ttl=settings.workspace_file_ttl,
            max_size_bytes=settings.workspace_max_size_mb * 1024 * 1024,
        )
        handler = build_script_tool_handler(
            tool_decl,
            source_dir=skill_dir,
            resolved_secrets=resolved_secrets,
            workspace_manager=tool_workspace_manager,
            script_semaphore=script_semaphore,
            workspace_locks=workspace_locks,
        )
        registry.register(
            name=tool_decl.name,
            description=tool_decl.description,
            parameters=_build_parameters(tool_decl),
            handler=handler,
            min_tier=min_tier,
            searchable=tool_decl.availability == "search",
            skill_name=skill.meta.name,
            category="Skills",
            # None => global (every guild); a tuple (incl. the empty () from a
            # parse that failed closed) => that exact guild set, empty = nowhere.
            guild_ids=(frozenset(tool_decl.guild_ids) if tool_decl.guild_ids is not None else None),
        )
        count += 1
        log.info("Registered skill tool: %s (%s)", tool_decl.name, tool_decl.availability)

    return count


def register_all_skill_tools(
    skills_store: Path,
    registry: ToolRegistry,
    secrets: dict[str, str],
    workspace_base_dir: Path | None = None,
    workspace_manager: WorkspaceManager | None = None,
    script_semaphore: asyncio.Semaphore | None = None,
    workspace_locks: UserLocks | None = None,
) -> int:
    count = 0
    for meta in scan_skills(skills_store).values():
        # Isolate each skill: a malformed SKILL.md (strict-parse error, bad
        # min_tier) or a cross-skill tool-name collision must not abort the
        # whole batch and leave the bot with zero skill tools. Registration
        # stages into a throwaway registry, so skipping one skill is clean.
        try:
            count += register_skill_tools(
                skill_dir=meta.path.parent,
                registry=registry,
                secrets=secrets,
                workspace_base_dir=workspace_base_dir,
                workspace_manager=workspace_manager,
                script_semaphore=script_semaphore,
                workspace_locks=workspace_locks,
            )
        except Exception:
            log.exception("Skipping skill %r: executable tool registration failed", meta.name)
    return count


def reload_all_skill_tools(
    skills_store: Path,
    registry: ToolRegistry,
    secrets: dict[str, str],
    workspace_base_dir: Path | None = None,
    workspace_manager: WorkspaceManager | None = None,
    script_semaphore: asyncio.Semaphore | None = None,
    workspace_locks: UserLocks | None = None,
) -> int:
    staged = ToolRegistry()
    count = register_all_skill_tools(
        skills_store=skills_store,
        registry=staged,
        secrets=secrets,
        workspace_base_dir=workspace_base_dir,
        workspace_manager=workspace_manager,
        script_semaphore=script_semaphore,
        workspace_locks=workspace_locks,
    )
    registry.replace_skill_tools_threadsafe(staged.get_all_tools())
    return count
