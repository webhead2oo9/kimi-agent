This is the pinned "Start here" thread for new arrivals.

- Answer setup questions directly in here; do not send people elsewhere.
- Assume the reader has installed nothing and configured nothing yet. Do not skip
  steps because they seem obvious.
- Prefer one complete walkthrough over a list of options.


---- HOW THIS FILE WORKS (delete everything below when you copy it) ----

Rename this file to one thread's own id: `1234567890.md`.

This is the narrowest scope there is, and most servers should not need it. For
"every thread in this channel" use `config/channel_threads/<channel_id>.md`
instead. That scope covers current and future threads at once, and keeps you
from writing a file per thread. Reach for this one only when a single long-lived
thread genuinely needs its own rules: a pinned start-here thread, a standing
megathread, a recurring event thread.

## Precedence

Inside a thread the bot fills `<channel_instructions>` with the most specific
non-empty body it finds:

  1. `config/threads/<thread_id>.md`           this one thread                 <- this file
  2. `config/channel_threads/<channel_id>.md`  every thread under the channel
  3. `config/channels/<channel_id>.md`         the channel itself

It **replaces**, it does not append: text here suppresses both scopes below, so
write complete instructions rather than a diff against them. Delete the text (or
the file) to go back to inheriting.

## Clean up after yourself

Nothing sweeps these files. Threads are far more numerous and shorter-lived than
channels, and a thread that gets archived or deleted leaves its fragment behind.
Review and remove stale files directly. Prefer the channel-wide scope unless one
thread really needs its own.

## What does not belong here

Body only, no frontmatter. `pinned_tools`, `blocked_tools`, `thread_handoff`,
`thread_auto_respond`, and the `auto_thread_*` keys are read only from
`config/channels/<channel_id>.md` and `config/servers/<guild_id>.md`. Inside a
thread they resolve against the parent channel, so keys placed here would look
configured and do nothing.

The body renders under a `## Thread Instructions` heading (a channel fragment
renders under `## Channel Instructions`).
