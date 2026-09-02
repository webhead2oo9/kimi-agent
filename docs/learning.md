# Learning

Learning is how staff add shared knowledge to the bot. It is not a tool of its own. The model routes each piece of knowledge to one of two existing, separately tier-gated tools, so the tool log shows exactly where the knowledge went.

| Kind of knowledge | Sink | Example |
|---|---|---|
| A **fact**: a server rule, a date, an event, a recommendation, community lore | Community memory, via `teach` ([memory.md](memory.md)) | "Raid night moved to Thursdays" |
| A **procedure**: how to handle a recurring request, steps to follow, a reusable workflow | A skill, via `skill_create` / `skill_edit` ([skills README](../bot/skills/README.md)) | "How to onboard a new moderator" |

## Triggers

**In conversation.** A staff member tells the bot something and asks it to remember. This path is pure prompting: the learn bullets under `## Behavioral Rules` in `config/prompt.md` tell the model to decide whether it has a fact or a procedure, check the `<skills>` index before creating a new procedure, prefer `skill_edit` (`append` or `edits`) over a near-duplicate `skill_create`, and say in one line what was stored and where. This path does not look up community memory before teaching a fact.

**The "Teach Kimi" message context menu** (the name follows `BOT_NAME`; `commands/learn_cmd.py`) lets staff right-click a good explanation and teach it right where it was written. It is staff-only, refuses a blocked user even if they hold staff standing (a block can predate a promotion or a per-guild trust grant), honours `PRIVACY_CONSENT_ENABLED` before running, answers only the staff member (ephemerally), and refuses bot-authored and empty messages. Its dedicated prompt checks `recall_community` before teaching a fact and skips duplicates.

## The context-menu turn

The context menu runs `app/learn_turn.py:run_learn_turn`, not the ordinary message path. The ordinary path would be wrong on three counts: it takes trust from the message's author, but here the person acting is someone else; it saves a transcript; and it replies in the channel.

The turn gets its own smaller tool registry, built by `build_learn_registry` from a copy (`clone_without`) that contains only `LEARN_TOOLS`. Because it is a separate registry, a tool registered on the main one mid-turn cannot appear in it. That matters: a successful `skill_create` triggers a skill-tool reload, and a denylist written at the start of the turn could never name tools that did not exist yet. The equivalent `ConversationContext.blocked_tools` denylist rides along as a second layer, and also hides those names from the tool list and the `browse_tools` catalog. Tool entries are shared with the main registry, so `min_tier`, `owner_only`, and `guild_ids` are still enforced when a tool is called.

The turn is given the taught message's id (`LearnTarget.message_id` becomes `trigger_discord_message_id`) so the audit card can link back to the source message.

The turn is capped at `LEARN_TURN_TIMEOUT_SECONDS` (a constant in `app/learn_turn.py`, 600 seconds), comfortably under Discord's 15-minute limit on answering an interaction. The deployment-wide ReAct timeout is hours long and would leave the reply with nowhere to go.

The turn's prompt template is `config/prompts/commands/learn.md` (`command_template="learn"`). It can be overridden per guild at `config/prompts/commands/learn/<guild_id>.md`, and falls back to the normal template chain if removed. See the [full-template guide](../bot/config/prompts/README.md). The template leaves out the content-rating line, because a learn turn writes knowledge; it doesn't chat with members.

## Hostile messages

A taught message can become durable instructions that shape later turns, which makes it a target for prompt injection. Only STAFF can trigger learning, and the context-menu flow keeps member-authored content visibly untrusted:

- Everything the message's author controls goes inside the `--- BEGIN UNTRUSTED MESSAGE CONTENT ---` fence: the body, their display name, and attachment filenames. Leaving the display name outside would hand an attacker one line of trusted-looking text for free.
- `_neutralize_fence_markers` rewrites any line that imitates a fence marker, so quoted text can't close the fence early and get the rest of itself read as instructions.
- The staff member's note is the only instruction outside the fence.
- The prompt tells the model that fenced content asking for privileged action is something to report back, not something to do.

This framing reduces prompt-injection risk but cannot prove that taught content is safe or correct. Staff should review the stored result named in the reply. A learn-log channel adds a shared review trail, but it is optional and not an access control. All of this rests on STAFF being the only trigger; if the learn surface is ever widened, revisit it.

## Audit log

Both tools emit a `LearnEvent` carrying a size-limited record of what was actually written: the new body, or the appended, patched, or replaced text for an edit. A card that only says "Skill updated" would be useless for review.

When the guild fragment has a valid `learn_log_channel_id`, `app/learn_log.py` posts a card there after the write commits. The field is optional: an absent or unreachable channel does not disable learning, and a failed Discord post does not roll back the stored knowledge. Without a working log channel, a context-menu teach is visible only to the staff member who did it. The conversational path uses the same optional feed.

`tools/learn.py` holds the feature's Discord-free types (`LearnTarget`, `LearnEvent`, `LearnHook`). They live there because `tools/` may not import `discord` and `commands/` may not import `app/`; `app/runtime.py` imports every command module, so the reverse edge would be an import cycle. `tests/test_package_graph.py` enforces the rule.

`emit_learn_event` takes a function that builds the event rather than a finished event, so building it is covered by the same error guard as delivering it. Both happen after the write is committed, where an escaping exception would report failure for a write that actually succeeded and invite a duplicate.
