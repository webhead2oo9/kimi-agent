# Full system-prompt layout overrides

Files here replace the **entire** prompt layout for a scope, unlike the small
slot fragments in `config/channels/`, `config/channel_threads/`, and
`config/threads/` (which fill the `<channel_instructions>` slot of the active
template) and `config/servers/` (which fills `<server_instructions>`).

Most channels and servers want a fragment, not a full override. Reach for a full
override only when a scope needs a different layout: different section
order, sections removed, or a different persona/tone.

## Resolution (most specific wins)

```
prompts/commands/<name>/<guild_id>.md
                                      (guild-specific command template)
  > prompts/commands/<name>.local.md  (ignored deployment-local override)
  > prompts/commands/<name>.md        (tracked/shared command template)
  > prompts/channels/<thread_id>.md   (inside a thread, when present)
  > prompts/channels/<channel_id>.md  (the channel, inherited by its threads)
  > prompts/servers/<guild_id>.md
  > ../prompt.md                     (the default layout)
```

`<thread_id>` / `<channel_id>` / `<guild_id>` are the numeric Discord ids. A
thread inherits its parent channel's full template unless a file keyed by the
thread's own id replaces it. There is no `prompts/threads/` directory; a
thread's own override is a `prompts/channels/` file named for the thread id. A
per-guild command template is a file named for the guild id inside a directory
named for the command (`prompts/commands/learn/<guild_id>.md`). The first file
that exists wins; its `<placeholder>` tokens are
then filled the same way as the default template.

Subdirectories (`commands/`, `channels/`, `servers/`) are created on demand; a
missing file falls through to the next resolution level. Create and edit
these files directly under `<CONFIG_DIR>/prompts/`; they are read fresh each
turn.

## Tokens

The template is literal prose with `<token>` placeholders. Substitution is
single-pass: an unknown token is left literal, and inserted content is never
re-scanned for tokens, so nothing a user or tool wrote can expand into a trusted
section. The full set (`config/fragments/prompt.py:build_system_prompt`):

| Token | Fills with |
| --- | --- |
| `<date>` | Today's date. |
| `<bot_name>` | `BOT_NAME`, sanitized. |
| `<user>`, `<user_id>`, `<trust_tier>` | The current speaker's display name (sanitized), Discord id, and resolved tier. |
| `<model>` | The selected chain's primary chat model. It does not change even when the turn falls back to another model. |
| `<channel>`, `<server>` | Sanitized Discord channel and guild names. |
| `<persona>` | `config/persona.md`, or the current user's compiled persona override in a code-owned frame (see below). |
| `<channel_instructions>` | The first non-empty body of `config/threads/<thread_id>.md` > `config/channel_threads/<parent_channel_id>.md` > `config/channels/<channel_id>.md` inside a thread, else `config/channels/<channel_id>.md`. Thread-scoped bodies render under a `## Thread Instructions` heading; the channel body renders under `## Channel Instructions`. |
| `<server_instructions>` | `config/servers/<guild_id>.md` under `## Server Instructions`. |
| `<onboarding>` | A `## New User` block for a speaker inside their first `NEW_USER_ONBOARDING_TURNS` interactions (orientation plus the `block_user`/report-to-staff guidance); empty otherwise. Omit it and new users get no special handling in that scope. |
| `<skills>` | The shared skills index visible in this guild. |
| `<personal_skills>` | The current user's `## Your Personal Skills` block, when they have any. Omit it only for intentionally tool-less layouts. |
| `<community_knowledge>` | Recalled community memory for this turn. |
| `<current_context>` | A summary block of the scalars above (user, id, tier, primary model, channel, server). |

Discord-sourced scalars are flattened (newlines and colons removed) before
substitution so a crafted channel or guild name cannot forge prompt structure.

## Authoring a full override

Copy `../prompt.md` and edit it.

- Reorder a section by moving its token; remove one by deleting its token.
- Selection chooses exactly one template and never merges it with `prompt.md`,
  so a full override inherits **nothing**. The safety and guardrail rules, the
  content-rating line, error hygiene, and the memory/tool-routing rules are all
  prose in the default template: carry across whatever the scope still needs.
  `<safety>` and `<guardrails>` are not tokens and no code appends those rules.
  Command templates are narrower: `commands/learn.md` carries the
  safety and guardrail rules but omits the content-rating and
  error-hygiene lines, since a learn turn reports privately to Staff and the
  conversational template already governs what members see.
- When the current user has a compiled persona override, `<persona>` renders it
  inside a code-owned frame that states a 13+ floor and refuses adult or graphic
  content regardless of what the template says; omit `<persona>` in layouts that
  should use neither persona.

A deployment keeps its live `prompt.md`, `persona.md`, command templates, and
any full overrides under the private tree selected by `CONFIG_DIR`; the public
files here are generic starting points. The private tree layout, the deploy and
restart boundary, and the list of files that must never be committed are in
[`docs/instance-data.md`](../../../docs/instance-data.md).

For a small in-checkout deployment that only needs to customize a shared
command prompt, use `<name>.local.md`. These files are gitignored and runtime
prefers them over the tracked `<name>.md` default. In particular, personal
user-app chat uses `commands/chat.local.md`; see
[`docs/user-app.md`](../../../docs/user-app.md).

The tracked `commands/chat.md` intentionally contains `<persona>`. It therefore
uses `persona.md` by default and the compiled per-user persona when one exists,
including that persona renderer's rating and safety boundaries. To give only
`/chat` a different persona, replace or remove `<persona>` in the complete
`commands/chat.local.md` layout.

## Fragment frontmatter

Slot fragments in `config/channels/` and `config/servers/` may start with YAML
frontmatter (`pinned_tools`, `blocked_tools`, thread switches, trust and log
wiring); see each directory's `example.md` and `docs/configuration.md`.
Frontmatter is config, not prompt text: it is stripped before the body fills its
slot. The thread-scoped `config/threads/` and `config/channel_threads/`
fragments are body-only.
