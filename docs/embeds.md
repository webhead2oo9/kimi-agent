# Discord embed builder

Most of what the bot says is plain text. Sometimes a reply is really a *card*: a server rule, a release note, a leaderboard, a generated image with a caption. `build_discord_embed` lets the model attach one rich Discord embed to the reply it is already writing.

The tool is searchable, meaning the model has to activate it through `browse_tools` before it can call it. It is open to every `MEMBER`, has no config gate, and needs no external service, so it is always available to be found.

## Available fields

Every field is optional, but an embed has to contain at least a title, a description, a field, or an image (see [Limits](#limits)). All URLs must be HTTPS. Each text field has the same character cap Discord enforces, and the text fields together cannot exceed 6,000 characters.

| Field | Type | Purpose |
|---|---|---|
| `title` | string | Card title (≤256 chars). |
| `url` | https string | When set, the title becomes a hyperlink pointing here. |
| `description` | string | Main body text. Supports Discord Markdown (≤4096 chars). |
| `color` | string or int | Accent bar on the left of the card. Accepts `"#5865F2"`, `"0x5865F2"`, or an integer 0–16777215. |
| `author_name` | string | Small author line above the title. |
| `author_url` | https string | Makes the author line a hyperlink. |
| `author_icon_url` | https string | Small icon next to the author line. |
| `fields` | list | Up to 25 `{name, value, inline}` rows. |
| `footer_text` | string | Small footer line. |
| `footer_icon_url` | https string | Small icon next to the footer. |
| `timestamp` | bool | Add the current time to the footer. |
| `image_url` | https string | Large image. Discord fetches it. |
| `image_workspace_path` | string | Large image from the user's workspace or a `generated/` artifact. Mutually exclusive with `image_url`. |
| `thumbnail_url` | https string | Small corner thumbnail. |

## What it looks like

The model calls the tool with the parts it wants:

```json
{
  "title": "Server Rules",
  "description": "The short version. Full text is pinned in #welcome.",
  "color": "#5865F2",
  "fields": [
    { "name": "1. Be decent", "value": "No harassment, slurs, or dogpiling.", "inline": false },
    { "name": "2. Keep it on topic", "value": "Off-topic chatter goes in #lounge.", "inline": true },
    { "name": "3. Ask before DMing", "value": "Especially for support.", "inline": true }
  ],
  "footer_text": "Updated by the mod team",
  "timestamp": true
}
```

The call returns `{"queued": true, "image": null}` rather than the embed itself, and nothing is sent yet. The embed rides out on the final reply. What lands in Discord is a blue-barred card with those three fields, the last two side by side, and a timestamped footer.

The model's own prose becomes the caption above the card:

```
Kimi
  Here's the short version:

  ┃ Server Rules
  ┃ The short version. Full text is pinned in #welcome.
  ┃
  ┃ 1. Be decent
  ┃ No harassment, slurs, or dogpiling.
  ┃
  ┃ 2. Keep it on topic     3. Ask before DMing
  ┃ Off-topic chatter …     Especially for support.
  ┃
  ┃ Updated by the mod team · Today at 14:02
```

If the model writes no prose, the message is embed-only. That is supported on purpose: when an embed is pending, `agent/core.py` sends an empty caption instead of the usual "I'm not sure how to respond to that." fallback text.

## One embed per reply

Calling `build_discord_embed` twice in a turn **replaces** the pending embed rather than stacking a second one. The replacement swaps out the image too, so an abandoned embed never leaves its picture attached to the reply.

The tool returns a short receipt rather than the embed because there is nothing for the model to reason about in it. Most tools hand text back for the model to read; this one queues something for Discord, and the model's next job is writing the caption.

## Images

There are two mutually exclusive ways to set the large image:

- **`image_url`** takes an external `https://` address. Discord fetches it; the bot never does, so there is no way to make the bot reach an internal address through it. A scheme and length check is all the guard needed.
- **`image_workspace_path`** takes a file the bot already has: a file in the user's workspace, named by path, or a `generated/…` file from this conversation, addressed by the exact path the tool that produced it handed back (the conversation id in the middle of that path is not guessable). The path is resolved through `WorkspaceManager.resolve_user_file_path`, or `resolve_context_generated_file` for generated files. The file is uploaded with the reply and the embed points at it as `attachment://<basename>`, so the picture and the card arrive in the same first Discord message.

`thumbnail_url` separately takes an external `https://` address and can accompany either large-image source.

```json
{
  "title": "Weekly activity",
  "description": "Messages per channel, last 7 days.",
  "image_workspace_path": "activity-chart.png"
}
```

Because the reference is by *basename*, two files called `chart.png` on one reply would be ambiguous. The tool refuses rather than guessing:

> A file named 'chart.png' is already attached to this reply; rename the image so its filename is unique.

## Limits

Most text limits mirror Discord's. The tool additionally requires HTTPS URLs and checks URL length and color locally. `build_embed_payload` enforces every limit before anything is queued and returns an error message the model can act on (for example "title must be 256 characters or fewer") rather than a bare rejection.

| Field | Limit |
|---|---|
| `title` | ≤ 256 characters |
| `description` | ≤ 4096 |
| `fields` | ≤ 25 rows; each `name` ≤ 256, `value` ≤ 1024, neither empty |
| `footer_text` | ≤ 2048 |
| `author_name` | ≤ 256 |
| **all of the above, combined** | **≤ 6000** |
| any URL | `https://` only, ≤ 2048 characters |
| `color` | `#RRGGBB`, `0x…`, or an integer in 0–16777215 |

An embed also has to *be* something: at least one of a title, a description, fields, or an image, or the call is rejected.

Validation has no side effects. Cheap rules run first and the workspace image is resolved last, so a call that was going to fail anyway never touches the disk. A rejected call leaves the turn state and any previously queued embed unchanged.

## How it reaches Discord

The embed travels in the same frozen `TurnOutbox` as queued files and thread
directives:

```
build_discord_embed handler (tools/embeds.py)
  → build_embed_payload: validate → EmbedSpec (+ optional EmbedAttachment), no ctx mutation
  → on success: MessageContext.update_outbox(embed=..., embed_attachment=...)
MessageContext.outbox → ConversationContext.pending_outbox     (synced in agent/core.py)
ConversationRunResult.outbox → TurnResult.outbox               (agent/turn.py:execute_turn)
guild/user-app adapter reads result.outbox.embed
  → discord_adapter.io.send_response(..., embed=spec)
  → chunk 0 only: build_embed(spec) → channel.send(content, embeds=[e], files=[...])
```

`TurnOutbox`, `EmbedSpec`, and `EmbedAttachment` are frozen dataclasses. Each
successful tool call replaces the outbox snapshot, so a prior snapshot cannot
be changed through an aliased list or mapping. The one `discord.Embed` is
assembled at the very end, in `discord_adapter/io.py:build_embed`, which keeps
`agent/core.py` Discord-agnostic like the rest of the loop.

At that same send boundary, standalone `http://` and `https://` links in the reply text are wrapped as Discord autolinks before chunking. That prevents automatic page-preview cards without setting the message-wide suppress-embeds flag. Uploaded image previews, explicit `EmbedSpec` cards, Markdown links, existing autolinks, and URLs inside code are left as they are.

The embed's image is not added to `MessageContext.outbox.output_files` when the
tool runs. It travels in `outbox.embed_attachment` and is added to the outgoing
file tuple once, in `execute_turn`. That single late step is what makes "the
second call replaces the first" safe: a discarded embed's image was never in
the file list.

## Staying in the transcript

An embed-only reply has no message text. Saving it as-is would store an empty row, and the reply would vanish from the bot's own memory of the conversation. Instead, the turn adapters (`app/guild_turn_adapter.py`, `app/user_app_turn_adapter.py`) store a stand-in line from `embed_transcript_summary(spec)`:

```
[embed] Server Rules: The short version. Full text is pinned in #welcome.
```

It prefers the title plus the first description line, then falls back through title alone, description alone, author name, first field name, and finally `(image)`, capped at 200 characters. Later turns read that line and know the bot answered with a card.

## Notes

- Embeds can't ping anyone. Only message `content` triggers mentions, so the embed body adds no mention-injection surface.
- The embed is the model's own writing, not fetched external data, so it isn't wrapped in the untrusted-context framing that retrieval tools get.
- The tool's own description is short and points at the built-in `embed` skill for field meanings and worked examples. That keeps the full schema out of every turn's prompt.
- Out of scope: pagination, buttons and select menus, `discord.ui.View`, editing an already-sent message's embed, and multiple stacked embeds.
