# Learning

Learning is the staff gesture for adding shared knowledge to the bot. It is
not a tool of its own. Instead, the model routes each piece of knowledge to
one of two existing, individually tier-gated sinks, which keeps the sink
visible in the tool log rather than hidden behind a wrapper.

| Kind of knowledge | Sink | Example |
|---|---|---|
| A **fact**: a server rule, a date, an event, a recommendation, community lore | Community memory via `teach` ([memory.md](memory.md)) | "Raid night moved to Thursdays" |
| A **procedure**: how to handle a recurring request, steps to follow, a reusable workflow | A skill via `skill_create` / `skill_edit` ([skills README](../bot/skills/README.md)) | "How to onboard a new moderator" |

Something that is genuinely both gets both.

## Triggers

**In conversation.** This path is pure prompting: the learn bullets under
`## Behavioral Rules` in `config/prompt.md` tell the model to classify the
knowledge, check the `<skills>` index before creating a procedure, prefer
`skill_edit` (`append` or `edits`) over a near-duplicate `skill_create`, and say
in one line what was stored and where. This path does not require a community
memory lookup before teaching a fact.

**The "Teach Kimi" message context menu** (the name follows `BOT_NAME`;
`commands/learn_cmd.py`) captures a good explanation right where it was
written. It is staff-gated at the interaction, deferred and answered
ephemerally, and refuses bot-authored and empty messages. Its dedicated prompt
checks `recall_community` before teaching a fact and skips duplicates.

## The context-menu turn

The context menu runs `app/learn_turn.py:run_learn_turn`, never
`handle_message`. The ordinary path would be wrong here on three counts: it
resolves trust from the message's author (but the actor is a different
person), it persists a transcript, and it replies in-channel.

The turn's tool surface is narrowed **structurally**. `build_learn_registry`
hands it an independent `clone_without` registry containing only
`LEARN_TOOLS`, so a tool registered on the main registry mid-turn cannot reach
it. That matters because a successful `skill_create` itself fires a skill-tool
reload; a snapshot denylist could never promise isolation, since it can only
name tools that existed when it was built. The equivalent
`ConversationContext.blocked_tools` rides along as defense in depth (it also
hides the names from the tool list and the `browse_tools` catalog). Tool
entries are shared, so `min_tier`, `owner_only`, and `guild_ids` still gate at
dispatch.

`trigger_discord_message_id` is passed from `LearnTarget.message_id`; without
it, every audit card for a context-menu learn would silently lose its source
link.

The turn is capped at `LEARN_TURN_TIMEOUT_SECONDS` (a module constant in
`app/learn_turn.py`, 600s), which sits comfortably under Discord's 15-minute
interaction-token expiry. The deployment-wide ReAct timeout is hours long and
would leave the reply stranded.

The turn's prompt template is `config/prompts/commands/learn.md`
(`command_template="learn"`). It can be overridden per guild at
`config/prompts/commands/learn/<guild_id>.md`, and falls back to the normal
template chain if removed. See the
[full-template guide](../bot/config/prompts/README.md). The template leaves out
the content-rating line, because a learn turn writes knowledge; it does not
chat with members.

## Hostile messages

A taught message can become durable instructions used on later turns. Only
STAFF can trigger learning, and the context-menu flow keeps member-authored
content visibly untrusted:

- Everything the message's author controls goes inside the
  `--- BEGIN UNTRUSTED MESSAGE CONTENT ---` fence: the body, their display
  name, and attachment filenames. Leaving a display name in the surrounding
  prose would hand an attacker one line of trusted-looking text for free.
- `_neutralize_fence_markers` rewrites any line that imitates a fence marker,
  so quoted text cannot close the fence early and get the rest of itself read
  as instructions.
- The staff member's note is the only instruction outside the fence.
- The prompt tells the model that fenced content asking for privileged action
  is something to report back, not something to do.

This framing reduces prompt-injection risk but cannot prove that taught content
is safe or correct. Staff should review the stored result named in the reply. A
configured learn-log channel adds a shared review trail, but it is optional and
is not an access-control boundary. This reasoning depends on STAFF being the only
trigger; widening the learn surface requires revisiting it.

## Audit log

Both sinks emit a `LearnEvent` through an injected hook, carrying a bounded
record of what was actually written: the created body, or the appended,
patched, or replaced text for an edit. A card that only says "Skill updated"
is useless for review.

When the guild fragment has a valid `learn_log_channel_id`, `app/learn_log.py`
attempts to post a bounded card after the write commits. The field is optional:
an absent or unreachable channel does not disable learning, and a Discord post
failure does not roll back the stored knowledge. Without a working log channel,
the context-menu confirmation remains visible only to the staff member who
triggered it. The conversational path uses the same optional feed.

`tools/learn.py` holds the feature's discord-free vocabulary (`LearnTarget`,
`LearnEvent`, `LearnHook`), because `tools/` may not import `discord` and
`commands/` may not import `app/` (`app/runtime.py` imports every command
module, so that edge would be an import-order cycle; the rule is frozen in
`tests/test_package_graph.py`).

`emit_learn_event` takes a *factory* rather than a finished event, so that
constructing the event sits inside the same guard as delivering it. Both run
after the write is committed, where an escaping exception would report failure
for a write that actually succeeded and invite a duplicate retry.
