---
name: start-thread
description: Start or manage a bot-owned Discord thread with the available handoff, pause, resume, and close controls.
tags: [discord, thread, handoff, conversation]
---

# Managed Discord threads

Use this when someone asks for a thread or a discussion clearly benefits from a
separate multi-turn space. Thread handoff is optional. `move_to_thread` is
searchable, so use `browse_tools` when needed; if it is unavailable, say that
thread creation is not enabled here.

`move_to_thread` starts a public thread from the current user message. The
current reply becomes the first message in that thread, so answer normally and
do not send a separate duplicate announcement. Give it a short, specific name
of at most 100 characters. A thread cannot be opened inside the current thread,
though cross-channel handoff may open one in a named allowlisted channel.

Omit `auto_reply` unless the request establishes a preference. `false` starts a
quiet managed thread where the bot answers only when invoked; otherwise the
configured channel default decides whether every message receives a reply.

Pass `channel` only when the user actually names another channel. Cross-channel
handoff requires an operator allowlist and posting permissions for both the bot
and user. If the requested target is refused, report the available choices or
ask which one they want; never silently choose a different channel.

Inside a managed thread, contextual tools appear only when they can act:

- `pause_thread_replies` keeps the thread open but returns it to mention, reply,
  or name invocation. Use this for "be quiet" or "only reply when asked."
- `resume_thread_replies` restores automatic replies in a paused thread.
- `leave_thread` sends the current final reply, then locks and archives the
  thread. This cannot be undone; use it only for an explicit close/archive/end
  request or a clearly completed thread, not merely a request for quiet.

Changing thread mode or closing requires the initiating user, Staff trust, or
Discord Manage Threads permission. Trust the available contextual tool surface
and the tool result rather than assuming authorization.
