"""Thread tools: ``move_to_thread``, ``leave_thread`` and the pause/resume pair.

``move_to_thread`` does not create the thread itself. Like ``build_discord_embed``
it queues a validated, single-slot request on the ``MessageContext``; the thread is
created at the Discord boundary in ``app/runtime.py:handle_message``, from the
triggering user message, and the reply is delivered as the thread's first message.
A second call replaces the first; a rejected call leaves a prior request intact.
Its ``auto_reply`` argument is tri-state on the request: ``None`` means the model
said nothing and the boundary applies the operator's channel/guild default.

Its optional ``channel`` argument opens the thread in a **different** channel:
"take this to #bot-spam". The written reference is resolved here, in-loop, so a
miss is a tool error the model can correct on the spot; resolution itself is
injected (``ThreadTargetResolver``) because deciding which channels are usable
needs Discord state this module deliberately cannot see. Only the *matching* is
local: :func:`match_thread_target` is pure and takes the already-filtered
candidates. Ambiguity is always an error and never a pick, since a wrong match
posts in the wrong channel and no retry takes that back.

The other three act on the *current* thread only; they take no target argument
(derived context, mirroring ``block_user``), so the model can never touch a thread
it is not speaking in. ``leave_thread`` queues a close that the runtime boundary
performs after the final reply. ``pause_thread_replies`` / ``resume_thread_replies``
write through immediately instead of riding a rail: they change thread state rather
than the outgoing reply, and "stop replying" should stick even if the rest of the
turn fails.

All four are registered as core (not searchable) tools. ``move_to_thread`` is
therefore available on the first ask in any channel where handoff is allowed;
the three lifecycle tools are masked out of every turn with no thread to act on
(see ``config/fragments/tool_policy.py:thread_state_blocked_tools``). Being core
is what makes both handoff and "stop replying to everything" work without a
preliminary ``browse_tools`` call.

All four are registered only when ``THREAD_HANDOFF_ENABLED`` is true. Nothing
here imports ``discord``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from tools._common import tool_error
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier

if TYPE_CHECKING:
    from app.threads import ThreadHandoffManager

log = logging.getLogger(__name__)

# Discord's hard cap on thread names.
THREAD_NAME_MAX = 100

_CHANNEL_MENTION_RE = re.compile(r"^<#(\d+)>$")
# High enough that a near-miss is a question rather than a guess. A fuzzy match
# that lands wrong posts the bot in a channel nobody asked for.
_FUZZY_CUTOFF = 0.75
# Two candidates this close are treated as ambiguous rather than ranked.
_AMBIGUITY_MARGIN = 0.05
# Error strings go to the model, which pays for them; a long allowlist should
# not turn every miss into a wall of channel names.
_MAX_LISTED_CHANNELS = 20


@dataclass(frozen=True)
class ThreadTarget:
    """A channel the bot may open a thread in, already checked and allowed.

    Built by the injected resolver from the guild's ``thread_targets``
    allowlist; by the time one of these exists, the channel is a text channel
    (not a forum), both the asker and the bot can post in it, and its own
    ``thread_handoff`` is on.
    """

    channel_id: int
    name: str


# (turn context, the channel reference as written) -> the resolved target.
# Raises ValueError whose message becomes the tool error the model sees.
ThreadTargetResolver = Callable[[MessageContext, str], ThreadTarget]
# Discord-specific permission check injected by the runtime. The tool module
# stays platform-agnostic; creator and STAFF authorization are enforced here,
# while this seam supplies the channel-overwrite-aware Manage Threads decision.
ThreadLifecyclePermissionChecker = Callable[[MessageContext, int], bool]


@dataclass(frozen=True)
class ThreadRequest:
    """Validated, plain-data request to move this reply into a new thread."""

    name: str
    # None = the model did not say, so the boundary applies the operator default
    # for this channel/guild. See app/thread_handoff_boundary.py:_thread_auto_respond_default.
    auto_respond: bool | None = None
    # None = open the thread here, off the triggering message (the original
    # behavior). An id means open it in that channel instead, off an anchor
    # message posted there. Only ever set from a resolver-checked target, so the
    # boundary can trust it came from the allowlist. It re-checks anyway.
    target_channel_id: int | None = None


@dataclass(frozen=True)
class ThreadCloseRequest:
    """Plain-data request to close the current managed thread after this reply."""

    thread_id: int


def normalize_channel_name(raw: str) -> str:
    """Fold a written channel reference into Discord's own name form.

    Users and models write "#Bot Spam", "bot spam", or "bot-spam" for the same
    channel; Discord stores only the last. Spaces become hyphens because a text
    channel cannot contain one, so anyone typing a space meant the hyphen.
    """
    return "-".join(raw.strip().lstrip("#").casefold().split())


def _channel_list(candidates: Sequence[ThreadTarget]) -> str:
    shown = [f"#{target.name}" for target in candidates[:_MAX_LISTED_CHANNELS]]
    if len(candidates) > _MAX_LISTED_CHANNELS:
        shown.append(f"and {len(candidates) - _MAX_LISTED_CHANNELS} more")
    return ", ".join(shown)


def _with_choices(message: str, candidates: Sequence[ThreadTarget]) -> str:
    return f"{message} Channels I can use: {_channel_list(candidates)}."


def _only(matches: Sequence[ThreadTarget]) -> ThreadTarget:
    """The single match, or a refusal naming all of them.

    The one chokepoint for "more than one thing matched". Every match path funnels
    through it, so no branch can grow a silent tie-break: picking wrong here posts
    the bot in a channel nobody asked for, and no retry takes that back.
    """
    if len(matches) == 1:
        return matches[0]
    raise ValueError(
        f"That could mean more than one channel: {_channel_list(matches)}. Ask "
        "which one they meant, or give me the channel link."
    )


def match_thread_target(raw: str, candidates: Sequence[ThreadTarget]) -> ThreadTarget:
    """Resolve a written channel reference against the usable targets.

    Cheapest first: an id (bare or ``<#id>``), an exact name, a unique prefix,
    then fuzzy above ``_FUZZY_CUTOFF``. Raises ``ValueError`` with a
    model-facing message (listing what *is* available) on anything else, so
    the ReAct loop can correct itself in place.

    Ambiguity never resolves silently. Two prefix hits or two fuzzy scores
    within ``_AMBIGUITY_MARGIN`` raise, because the cost of guessing here is a
    message posted in the wrong channel.
    """
    if not candidates:
        raise ValueError(
            "There are no other channels I'm allowed to start threads in here, "
            "so this thread has to open in the current channel."
        )

    token = raw.strip()
    mention = _CHANNEL_MENTION_RE.match(token)
    wanted_id = mention.group(1) if mention else (token if token.isdigit() else "")
    if wanted_id:
        for candidate in candidates:
            if str(candidate.channel_id) == wanted_id:
                return candidate
        # A bare number can still be a channel *name* (#2024), so fall through to
        # name matching rather than insisting it was meant as an id.
        if mention is not None:
            raise ValueError(
                _with_choices("That channel isn't one I can start a thread in.", candidates)
            )

    needle = normalize_channel_name(token)
    if not needle:
        raise ValueError(_with_choices("Name the channel to start the thread in.", candidates))

    # Grouped, not mapped: Discord allows two channels to share a name (different
    # categories), and keying a dict by name would silently collapse them into a
    # last-one-wins pick, the exact outcome this function exists to refuse.
    by_name: dict[str, list[ThreadTarget]] = {}
    for target in candidates:
        by_name.setdefault(normalize_channel_name(target.name), []).append(target)

    exact = by_name.get(needle)
    if exact is not None:
        return _only(exact)

    prefixed = [t for name, group in by_name.items() if name.startswith(needle) for t in group]
    if prefixed:
        return _only(prefixed)

    scored = sorted(
        (
            (SequenceMatcher(None, needle, name).ratio(), target)
            for name, group in by_name.items()
            for target in group
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score = scored[0][0]
    if best_score < _FUZZY_CUTOFF:
        raise ValueError(_with_choices(f"I don't have a channel matching '{token}'.", candidates))
    return _only([target for score, target in scored if best_score - score <= _AMBIGUITY_MARGIN])


def build_thread_request_payload(
    args: Mapping[str, Any],
    ctx: MessageContext,
    resolve_target: ThreadTargetResolver | None = None,
) -> ThreadRequest:
    """Validate model args into a ``ThreadRequest``.

    Raises ``ValueError`` (message becomes the ``tool_error`` string) on any rule
    violation. Reads ``ctx`` but never mutates it.
    """
    target: ThreadTarget | None = None
    raw_channel = args.get("channel")
    if isinstance(raw_channel, str) and raw_channel.strip():
        if resolve_target is None:
            raise ValueError(
                "I can't start a thread in another channel from here; leave the "
                "channel out to open one where we are."
            )
        target = resolve_target(ctx, raw_channel.strip())
        if str(target.channel_id) == ctx.channel_id:
            # Naming the channel we are already in is the ordinary handoff, not
            # a move: no anchor, no pointer message, no second notification.
            target = None

    if target is None and ctx.thread_id is not None:
        raise ValueError(
            "This conversation is already in a thread; Discord does not allow "
            "threads inside threads. Name a different channel if you want a "
            "thread started over there instead."
        )

    raw = args.get("name")
    name = " ".join(str(raw or "").split())
    if not name:
        raise ValueError("Provide a short, descriptive thread name.")
    auto_reply = args.get("auto_reply")
    return ThreadRequest(
        name=name[:THREAD_NAME_MAX],
        # Only a real boolean counts; anything else leaves the operator default
        # in charge rather than guessing at what the model meant.
        auto_respond=auto_reply if isinstance(auto_reply, bool) else None,
        target_channel_id=target.channel_id if target is not None else None,
    )


def _current_managed_thread(
    ctx: MessageContext,
    manager: ThreadHandoffManager | None,
) -> tuple[ThreadHandoffManager, int]:
    """Resolve the managed thread this turn is speaking in.

    Raises ``ValueError`` whose message becomes the ``tool_error`` string. The
    thread is always derived from the turn, never a model argument, so these
    tools can only ever act where they were called.
    """
    if manager is None:
        raise ValueError("Thread handoff is not available right now.")
    if ctx.thread_id is None:
        raise ValueError("This conversation is not in a thread.")
    try:
        thread_id = int(ctx.thread_id)
    except ValueError:
        raise ValueError("This conversation is not in a thread.") from None
    if not manager.is_managed(thread_id):
        raise ValueError(
            "This thread is not one I manage; I already only respond here when mentioned."
        )
    return manager, thread_id


async def _authorized_managed_thread(
    ctx: MessageContext,
    manager: ThreadHandoffManager | None,
    can_manage_thread: ThreadLifecyclePermissionChecker | None,
) -> tuple[ThreadHandoffManager, int]:
    """Resolve the current thread and enforce its lifecycle authorization.

    The model cannot grant this permission. The initiator is read from durable
    thread metadata, STAFF comes from the server-side trust resolver, and the
    optional callback checks Discord's effective Manage Threads permission.
    Missing creator metadata and lookup failures both fail closed.
    """
    manager, thread_id = _current_managed_thread(ctx, manager)
    if ctx.trust_tier >= TrustTier.STAFF:
        return manager, thread_id
    try:
        if await manager.is_creator(thread_id, ctx.user_id):
            return manager, thread_id
    except Exception:
        log.exception("Could not verify creator for managed thread %s", thread_id)
    try:
        if can_manage_thread is not None and can_manage_thread(ctx, thread_id):
            return manager, thread_id
    except Exception:
        log.exception("Could not check Manage Threads permission for %s", thread_id)
    raise ValueError(
        "Only the person who started this managed thread, staff, or someone with "
        "Discord's Manage Threads permission can close it or change its reply mode."
    )


def init_thread_tools(
    registry: ToolRegistry,
    get_manager: Callable[[], ThreadHandoffManager | None],
    *,
    bot_name: str,
    resolve_target: ThreadTargetResolver | None = None,
    can_manage_thread: ThreadLifecyclePermissionChecker | None = None,
) -> None:
    # The phrase that reaches a paused thread without an @mention. Users cannot
    # guess it, so the pause tool hands it to the model to pass along.
    wake_phrase = f"hey {bot_name.strip() or 'bot'}"

    async def move_handler(args: dict, ctx: MessageContext) -> str:
        try:
            request = build_thread_request_payload(args, ctx, resolve_target)
        except ValueError as exc:
            return tool_error(str(exc))
        ctx.thread_request = request
        if request.target_channel_id is not None:
            note = (
                f"Your reply will be posted in <#{request.target_channel_id}> as "
                "the first message of the new thread, and they'll be added to it "
                "and pointed there from here. Answer there in full. Don't also "
                "answer in this channel."
            )
        else:
            note = (
                "Your reply will be posted as the first message of the new "
                "thread; the conversation continues there."
            )
        return json.dumps(
            {
                "queued": True,
                "name": request.name,
                "channel_id": (
                    str(request.target_channel_id)
                    if request.target_channel_id is not None
                    else None
                ),
                "note": note,
            }
        )

    async def leave_handler(args: dict, ctx: MessageContext) -> str:
        try:
            _manager, thread_id = await _authorized_managed_thread(
                ctx, get_manager(), can_manage_thread
            )
        except ValueError as exc:
            return tool_error(str(exc))
        ctx.thread_close_request = ThreadCloseRequest(thread_id=thread_id)
        return json.dumps(
            {
                "queued": True,
                "thread_id": thread_id,
                "note": (
                    "After your reply is sent, this managed thread will be locked "
                    "and archived. Say goodbye briefly."
                ),
            }
        )

    async def pause_handler(args: dict, ctx: MessageContext) -> str:
        try:
            manager, thread_id = await _authorized_managed_thread(
                ctx, get_manager(), can_manage_thread
            )
        except ValueError as exc:
            return tool_error(str(exc))
        if not await manager.pause(thread_id):
            return tool_error("This thread is not one I manage.")
        return json.dumps(
            {
                "paused": True,
                "thread_id": thread_id,
                "note": (
                    "Done. I won't reply to messages here unless someone "
                    "mentions me, replies to me with the ping left on, or starts "
                    f"a message with '{wake_phrase}'. The thread stays open and I "
                    "keep the history. Call resume_thread_replies if someone "
                    "later asks me to answer everything again. You can let them "
                    "know how to reach me in the meantime."
                ),
            }
        )

    async def resume_handler(args: dict, ctx: MessageContext) -> str:
        try:
            manager, thread_id = await _authorized_managed_thread(
                ctx, get_manager(), can_manage_thread
            )
        except ValueError as exc:
            return tool_error(str(exc))
        if not await manager.resume(thread_id):
            return tool_error("This thread is not one I manage.")
        return json.dumps(
            {
                "resumed": True,
                "thread_id": thread_id,
                "note": ("I'm answering every message in this thread again. Confirm that briefly."),
            }
        )

    registry.register(
        name="move_to_thread",
        description=(
            "Start a new Discord thread from the current user message, including "
            "for a brand-new conversation. Your current reply becomes the "
            "thread's first message and you keep responding there without needing "
            "mentions. Use it when someone asks for a thread, or proactively when "
            "detailed step-by-step troubleshooting or another multi-turn discussion "
            "would clutter a busy channel. Can also open the thread in another "
            "channel when someone asks for that. See the 'start-thread' skill for "
            "the full behavior."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Short, descriptive thread title (<=100 chars), e.g. "
                        "'Quest 3 link cable troubleshooting'."
                    ),
                },
                "channel": {
                    "type": "string",
                    "description": (
                        "Optional: another channel to open the thread in, as "
                        "written, e.g. '#bot-spam'. Only pass it when someone "
                        "actually names a channel; omit it and the thread opens "
                        "right here. Not every server allows this, and both you "
                        "and they need to be able to post there; you'll be told "
                        "which channels are available if this one isn't."
                    ),
                },
                "auto_reply": {
                    "type": "boolean",
                    "description": (
                        "Whether to answer every message in the new thread "
                        "without needing a mention. Omit unless the user asks "
                        "for one or the other; false starts the thread quiet, "
                        "so you only answer there when mentioned."
                    ),
                },
            },
            "required": ["name"],
        },
        handler=move_handler,
        min_tier=TrustTier.MEMBER,
        category="Discord",
    )
    registry.register(
        name="leave_thread",
        description=(
            "Close the current managed thread by sending your final reply, then "
            "locking and archiving the thread. Use when asked to close, lock, "
            "archive, or end the thread, or when its purpose is clearly resolved. "
            "Only the person who started it, staff, or someone with Discord's "
            "Manage Threads permission may do this. "
            "This cannot be undone, so if someone only wants you to stop answering "
            "every message, use pause_thread_replies instead."
        ),
        parameters={"type": "object", "properties": {}},
        handler=leave_handler,
        min_tier=TrustTier.MEMBER,
        category="Discord",
    )
    registry.register(
        name="pause_thread_replies",
        description=(
            "Stop answering every message in this thread. Afterwards you only "
            "reply here when mentioned, replied to, or greeted by name; the "
            "thread stays open and keeps its history. Only the person who started "
            "it, staff, or someone with Discord's Manage Threads permission may "
            "change this mode. Use when an authorized person asks you "
            "to stop responding to everything, to only answer when addressed, or "
            "to be quiet for a while."
        ),
        parameters={"type": "object", "properties": {}},
        handler=pause_handler,
        min_tier=TrustTier.MEMBER,
        category="Discord",
    )
    registry.register(
        name="resume_thread_replies",
        description=(
            "Answer every message in this paused thread again, without needing a "
            "mention. Only the person who started it, staff, or someone with "
            "Discord's Manage Threads permission may change this mode. Use when "
            "an authorized person asks you to start responding normally here."
        ),
        parameters={"type": "object", "properties": {}},
        handler=resume_handler,
        min_tier=TrustTier.MEMBER,
        category="Discord",
    )
