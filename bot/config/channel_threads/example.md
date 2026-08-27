You are in a troubleshooting thread that was split off from #help.

- One person, one problem. Stay scoped to the issue that started this thread.
- The existing thread is context you already have. Do not re-ask
  for their setup, versions, or steps the user has already given. Read back
  instead.
- Work one fix at a time and wait for the result before suggesting the next.
- If it turns out to be a different problem, say so and point them back to
  #help rather than starting a second investigation in here.
- When it's solved, say so plainly so the thread can be closed.


---- HOW THIS FILE WORKS (delete everything below when you copy it) ----

Rename this file to the PARENT CHANNEL's id: `1234567890.md`, not a thread id.
It then applies to every thread under that channel, current and future. That is
the point of this scope: threads are too many and too short-lived to configure
one at a time.

## The pairing this example is half of

The text above is written to sit alongside a `config/channels/<same id>.md` that
covers the channel itself. Together they read as one policy:

    config/channels/1234567890.md          (the channel)
      You are in #help. Support only, no off-topic chat.
      Always get their setup and version details before suggesting fixes.
      Move anything that needs more than two replies into a thread.

    config/channel_threads/1234567890.md   (this file, covering its threads)
      You are in a troubleshooting thread that was split off from #help.
      The existing thread is context you already have. Do not
      re-ask for their setup or version details...

The channel's job is to open a case; the thread's job is to work it. Note that
the thread text deliberately *cancels* the channel's "always ask for specs"
instruction, because inside the thread that information has already been given
and re-asking is the exact annoyance this scope exists to fix.

## Precedence

Inside a thread the bot fills `<channel_instructions>` with the most specific
non-empty body it finds:

  1. `config/threads/<thread_id>.md`           this one thread
  2. `config/channel_threads/<channel_id>.md`  every thread under the channel  <- this file
  3. `config/channels/<channel_id>.md`         the channel itself

It **replaces**, it does not append. If this file has text, the channel's own
instructions do not also apply inside its threads. That is why the example
above restates the parts of #help's policy it still wants ("stay scoped",
"point them back") instead of assuming they carry over. Write these as complete
instructions, not as a diff against the channel.

Delete the text (or the file) to go back to inheriting the channel.

## Forums

Every post in a forum channel is a thread. `config/channels/<forum_id>.md` is
the inherited default for those posts; this file, using the same forum id,
replaces that default inside every post when it has a non-empty body.

## What does not belong here

Body only, no frontmatter. `pinned_tools`, `blocked_tools`, `thread_handoff`,
`thread_auto_respond`, and the `auto_thread_*` keys are read only from
`config/channels/<channel_id>.md` and `config/servers/<guild_id>.md`. Inside a
thread they resolve against the parent channel, so keys placed here would look
configured and do nothing.

The body renders under a `## Thread Instructions` heading (a channel fragment
renders under `## Channel Instructions`).

Create and edit this file directly. Use the precedence list above to determine
which scope the bot will load before replacing an inherited fragment.
