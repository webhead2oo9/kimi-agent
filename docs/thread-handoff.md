# Thread handoff

Thread handoff lets the model move a conversation out of a busy channel into a Discord
thread it creates, and then keep talking there without anyone needing to @mention it.
The model calls a `move_to_thread` tool mid-turn; the reply is delivered as the first
message of a new thread spawned from the triggering user message, and from then on
every human message in that thread continues the same rooted conversation. The parent
channel keeps its mention-only contract untouched. "The convo moved" is meant
literally: the thread becomes the conversation surface, and the channel stays quiet.

A managed thread has a **mode**: auto-responding (the default, where every message
gets an answer) or paused, where it falls back to the ordinary channel contract. See
[Thread mode](#thread-mode-pause-and-resume).


## Where it lives

- `tools/threads.py`: the four thread tools, the plain-data `ThreadRequest`, and the
  pure `match_thread_target` (no `discord` import), registered in `app/tools.py` behind
  the config gate. The instruction-only `start-thread` skill teaches the model when to
  use them.
- `app/threads.py`: `ThreadHandoffManager`, the durable thread→root mapping plus the
  two in-memory id sets it owns (every managed thread, and the subset currently
  auto-responding); created and loaded in `on_ready`.
- `app/thread_handoff_boundary.py`: `ThreadHandoffBoundary`, which owns `_create_handoff_thread`
  (creation plus enrollment at the send boundary) and its cross-channel branch
  `_open_cross_channel_thread`, `_thread_target_candidates` / `resolve_thread_target`
  (the cross-channel gate and the seam the tool is given), `_thread_state_blocked_tools`
  (the per-turn tool mask), and the `_can_open_thread` permission check.
- `app/runtime.py`: calls the boundary through `self.threads.*`, owns
  `responds_without_mention` (the gate's predicate), and persists replies under the
  channel id they were actually sent to.
- `config/fragments/guild_config.py:load_guild_thread_targets`: the guild's cross-channel
  allowlist.
- `app/conversation_routing.py`: the managed-thread branch in
  `resolve_conversation_for_message`.
- `discord_adapter/io.py:should_respond`: the no-mention gate for auto-responding threads.
- `config/fragments/tool_policy.py`: `THREAD_STATE_TOOLS` and
  `thread_state_blocked_tools`, the pure per-turn visibility rule.
- `storage/db.py` / `storage/conversations.py`: the `thread_conversations` table
  and its CRUD.

The pending request rides the same rails as embeds: `MessageContext.thread_request` →
`ConversationContext.pending_thread_request` (synced in `agent/core.py`) →
`TurnResult.thread_request` (`agent/turn.py`) → thread creation in `handle_message`.

## Config

`THREAD_HANDOFF_ENABLED` (default `true`, no external dependency) gates both tool
registration **and** the `should_respond` thread branch, so flipping it off mid-flight
reverts every managed thread to mention-only without touching any data. The bot needs
the **Create Public Threads**, **Send Messages in Threads**, and **Manage Threads**
permissions; private threads, forum channels, and announcement channels are out of
scope.

### In-turn suggestion

`THREAD_HANDOFF_SUGGEST_AFTER_TOOL_CALLS` defaults to `5`. During an ordinary
guild-channel turn, after that many completed **substantive** tool actions, the
ReAct loop adds one optional model-facing note: call `move_to_thread` if meaningful
work remains, or finish inline when the answer is already imminent. `plan`,
`browse_tools`, and the thread-control tools do not count. `0` disables the note.

The suggestion is never an automatic move. It is emitted at most once, only when
`move_to_thread` is visible for that turn, and never in a DM or an existing thread.
The runtime appends it as a separate content part on the latest tool result, then
removes it before retaining conversation history. This preserves the append-only
ReAct transcript and provider prompt-cache prefix: the initial prompt, earlier
messages, and tool schemas are not rewritten when the threshold trips.

`THREAD_AUTO_HANDOFF_ENABLED` (default `false`, requires `THREAD_HANDOFF_ENABLED`)
turns on the **deterministic backstop** described below. It is opt-in *per channel*: a
channel enrolls by declaring a threshold in its fragment frontmatter
(`config/channels/<id>.md`), which is read fresh each turn just like `pinned_tools`:

```yaml
---
auto_thread_min_lines: 4   # fire when the reply has > 4 lines
auto_thread_min_chars: 600 # ...or > 600 chars (catches a single wrapped wall of text)
---
```

Tripping either threshold is enough, an absent threshold is simply not checked, and a
channel with neither key never auto-hands off even with the global flag on
(`config/fragments/channel_pins.py:load_channel_auto_thread`). `auto_thread_always: true`
enrolls the channel with no length check at all, so every reply moves to a thread (the
value must be a YAML boolean; anything else is ignored, fail-closed). A successful
handoff reacts to the parent message with 🧵 on both the automatic and the
model-requested paths.

### Per-channel / per-guild switch

The `thread_handoff:` frontmatter key turns handoff on or off below the boot gate, at
two scopes: guild-wide in `config/servers/<guild_id>.md` and per channel in
`config/channels/<channel_id>.md`. Resolution is most-specific-wins
(`config/fragments/channel_pins.py:resolve_tristate`): the channel value if it is set,
otherwise the guild value, otherwise on. Only literal YAML booleans count; anything else
is treated as "not set here", so a typo falls back to the wider scope instead of
flipping the channel (`parse_tristate`).

When it resolves to **off**, two things happen, both read fresh each turn:
`move_to_thread` joins the turn's blocked-tools mask in `app/turn_entry.py` (hidden from
the model and refused at dispatch, exactly like a `blocked_tools` entry), and the
auto-handoff backstop skips the channel in `app/runtime.py` even if its fragment still
carries `auto_thread_*` thresholds. `leave_thread` stays available so an existing
managed thread can still be wound down after handoff is turned off. The switch only
ever *adds* to the blocked set: a channel-level `thread_handoff: true` overrides a
guild-wide `false` (same key, more specific scope) but never un-blocks an explicit
`blocked_tools` entry at deployment, guild, or channel scope.

This is how you express "handoff only in the support channels": set
`thread_handoff: false` on the guild fragment and `thread_handoff: true` on the
channels that want it. Edit both scopes directly in their fragment files.

## Thread mode: pause and resume

A managed thread answers every message. That is right while the thread *is* the
conversation and wrong the moment two people start talking to each other in it, so a
thread carries a mode:

| Mode | Behavior |
|------|----------|
| auto-responding (default) | every human message runs a turn, no mention needed |
| paused | exactly like an ordinary channel: @mention, reply-ping, or `hey <bot> …` |

Paused is a **mode, not a judgment call**. The gate is `should_respond`, which runs
before the response lock, the transcript write, and any provider call, so on a message
it is not meant to answer the bot never sees the message at all. No tokens are spent
and there is nothing to get wrong. It also means a paused thread is *not transcribed*:
when the bot is next mentioned there it has no record of the gap and catches up with
`get_channel_context` if it needs to. (Writing those messages anyway would persist text
from users who never triggered a turn, which is exactly what the privacy consent gate
exists to prevent.)

For that reason the gate is re-checked once the root lock is held
(`_on_message_for_user`). A message posted while the *pausing* turn was still running
passed the pre-lock check against the old mode and then queued on the same root;
without the second check it would be answered, and transcribed, after the bot had
already said it was standing down.

**Managed and auto-responding are separate facts.** Routing keys on *managed*
(`ThreadHandoffManager.is_managed`), the gate keys on *auto-responding*
(`is_auto_responding`), and the second is always a subset of the first. That is what
lets a paused thread keep its transcript: a later @mention resolves the same mapped
root instead of opening a fresh one. The manager owns both sets privately and answers
questions about them; nothing else holds a reference, so the subset invariant cannot be
broken from outside.

There are three ways the mode gets set:

1. **At creation.** `move_to_thread(name, auto_reply=false)` opens the thread quiet.
   The argument is tri-state on `ThreadRequest`: absent means the model expressed no
   preference, and the boundary applies the operator default described below.
2. **By an authorized participant.** `pause_thread_replies` /
   `resume_thread_replies` take no arguments and act only on the thread the turn is
   speaking in (derived context, like `block_user`'s target). The user who requested
   the handoff, configured STAFF, and members with Discord's effective **Manage
   Threads** permission may change the thread-wide mode. The initiator is stored on
   the `thread_conversations` row; a row with no recoverable initiator fails closed to
   STAFF/Manage Threads. Unlike the other rails, these write through immediately
   instead of riding `TurnResult`: they change thread state rather than the outgoing
   reply, and "stop replying" should stick even if the rest of the turn fails.
3. **By the operator.** The tri-state `thread_auto_respond:` frontmatter key, with the
   guild fragment as base and the channel fragment overriding, is read fresh each
   turn. It is the default for new threads only and never changes an existing thread;
   a model-supplied `auto_reply` wins. Edit both scopes directly in their fragment
   files.

The pause tool's result tells the model the resume tool's name **and** the
`hey <bot> …` wake phrase, with a nudge to pass the latter along: a user cannot guess
it, and "you can reply now" typed into a paused thread never reaches the bot.

### Tool visibility

All four thread tools are **core** tools (not searchable). The turn masks
`leave_thread`, `pause_thread_replies` and `resume_thread_replies` when they
have nothing to act on
(`config/fragments/tool_policy.py:thread_state_blocked_tools`, applied through the
`extra_blocked_tools` argument to `build_turn_dependencies`):

| Where | Offered |
|-------|---------|
| not a managed thread | none of the three |
| managed, auto-responding | `leave_thread`, `pause_thread_replies` |
| managed, paused | `leave_thread`, `resume_thread_replies` |

They are core because both starting a useful thread and "stop replying to
everything" should work on the first ask, without a `browse_tools` round trip.
`move_to_thread` is visible in ordinary guild channels where policy permits
handoff and is masked in DMs; the lifecycle mask, not the search pool, keeps the
other three out of turns where they cannot act.

### Instructions inside the thread

Everything above resolves against the **parent channel** when a turn runs inside a
thread, and the prompt's `<channel_instructions>` slot follows the same rule.

Threads inherit their parent channel's instructions, and two optional scopes can
replace them (`config/fragments/prompt.py:instruction_fragment_candidates`):

1. `config/threads/<thread_id>.md`: this one thread
2. `config/channel_threads/<parent_channel_id>.md`: every thread under that channel
3. `config/channels/<channel_id>.md`: the channel itself (the inherited default)

The first non-empty body wins, and it replaces rather than appends; clearing a body
falls back to the next scope. Both thread scopes are body only: `thread_handoff`, the
`auto_thread_*` keys, and the tool lists still live on the channel fragment, and those
keys are ignored at thread scope. See [`docs/configuration.md`](configuration.md) for
the full table.

Full prompt templates follow the same parent-inheritance principle through a separate
chain: `prompts/channels/<thread_id>.md` replaces `prompts/channels/<parent_channel_id>.md`
when present; otherwise the parent channel's full template follows the conversation
into the thread. The server and default templates remain the later fallbacks.

## Cross-channel threads

`move_to_thread(name, channel="#bot-spam")` opens the thread in a **different** channel
and points the asker at it, which covers the "take this to #support" case. It is off
unless the guild opts in, and the asker is added to the new thread instead of being
left to find it.

Five independent filters live in `app/thread_handoff_boundary.py:_thread_target_candidates`,
and each is enough on its own to refuse:

1. **The operator allowlist.** `thread_targets:` on the guild fragment
   (`config/servers/<guild_id>.md`), numeric channel ids, read fresh each turn. Absent
   or empty means the capability is off in that guild. This is the one thread
   affordance that puts the bot's voice in a channel nobody in the conversation is
   looking at, so it is opt-in per community, never deployment-wide. It is guild scope
   only; there is no channel-scope override.
2. **The deployment channel allowlist.** `ALLOWED_CHANNEL_IDS`, when set, still binds:
   a guild fragment must not reach past the boundary that says where this bot operates
   at all. Without this the bot would post into an excluded channel *and* then never
   answer there again, since `is_eligible_to_respond` drops every follow-up, and the
   asker would be pinged into a thread that is dead on arrival.
3. **No forums, no announcement channels.** A forum post *is* a thread, so there is no
   message to anchor one to; a news channel is a `discord.TextChannel` in discord.py,
   so `isinstance` alone would let it through and `channel.is_news()` is checked
   explicitly. Forums are independently refused by the runtime filter.
4. **Permissions: the asker *and* the bot must both be able to post there.**
   `view_channel`, `send_messages`, `create_public_threads` and
   `send_messages_in_threads` are checked against both members
   (`app/thread_handoff_boundary.py:_can_open_thread`). The rule this encodes is *the
   bot does nothing in another channel the asker could not do themselves*, which is
   why cross-channel targeting needs no rate limit or trust tier of its own; it grants
   no reach.
5. **The target channel's own handoff policy**, through the same
   `thread_handoff_creation_allowed` used for the source channel. Both ways of saying
   "no bot threads here" count: `thread_handoff: false` and a `move_to_thread` entry
   in `blocked_tools` at any scope. Being listed as a target is not consent; the
   denylist remains authoritative.

Resolution runs **in the tool**, so a miss is a tool error the ReAct loop corrects in
the same turn, not a failure the user sees. The tool is handed a resolver
(`resolve_thread_target`) at registration because deciding which channels are usable
needs Discord state that `tools/threads.py` cannot see; only the matching is local and
pure (`match_thread_target`). It reads the `channel` **argument** and nothing else:
`<#id>`, a bare id, an exact name, a unique prefix, then fuzzy above a 0.75 cutoff. (A
bare number that matches no id falls through to name matching, since `#2024` is a legal
channel name.) **Ambiguity always raises**, never picks: a wrong match posts in a
channel nobody asked for, and no retry takes that back. Every match path funnels
through one `_only` chokepoint so no branch can grow a silent tie-break, and candidates
are grouped by name instead of keyed into a dict, because Discord allows two channels
to share a name and a by-name map would collapse them into a last-one-wins pick. Every
refusal lists the channels that *are* available. There is no enum in the schema, so
context cost stays flat as the allowlist grows, at the price of the `channel` argument
being advertised in guilds that have no targets, where the model gets told so.

Naming the channel the turn is already in **collapses** to the ordinary handoff (no
anchor, no pointer, no second notification). Naming a *different* channel is legal from
inside a thread (nesting is the Discord limit, not "no threads from threads"), so "take
this to #dev" works from a support thread. Both threads then share one root and one
lock, which is continuity, not a bug.

At the boundary:

1. An **anchor** message is posted in the target channel:
   `Hey {display name}, brought your question over here! 🧵`. The display name is the
   plaintext server display name, run through `sanitize_author_name` and sent with
   `AllowedMentions.none()`, since nicknames are user-controlled and a literal
   `@everyone` in one would otherwise ping. Nobody in the target channel is notified.
2. The thread is created off that anchor with a 24-hour auto-archive
   (`CROSS_CHANNEL_ARCHIVE_MINUTES`), so an abandoned thread nobody was watching tidies
   itself away; nothing is ever deleted.
3. `thread.add_user(asker)` puts it in their sidebar. A failure here logs and continues.
4. The reply is delivered into the thread as usual, and the source channel gets the
   constant `Moved this over to {thread}, see you there! 🧵` **as a reply to the asker's
   message with the ping on**. That reply is the actual notification: the anchor pings
   nobody and `add_user` is not a reliable one. One ping, one person, in the channel
   they were already reading. It is sent after the answer and is **not persisted**,
   because the transcript maps under the channel the reply landed in, and a stub filed
   against the source channel would seed later turns from the wrong place.

The gate is re-run at the boundary instead of trusting the id on the request: the tool
resolved it a model turn ago, and this is the code that actually posts. Anything left
over gets cleaned up. The anchor is deleted if `create_thread` fails, and if the reply
never lands (the existing `prune` path) the anchor goes too, which takes the empty
thread with it, since a thread shares the id of the message it was created from. That
cleanup keys on `expected_delivery` (text **or** an embed **or** output files), not on
the reply text alone: an embed-only reply that failed to send is just as much a
failure, and keying on the text alone would orphan both the thread and a message in a
third channel.

A reply blocked by moderation creates no thread on either path. Same-channel, a stray
thread is merely untidy; cross-channel, it would post an anchor in a channel nobody in
the conversation is looking at.

Edit `thread_targets` directly in the guild's server fragment. Stale channel ids
remain until the operator removes them.

## Flow

1. **Model opt-in.** During a normal mention-gated turn in a regular channel, the model
   calls the core `move_to_thread(name)` tool (`MEMBER` tier). The tool validates and
   queues a single-slot pending request:
   `ConversationContext.pending_thread_request` → `TurnResult.thread_request`. Validation
   is side-effect free; a second call replaces the first. The tool rejects immediately
   when the turn is already inside a thread (known from the turn's `thread_id`, derived and
   never a model argument) *unless* a different `channel` was named, or when the name is
   empty; names are sanitized and truncated
   to Discord's 100-char cap. The terse tool description points at an instruction-only
   `start-thread` skill for guidance on *when* starting a thread is appropriate (long
   troubleshooting, multi-message back-and-forth), which keeps the registry
   description short.
2. **Thread creation at the send boundary.** After the turn, `handle_message` sees
   `turn_result.thread_request`, calls `message.create_thread(name=...)` on the
   triggering user message (or takes the
   [cross-channel branch](#cross-channel-threads) when the request carries a target),
   and passes the new thread to `send_response` as the target channel (with no
   `reference`: you can't reply-reference across channels, and being the first thread
   message is anchor enough). Discord itself posts the "X started a thread" notice in
   the parent channel, so no extra stub message is sent there. Creation is serialized
   per starter message: if foreground and background coding delivery race, the second
   path adopts and persists the already-managed thread. On `Forbidden` (missing
   permission) the bot falls back to replying in-channel immediately; on a transient
   `HTTPException` (say, the message already has a thread, since Discord allows one per
   message, or a flaky API call) it retries creation once after a short pause
   (`THREAD_HANDOFF_CREATE_RETRY_DELAY_SECONDS`, a hardcoded module constant in
   `app/thread_handoff_boundary.py`, not a setting) before falling back. Either way it
   logs, and the turn never fails because of the thread. **Persistence must follow the
   send target:** the assistant reply chunks are saved and mapped with the channel id
   of the message actually sent (the thread id), not the pre-send `context_channel_id`
   captured from the triggering message. `get_continuation_conversation_for_reply` and
   `get_conversation_by_discord_message` filter `message_contexts` by channel id, so a
   reply mapped under the parent channel would silently miss for in-thread lookups.
   That would be masked while the thread is managed (the `thread_conversations` branch
   routes anyway) but would break reply continuation after `leave_thread` and pollute
   the `lookup_memory_source` window.
3. **Enrollment.** On successful creation, the thread id, the resolved root
   conversation id and the resolved mode are written to the `thread_conversations`
   table and added to the manager's sets. The mode is `request.auto_respond` when the
   model asked for one, otherwise the channel/guild `thread_auto_respond` default. Only
   bot-*created* threads enroll; responding to a stray @mention inside someone else's
   thread stays a one-shot mention-gated reply. Enrolling on mention instead would
   silently auto-enroll any thread the bot is ever mentioned in.
4. **Participation gate.** `should_respond` takes a `responds_without_mention`
   predicate, answered in production by `ThreadHandoffManager.is_auto_responding`: if
   `message.channel` is a `discord.Thread` the predicate accepts, the bot responds with
   no mention needed. It reports False for an unmanaged thread *and* for a paused one,
   so a paused thread falls through to the mention/text-invocation gates below it.
   Everything upstream still applies unchanged: self/bot-author and message-type
   gates, the channel allowlist (via parent mapping), user blocks, and the privacy
   consent gate. Every human member who posts in an auto-responding thread gets
   responses, which is what "the conversation moved here" means, and per-user memory
   recall already keys off the triggering author per message, so multi-user threads
   need no memory changes. (One exception: when the mapped conversation is
   `OWNER_ONLY`, a non-owner's message is answered from a fresh root instead of the
   private transcript; see step 5.)
5. **Routing.** `resolve_conversation_for_message` gains a thread branch before
   fresh-root creation: a message in a managed thread, paused or not, resolves to the
   mapped root, so the whole thread shares one transcript, one per-root response lock
   (thread chatter serializes, which is what you want), and one persisted
   activated-tools set. There is one exception (`app/conversation_routing.py`): when
   the mapped conversation's `access_scope` is `OWNER_ONLY` and the poster is not its
   owner, the thread branch falls through and that poster gets a fresh root; the
   thread stays enrolled for its owner without exposing the private transcript to
   other participants. Replies to bot messages inside the thread also resolve via the
   existing `message_contexts` continuation path to the same root, so the two routes
   agree (this depends on the thread-channel-id persistence rule in step 2).
6. **Lifecycle.** Sending to an auto-archived public thread un-archives it (Discord
   behavior), so dormant threads resume transparently. A `NotFound`/`Forbidden` on send
   (thread deleted or locked) deletes the `thread_conversations` row, discards the id
   from both sets, and stays silent. The `leave_thread` tool (core but thread-masked,
   `MEMBER`, no arguments, acting only on the current thread, derived like
   `block_user`'s targets) queues a close when asked to stop, close, lock, or archive
   it: after the final reply is sent, the runtime calls
   `Thread.edit(locked=True, archived=True)` and then removes the mapping.

## Deterministic backstop (auto-handoff)

The prompt nudges the model to thread long replies, but in busy channels it doesn't
reliably comply, so an operator-gated backstop makes the behavior deterministic without
adding a parallel code path. At the send boundary in `handle_message`, *before* the
existing `thread_request` branch:

1. If the model already requested a thread, nothing changes; its choice wins.
2. Otherwise, when `THREAD_AUTO_HANDOFF_ENABLED` is on, the turn is not
   moderation-blocked, the message is not already in a thread, and the channel is
   enrolled (`load_channel_auto_thread`), `agent/auto_handoff.py:build_auto_handoff_request`
   measures the final reply (`splitlines()` vs `auto_thread_min_lines`, `len` vs
   `auto_thread_min_chars`). If it trips, it synthesizes a `ThreadRequest` named from
   the triggering question (mention stripped, collapsed, ≤100 chars; `"Chat with {bot}"`
   as the fallback).
3. The synthesized request flows into the **same** `_create_handoff_thread` →
   `target_channel = thread` → `send_response` path as the model path, so enrollment,
   persistence-follows-send-target, and the no-mention participation gate are all
   identical. Both paths react to the parent message with 🧵 after the thread is
   created.

Because the thread is created from the *user* message and the reply is just its first
message, the length check can run *after* generation and still thread retroactively:
there is no re-run, and the model needs no awareness that it was moved (the next turn
already carries thread context). `agent/auto_handoff.py` is `discord`-free and
unit-tested in isolation; the boundary only extracts the channel/mention primitives and
adds the reaction.

## Data model

```sql
CREATE TABLE IF NOT EXISTS thread_conversations (
    thread_id       TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    creator_user_id TEXT,
    created_at      REAL NOT NULL,
    auto_respond    INTEGER NOT NULL DEFAULT 1
);
```

The table is defined in `storage/db.py` (see [`database.md`](database.md)).
`creator_user_id` can be NULL, because the handoff initiator cannot be reconstructed
safely from conversation ownership, and a row without one fails closed to STAFF/Manage
Threads. CRUD lives in `storage/conversations.py`. `on_ready` calls
`ThreadHandoffManager.load()`, which fills both id sets from the table so that
participation *and* an explicit pause survive restarts.

`map_thread_conversation` is an `INSERT OR REPLACE` and therefore always writes
`auto_respond` explicitly; re-enrolling a thread would otherwise silently reset its
mode to the column default. `set_thread_auto_respond` reports whether a row was
actually updated, since the mapping can be swept out from under a live thread id
(retention, privacy deletion), and the manager drops the stale id instead of
announcing a mode change that would not survive a restart.

Why not reuse `message_contexts`? A thread created from a message shares that message's
id, and the trigger message is already mapped, so the lookup would *appear* to work for
free. But that mapping means "this message belongs to this conversation," not "the bot
owns this thread": any user could right-click → Create Thread on a mapped message and
the bot would silently start auto-responding in a thread it never opted into.
Enrollment has to be an explicit bot-side write.

## What does not change

- The parent channel stays mention-only. Replies to the bot's pre-handoff messages in
  the main channel still continue the same root *in the channel* (no redirect to the
  thread). The current implementation does not redirect users across surfaces.
- Automatic thread creation happens only through the operator-gated backstop above
  (off by default, per-channel opt-in). Outside it, only the model opts in: one
  single-slot request per turn, one thread per triggering message (Discord-enforced),
  which bounds abuse from "make a thread" spam to one thread per mention-gated turn.
- Memory write paths and trust tiers are untouched.

## Testing

- `should_respond`: an auto-responding thread answers without a mention; an unmanaged
  thread still requires one; a **paused** thread requires one but still answers on
  @mention and on `hey <bot> …`; gate off ⇒ mention-only everywhere; blocks and the
  allowlist still win.
- Routing: a managed-thread message resolves the mapped root (no fresh root) whether
  or not it is paused; an in-thread reply-to-bot resolves the same root via
  `message_contexts`.
- Mode: pause/resume flip the set and the column; both reject an unmanaged thread; the
  pause note names the resume tool and the wake phrase; `load()` restores a pause
  across a restart.
- Lifecycle authorization: the durable handoff initiator, configured STAFF, and a
  caller with effective Discord Manage Threads may close/pause/resume; ordinary
  participants fail closed, as does a row whose initiator was cleared by a
  `/privacy` deletion.
- Tool visibility: `thread_state_blocked_tools` hides all three outside a managed
  thread and exactly one of pause/resume inside one.
- Tool: queue/replace semantics, in-thread rejection, name validation; a
  side-effect-free rejection leaves a prior pending request intact (embed parity).
  `leave_thread` queues a close only for the current managed thread.
- Boundary: creation failure falls back to an in-channel reply; `NotFound` on send
  prunes the mapping; the enrollment row is written exactly once; close requests
  lock/archive the thread after the final reply.
- Persistence: reply chunks sent into a thread are mapped in `message_contexts` under
  the thread id (an in-thread reply-to-bot resolves continuation even after
  `leave_thread`).
