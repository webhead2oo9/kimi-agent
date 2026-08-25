# Discord embed builder

Most of what the bot says is plain text. Sometimes, though, a reply is really a
*card*: a server rule, a release note, a leaderboard, a generated image with a
caption. `build_discord_embed` lets the model attach one rich Discord embed to the
reply it is already writing.

The tool is searchable (`browse_tools` has to activate it first), open to `MEMBER`, and
has no config gate or external dependency, so it is always there to be found.

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

The call returns `{"queued": true, "image": null}` rather than the embed itself, and
nothing is sent yet; the embed rides out on the final reply. What lands in Discord is a
blue-barred card with those three fields, the last two side by side, and a timestamped
footer.

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

If the model writes no prose, the message is embed-only. That is a real case rather
than an accident: whenever an embed is pending, `agent/core.py` keeps an empty
`final_text` instead of substituting the usual "I'm not sure how to respond" fallback.

## One embed per reply

Calling `build_discord_embed` twice in a turn **replaces** the pending embed rather than
stacking a second one. The replacement swaps out the image along with it, so an
abandoned embed never leaves its picture attached to the reply.

This is also why the tool returns a receipt instead of data. Most tools hand a string
back to the *model* to reason about; this one queues something for *Discord*. There is
nothing useful to think about in the return value, and the model should get on with its
caption.

## Images

There are two ways to get an image in, and they are mutually exclusive:

- **`image_url` / `thumbnail_url`** take an external `https://` address. Discord
  fetches them; the bot never does. That is worth stating plainly, because it means
  there is no SSRF surface here at all, and a scheme and length check really is the
  only guard needed.
- **`image_workspace_path`** takes a file the bot already has: something in the user's
  workspace by name, or a `generated/…` artifact from this conversation, addressed by
  the exact path the tool that produced it handed back (the conversation segment in the
  middle is not guessable). It resolves through
  `WorkspaceManager.resolve_user_file_path`, falling back to
  `resolve_context_generated_file` for generated paths, uploads with the reply, and the
  embed points at it as `attachment://<basename>` so the picture and the card ride the
  same first Discord chunk.

```json
{
  "title": "Weekly activity",
  "description": "Messages per channel, last 7 days.",
  "image_workspace_path": "activity-chart.png"
}
```

Because the reference is by *basename*, two files called `chart.png` on one reply would
be ambiguous. The tool refuses rather than guessing:

> A file named 'chart.png' is already attached to this reply; rename the image so its
> filename is unique.

## Limits

These are Discord's limits, not ours. `build_embed_payload` enforces them before
anything is queued and returns a `tool_error` the model can correct itself from, such
as "title must be 256 characters or fewer", rather than a bare rejection with no handle
on it.

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

An embed also has to *be* something: at least one of a title, a description, fields, or
an image, or the call is rejected.

Validation is side-effect free, and it is ordered to stay that way. Every cheap rule
runs first and the workspace image resolves last, so a call that was going to fail
anyway never touches the disk. A rejected call leaves `ctx` and any previously pending
embed exactly as they were.

## How it reaches Discord

The embed follows the same rails as the `output_files` queue:

```
build_discord_embed handler (tools/embeds.py)
  → build_embed_payload: validate → EmbedSpec (+ optional EmbedAttachment), no ctx mutation
  → on success: MessageContext.embed / MessageContext.embed_attachment
ConversationContext.pending_embed / pending_embed_attachment   (synced in agent/core.py)
TurnResult.embed                                               (agent/turn.py:execute_turn)
discord_adapter.io.send_response(..., embed=spec)              (via discord_gateway + runtime)
  → chunk 0 only: build_embed(spec) → channel.send(content, embeds=[e], files=[...])
```

`EmbedSpec` and `EmbedAttachment` are frozen dataclasses that import nothing from
`discord`. The one `discord.Embed` is assembled at the very end, in
`discord_adapter/io.py:build_embed`, which is what keeps `agent/core.py`
Discord-agnostic like the rest of the loop.

At that same send boundary, standalone `http://` and `https://` links in the reply text
are wrapped as Discord autolinks before chunking. That prevents automatic page-preview
cards without having to set the message-wide suppress-embeds flag. Uploaded image
previews, explicit `EmbedSpec` cards, Markdown links, existing autolinks, and URLs
inside code are left as they are.

The embed's image is **not** pushed straight onto `MessageContext.output_files`. It
travels as an `EmbedAttachment` and is materialized onto the file rails once, at the
`execute_turn` boundary. That single late step is what makes "the second call replaces
the first" safe: a discarded embed's image was never on the rails to begin with.

## Staying in the transcript

An embed-only reply has no message content, so a naive persist would store an empty row
and the reply would vanish from the bot's own memory of the conversation. Instead,
`app/runtime.py` stores a stand-in from `embed_transcript_summary(spec)`:

```
[embed] Server Rules: The short version. Full text is pinned in #welcome.
```

It prefers the title plus the first description line, then falls back through title
alone, description alone, author name, first field name, and finally `(image)`, capped
at 200 characters. Later turns read that line and know the bot answered with a card.

## Notes

- Embeds cannot ping anyone. Only message `content` triggers mentions, so the embed
  body adds no mention-injection surface.
- The embed is the model's own writing, not fetched external data, so it is **not**
  wrapped in the untrusted-context framing that retrieval tools get.
- The tool's own description is terse and points at the instruction-only `embed` skill
  for field semantics and worked examples, which keeps the full schema out of every
  turn's prompt.
- Out of scope: pagination, buttons and select menus, `discord.ui.View`, editing an
  already-sent message's embed, and multiple stacked embeds.
