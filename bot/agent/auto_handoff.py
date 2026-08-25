"""Deterministic auto-handoff backstop (discord-free).

When the model does *not* call ``move_to_thread`` but produces a long reply in a
channel that opted in (``auto_thread_always`` for every reply, or the
``auto_thread_min_lines`` / ``auto_thread_min_chars`` length thresholds, in
``config/channels/<id>.md``), the Discord boundary synthesizes a ``ThreadRequest``
so the reply moves into a new thread instead of cluttering the channel. This
module holds the pure decision and thread-name derivation; the boundary glue
(reading ``message.channel`` / reacting to the parent message) stays in
``app/thread_handoff_boundary.py``. See ``docs/thread-handoff.md``.
"""

from __future__ import annotations

from tools.threads import THREAD_NAME_MAX, ThreadRequest


def reply_exceeds_threshold(
    response_text: str,
    *,
    min_lines: int | None,
    min_chars: int | None,
) -> bool:
    """True when the reply is long enough to warrant a handoff.

    Line count keys off newlines, so a single very long wrapped paragraph counts
    as one line. ``min_chars`` is the fallback that catches such walls of text.
    Either threshold tripping is enough.
    """
    text = response_text or ""
    return bool(
        (min_lines is not None and len(text.splitlines()) > min_lines)
        or (min_chars is not None and len(text) > min_chars)
    )


def derive_thread_name(question_text: str, *, fallback: str) -> str:
    """Build a thread name from the triggering question, with a fallback.

    Whitespace (including the stripped mention's gaps) is collapsed and the name
    is truncated to Discord's hard cap.
    """
    name = " ".join((question_text or "").split())
    if not name:
        name = " ".join((fallback or "").split()) or "Thread"
    return name[:THREAD_NAME_MAX]


def build_auto_handoff_request(
    *,
    response_text: str,
    question_text: str,
    bot_name: str,
    min_lines: int | None,
    min_chars: int | None,
    always: bool = False,
) -> ThreadRequest | None:
    """Synthesize a ``ThreadRequest`` when the reply trips a threshold, else None.

    ``always`` skips the length judgment entirely; every reply moves. Callers
    must have already confirmed the turn is eligible (auto-handoff enabled, not
    already in a thread, no model-issued request, not moderation blocked); this
    only judges length and names the thread.
    """
    if not always:
        if min_lines is None and min_chars is None:
            return None
        if not reply_exceeds_threshold(response_text, min_lines=min_lines, min_chars=min_chars):
            return None
    name = derive_thread_name(question_text, fallback=f"Chat with {bot_name}")
    return ThreadRequest(name=name)
