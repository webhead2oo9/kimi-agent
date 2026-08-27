from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityUpdate:
    label: str
    phase: str = "status"
    tool: str = ""


ActivityReporter = Callable[[ActivityUpdate], Awaitable[None]]


@runtime_checkable
class SupportsNarrationSteps(Protocol):
    async def commit_step(self, narration: str, tool_names: list[str]) -> None: ...


@runtime_checkable
class SupportsPlanUpdates(Protocol):
    async def update_plan(self, steps: list[dict[str, str]]) -> None: ...


# Single source of truth for the friendly tool labels shown to users, both in the
# muted activity subtext under a reply and in the live "..." status line. Phrases are
# gerunds so they read in a comma list ("Searching the web, Editing a file") and with
# a trailing ellipsis ("Searching the web..."). A tool with no entry here falls back to a
# title-cased version of its raw name, so a snake_case name never leaks to users. Add an
# entry for every new tool anyway. Keys are registry tool names (plus the research
# subagent's private tools, which never hit the global registry).
_TOOL_LABELS: dict[str, str] = {
    "start_coding_task": "Starting a coding task",
    "coding_task_status": "Checking a coding task",
    "coding_task_message": "Steering a coding task",
    "coding_task_cancel": "Cancelling a coding task",
    "coding_task_retry_delivery": "Retrying a coding report",
    "coding_plan": "Updating the coding plan",
    "coding_progress": "Reporting coding progress",
    "coding_request_input": "Asking for coding input",
    "coding_job_start": "Starting a coding job",
    "coding_job_status": "Waiting on a coding job",
    "coding_job_cancel": "Stopping a coding job",
    "internet_search": "Searching the web",
    "wolfram_alpha": "Computing with Wolfram|Alpha",
    "video": "Analyzing a video",
    "generate_image": "Generating an image",
    "browser": "Using the browser",
    "render_chart": "Rendering a chart",
    "render_diagram": "Rendering a diagram",
    "run_code": "Running code",
    # Workspace / files
    "read_file": "Reading a file",
    "write_file": "Writing a file",
    "edit_file": "Editing a file",
    "multi_edit": "Editing a file",
    "move_file": "Moving a file",
    "delete_file": "Deleting a file",
    "list_workspace": "Listing your files",
    "grep_workspace": "Searching your files",
    "glob_workspace": "Finding files",
    "view_image": "Looking at an image",
    "import_attachment": "Importing an attachment",
    "extract_archive": "Extracting an archive",
    "extract_document_text": "Reading a document",
    "fetch_url": "Fetching a web page",
    "zip": "Zipping files",
    "queue_file": "Updating reply attachments",
    # Search / knowledge
    "discord_text_search": "Searching this server",
    # Memory
    "recall_user": "Recalling what I know",
    "reflect_user": "Reflecting on memory",
    "remember_user_memory": "Saving to memory",
    "recall_community": "Recalling community memory",
    "reflect_community": "Reflecting on community memory",
    "teach": "Saving to community memory",
    # Skills
    "load_skill": "Loading a skill",
    "skill_file": "Reading skill reference",
    "skill_create": "Creating a skill",
    "skill_edit": "Editing a skill",
    "skill_delete": "Deleting a skill",
    "skill_list": "Listing skills",
    "my_skill_get": "Reading a personal skill",
    "my_skill_create": "Saving a personal skill",
    "my_skill_edit": "Updating a personal skill",
    "my_skill_delete": "Deleting a personal skill",
    # Personalization
    "persona_set": "Updating your persona",
    "persona_show": "Checking your persona",
    "persona_clear": "Clearing your persona",
    # Discord context / reply composition
    "get_channel_context": "Reading recent messages",
    "lookup_member": "Looking up a member",
    "browse_tools": "Looking for the right tool",
    "plan": "Planning",
    "build_discord_embed": "Formatting a rich reply",
    "move_to_thread": "Moving to a thread",
    "leave_thread": "Leaving the thread",
    "pause_thread_replies": "Standing down in this thread",
    "resume_thread_replies": "Rejoining this thread",
    "block_user": "Blocking this user",
}


def register_tool_labels(labels: Mapping[str, str]) -> None:
    """Merge plugin-contributed tool labels into ``_TOOL_LABELS`` (first wins).

    Core entries are authoritative: an existing key keeps its label and a
    conflicting contribution is logged, never applied. Exposed to plugins via
    ``app/plugins.py:PluginContext`` so the dict above stays the single runtime
    source of truth for both activity surfaces.
    """
    for name, label in labels.items():
        existing = _TOOL_LABELS.get(name)
        if existing is None:
            _TOOL_LABELS[str(name)] = str(label)
        elif existing != label:
            log.warning(
                "Ignoring tool label for %r (%r): already labeled %r",
                name,
                label,
                existing,
            )


def tool_display_label(tool_name: str) -> str:
    """Friendly label for a tool, for both the activity subtext and live status."""
    label = _TOOL_LABELS.get(tool_name)
    if label:
        return label
    return tool_name.replace("_", " ").title()


def tool_activity_label(tool_name: str) -> str:
    # ASCII "..." matches the other live status strings ("Thinking...", etc.).
    return f"{tool_display_label(tool_name)}..."


async def emit_activity(
    reporter: ActivityReporter | None,
    label: str,
    *,
    phase: str = "status",
    tool: str = "",
) -> None:
    if reporter is None:
        return
    try:
        await reporter(ActivityUpdate(label=label, phase=phase, tool=tool))
    except Exception:
        log.debug("Activity reporter failed for %r", label, exc_info=True)


async def emit_narration_step(
    reporter: ActivityReporter | None,
    narration: str,
    tool_names: list[str],
) -> None:
    """Commit one tool-iteration narration step when the reporter supports it."""
    if reporter is None or not isinstance(reporter, SupportsNarrationSteps):
        return
    try:
        await reporter.commit_step(narration, list(tool_names))
    except Exception:
        log.debug("Narration-step reporter failed", exc_info=True)


async def emit_plan_update(
    reporter: ActivityReporter | None,
    steps: list[dict[str, str]],
) -> None:
    """Push the current plan checklist to the live surface when supported."""
    if reporter is None or not isinstance(reporter, SupportsPlanUpdates):
        return
    try:
        # Copy so reporter state never aliases a later ctx.plan rebind.
        await reporter.update_plan([dict(step) for step in steps])
    except Exception:
        log.debug("Plan-update reporter failed", exc_info=True)
