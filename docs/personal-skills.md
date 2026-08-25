# Personal skills

Personal skills are durable, per-user instruction documents. They let a member
save a reusable procedure or preference that the bot can see on that member's
future turns.

Character/persona overrides are a separate feature. They are managed by the
regular+ persona tools and stored in SQLite; no `SKILL.md` document is
involved.

## Storage

Personal skills live under:

```text
data/personal_skills/<user_id>/<skill-name>/SKILL.md
```

`<user_id>` must be a numeric Discord snowflake. Read paths do not create user
directories; the first successful create lazily creates the user's root.

The directory is configured by `PERSONAL_SKILLS_DIR` and sits outside
`WORKSPACE_DIR`. Workspace TTL/quota sweeping does not touch personal skills.

## Tools

Personal skill tools are member-tier and always derive ownership from
`MessageContext.user_id`; they do not accept a target user id.

- `my_skill_get` is a core member tool. Use it to load the full markdown for a
  personal skill listed in the `## Your Personal Skills` prompt section.
- `my_skill_create`, `my_skill_edit`, and `my_skill_delete` are hidden/searchable
  tools. The model must load them through `browse_tools` when the current user
  explicitly asks to manage personal skills. `my_skill_edit` is whole-content
  replacement only; it has none of the `edits`/`append` modes of the shared
  `skill_edit`.

Personal skills are retained until the user removes them with `my_skill_delete`.
`/privacy` **Delete my data** covers transcripts, workspaces, and memory but
does not touch this directory (see [privacy.md](privacy.md)).

Staff-owned global skill tools are separate: `skill_create`, `skill_edit`, and
`skill_delete` still operate only on the shared skills store and remain
staff-gated. Discord-created shared skills belong exclusively to their creating
guild; global and multi-guild shared skills are read-only from Discord and stay
under operator control. Executable skill tools are authored on disk, not through
Discord.

## Prompt

Normal responding turns render the current user's personal index as:

```text
## Your Personal Skills
<code-owned preamble explaining how to load one>
- **skill-name**: description [tag1, tag2]
```

The preamble line is fixed prose owned by `config/fragments/prompt.py`; the tag
suffix appears only when a skill declares tags. The section is omitted when the
user has no personal skills.

## No executable tools

Personal skills are instruction-only. The personal store is never passed to
`register_all_skill_tools` or `reload_all_skill_tools`, so a hand-written
`tools:` frontmatter block under `data/personal_skills/` is inert. The supported
create/edit paths also reject embedded executable-tool metadata in the markdown
body by reusing the global skill manager's content validation.
