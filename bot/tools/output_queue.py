from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from workspace import WorkspaceKey, WorkspaceManager
from tools.registry import MessageContext


class AttachmentLimitError(ValueError):
    pass


MAX_ATTACHMENT_DESCRIPTION_CHARS = 1024


@dataclass(frozen=True)
class QueuedOutput:
    path: Path
    root: Path
    added: bool
    remove_id: str


def enqueue_workspace_file(
    ctx: MessageContext,
    workspace_manager: WorkspaceManager,
    path: Path,
    *,
    max_attachments: int | None = None,
    description: str | None = None,
) -> QueuedOutput:
    resolved = path.resolve(strict=False)
    workspace_manager.relative_user_file_path(ctx.workspace_key, resolved)
    return enqueue_output_file(
        ctx,
        resolved,
        workspace_manager.user_files_dir(ctx.workspace_key).resolve(),
        max_attachments=max_attachments,
        description=description,
    )


def enqueue_context_generated_file(
    ctx: MessageContext,
    workspace_manager: WorkspaceManager,
    generated_path: str,
    *,
    max_attachments: int | None = None,
) -> QueuedOutput:
    if not ctx.context_key:
        raise ValueError("Generated files can only be queued from a conversation context")
    resolved = workspace_manager.resolve_context_generated_file(
        generated_path,
        context_key=ctx.context_key,
        must_exist=True,
    )
    return enqueue_output_file(
        ctx,
        resolved.path,
        resolved.root,
        max_attachments=max_attachments,
    )


def enqueue_output_file(
    ctx: MessageContext,
    path: Path,
    root: Path,
    *,
    max_attachments: int | None = None,
    description: str | None = None,
) -> QueuedOutput:
    resolved = path.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    if description is not None:
        description = description.strip()
        if len(description) > MAX_ATTACHMENT_DESCRIPTION_CHARS:
            raise ValueError(
                f"attachment description exceeds {MAX_ATTACHMENT_DESCRIPTION_CHARS} characters"
            )
        if not description:
            description = None
    if not resolved.is_relative_to(root_resolved):
        raise ValueError("Queued file is outside allowed output root")

    root_text = str(root_resolved)
    resolved_text = str(resolved)
    if resolved_text in ctx.output_files:
        _add_allowed_root(ctx, root_text)
        if description:
            ctx.output_file_descriptions[resolved_text] = description
        return QueuedOutput(
            path=resolved,
            root=root_resolved,
            added=False,
            remove_id=output_file_remove_id(ctx, resolved_text),
        )

    # A pending embed image rides Discord on its filename (attachment://<name>).
    # Reject a *different* file that would collide with that basename, so the
    # embed image cannot resolve to the wrong attachment. The same-file case is
    # already handled by the idempotency return above.
    embed_attachment = ctx.embed_attachment
    if embed_attachment is not None:
        embed_path = str(Path(embed_attachment.path).resolve(strict=False))
        if resolved.name == embed_attachment.filename and resolved_text != embed_path:
            raise ValueError(
                f"A file named '{resolved.name}' is already attached to this reply's "
                "embed; rename this file so its filename is unique."
            )

    if max_attachments is not None and len(ctx.output_files) >= max_attachments:
        raise AttachmentLimitError(f"attachment limit reached ({max_attachments})")

    ctx.output_files.append(resolved_text)
    if description:
        ctx.output_file_descriptions[resolved_text] = description
    _add_allowed_root(ctx, root_text)
    return QueuedOutput(
        path=resolved,
        root=root_resolved,
        added=True,
        remove_id=output_file_remove_id(ctx, resolved_text),
    )


def output_file_remove_id(ctx: MessageContext, output: str) -> str:
    """Return the stable, opaque per-turn removal selector for one queued path."""

    for remove_id, queued in ctx.output_file_remove_ids.items():
        if queued == output:
            return remove_id
    ctx.output_file_remove_id_counter += 1
    remove_id = f"attachment:{ctx.output_file_remove_id_counter}"
    ctx.output_file_remove_ids[remove_id] = output
    return remove_id


def match_output_file_remove_id(ctx: MessageContext, remove_id: str) -> str | None:
    """Resolve an exposed removal selector without revealing its backing path."""

    output = ctx.output_file_remove_ids.get(remove_id)
    if output is None:
        return None
    if output in ctx.output_files:
        return output
    # Tolerate direct mutations of output_files without letting a stale
    # selector target a path that might later be re-queued.
    del ctx.output_file_remove_ids[remove_id]
    return None


def unqueue_output_file(ctx: MessageContext, output: str) -> None:
    """Remove one reply attachment and retire any selector that pointed to it."""

    ctx.output_files.remove(output)
    ctx.output_file_descriptions.pop(output, None)
    for remove_id, queued in list(ctx.output_file_remove_ids.items()):
        if queued == output:
            del ctx.output_file_remove_ids[remove_id]


def requeue_moved_output(ctx: MessageContext, old_path: Path, new_path: Path) -> int:
    """Rewrite queued entries after a rename so the rail never dangles.

    Covers the exact file and everything under a moved directory. A dangling
    queued path fails the entire reply's file staging at the Discord boundary,
    so every mutation that relocates workspace files must call this.
    """
    old_text = str(old_path)
    new_text = str(new_path)
    old_prefix = old_text + os.sep
    changed = 0
    for index, entry in enumerate(ctx.output_files):
        if entry == old_text:
            updated = new_text
        elif entry.startswith(old_prefix):
            updated = new_text + entry[len(old_text) :]
        else:
            continue
        ctx.output_files[index] = updated
        description = ctx.output_file_descriptions.pop(entry, None)
        if description is not None:
            ctx.output_file_descriptions[updated] = description
        changed += 1
        for remove_id, queued in ctx.output_file_remove_ids.items():
            if queued == entry:
                ctx.output_file_remove_ids[remove_id] = updated
    return changed


def unqueue_removed_outputs(ctx: MessageContext, removed_path: Path) -> int:
    """Drop queued entries for a deleted file (or anything under a deleted dir)."""
    removed_text = str(removed_path)
    prefix = removed_text + os.sep
    stale = [e for e in ctx.output_files if e == removed_text or e.startswith(prefix)]
    for entry in stale:
        unqueue_output_file(ctx, entry)
    return len(stale)


def match_already_queued(ctx: MessageContext, path_arg: str) -> str | None:
    """Return the queued entry that ``path_arg`` refers to, if any.

    Absolute inputs must match an attached entry exactly. Relative inputs are
    honored only when they are bare filenames, which keeps structured workspace
    paths flowing through normal validation.
    """

    requested = Path(path_arg)
    if requested.is_absolute():
        abs_text = str(requested)
        return next((q for q in ctx.output_files if q == abs_text), None)
    if requested.name == path_arg:
        return next(
            (q for q in ctx.output_files if Path(q).name == requested.name),
            None,
        )
    return None


def queued_file_paths(
    ctx: MessageContext,
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
) -> list[str]:
    queued: list[str] = []
    for output in ctx.output_files:
        path = Path(output)
        try:
            queued.append(workspace_manager.relative_user_file_path(workspace_key, path))
            continue
        except ValueError:
            pass
        try:
            queued.append(workspace_manager.relative_generated_file_path(path))
            continue
        except ValueError:
            pass
        queued.append(path.name)
    return queued


def _add_allowed_root(ctx: MessageContext, root: str) -> None:
    if root not in ctx.allowed_file_roots:
        ctx.allowed_file_roots.append(root)
