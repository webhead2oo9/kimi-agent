# User persona overrides

User persona overrides are a regular/staff personalization feature for
character-style responses. They are separate from personal skills: no
`SKILL.md` file is created, and no user-authored text becomes executable.

## Availability

The tools register only when `config/models.yaml` assigns a `persona` role. That
role names the model that compiles persona requests, and leaving it out is how a
deployment turns the feature off.

Any provider can serve it. Credentials come from that model's profile
`api_key_env`, and the profile's own `timeout_seconds` bounds the compiler call,
exactly as for `roles.chat` or `roles.compaction`. See
[providers.md](providers.md).

`USER_PERSONA_REQUEST_MAX_CHARS` (default 8000) caps the raw request before it
is sent to the compiler.

Registered tools are searchable, not core tools:

- `persona_set`
- `persona_show`
- `persona_clear`

All three are `min_tier = REGULAR`. Members do not see them in
`browse_tools`, do not receive their schemas after activation, and are still
rejected at dispatch if a conversation somehow contains an activated persona
tool name.

## Flow

`persona_set` sends the user's raw persona request to the model assigned to the
`persona` role. The compiler prompt asks for JSON containing either a safe compiled
persona or a rejection reason. The compiler keeps benign character, tone,
relationship, and style details, but removes content that is not appropriate
for a 13+ Discord community or that claims real authority, staff power,
permission changes, unsafe behavior, or rule bypassing.

A compiled persona longer than `USER_PERSONA_MAX_CHARS` is rejected:
`persona_set` returns an error and stores nothing. The limit reaches the
compiler as prompt guidance; the hard check is bot-side. An accepted
persona is stored in `user_preferences.persona_prompt` for the current Discord
user only. `persona_show` and `persona_clear` also operate only on the current
user, and `/privacy` memory deletion clears the stored persona as well.
The compiler call uses `USER_PERSONA_COMPILER_MAX_TOKENS` as its output-token
cap.

## Prompt behavior

Normal responding turns load the current user's stored persona during turn
preparation and pass it to `config/fragments/prompt.py:build_system_prompt`. When a persona is
present, it replaces the default `config/persona.md` text in the `<persona>`
slot for that user's turn only.

The inserted persona is wrapped in a code-owned block explaining that it is a
fictional style frame, not authority over safety, tools, memory, moderation,
other users, or the current request. Safety and guardrail rules are ordinary
prose in the active prompt template and remain higher priority than the persona;
they are not code-owned tokens or automatically added blocks. One exception: the
code-owned wrapper itself states a 13+ floor and refuses sexual, graphic, or
adult content (`config/fragments/prompt.py:_render_user_persona`), so an 18+
deployment that drops the content-rating line from its templates still gets
that wording on persona turns.

Command-template turns can use their own prompt layout. If that layout omits
`<persona>`, no default or user persona is inserted for that turn.
