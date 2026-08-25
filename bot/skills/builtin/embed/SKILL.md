---
name: embed
description: Compose one rich Discord embed with build_discord_embed when a structured card is clearer than plain chat.
tags: [discord, embed, formatting, card]
---

# Discord embeds

Use `build_discord_embed` when a bordered card materially improves the reply:
for example a compact status card, release summary, profile, or a few labeled
facts. Prefer plain text for ordinary conversation. The tool is searchable, so
load it with `browse_tools` when it is not already visible.

The tool queues one embed for the current reply. Ordinary reply text becomes a
caption above it. A later call in the same turn replaces the pending embed,
including its image.

All fields are optional, but include at least a title, description, field, or
large image:

- `title` is at most 256 characters; optional `url` makes it clickable.
- `description` supports Discord Markdown and is at most 4096 characters.
- `fields` accepts up to 25 `{name, value, inline}` objects. Names are at most
  256 characters and values at most 1024.
- `color` accepts a decimal or hex string such as `"5793266"`, `"#5865F2"`,
  or `"0x5865F2"`.
- `author_name`, `footer_text`, their optional URL/icon fields, and
  `timestamp: true` add compact metadata.
- `thumbnail_url` adds a corner image.
- For the large image, pass either an external `image_url` or a workspace or
  generated `image_workspace_path`, never both.

Every URL must use HTTPS. All text across the embed has a combined 6000-character
limit. If validation fails, change the named field and call the tool again; do
not paste the raw failure into the user-facing reply.
