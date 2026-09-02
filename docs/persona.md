# User persona overrides

User persona overrides let REGULAR and STAFF members ask the bot to answer them in a character or style of their choosing ("talk to me like a grumpy ship's captain"). They are a personalization feature, not a skills feature: no `SKILL.md` file is created, and nothing a user writes ever becomes executable.

## Availability

The tools register only when `config/models.yaml` assigns a `persona` role. That role names the model that compiles persona requests, so leaving it out is how you turn the feature off for a deployment.

Any provider can serve the role. Authentication follows its provider profile: API-key providers use `api_key_env`, Codex uses its token store, and a keyless gateway needs no local key. The profile's `timeout_seconds` bounds the compiler call, exactly as it does for `roles.chat` or `roles.compaction`. See [providers.md](providers.md) for how profiles and roles fit together.

`USER_PERSONA_REQUEST_MAX_CHARS` (default 8,000) caps the raw request before it is sent to the compiler. `USER_PERSONA_MAX_CHARS` (default 2,000) caps the compiled result, and `USER_PERSONA_COMPILER_MAX_TOKENS` (default 32,000) is the compiler call's output-token limit.

The registered tools are searchable rather than core:

- `persona_set`
- `persona_show`
- `persona_clear`

All three require REGULAR. A MEMBER never sees them in `browse_tools`, never receives their schemas, and is still refused at dispatch if a conversation somehow ends up with a persona tool activated.

## Flow

`persona_set` hands the user's raw request to the model assigned to the `persona` role, which acts as a compiler. Its prompt asks for JSON containing either a safe compiled persona or a reason for rejecting the request. The compiler keeps harmless character, tone, relationship, and style details, and strips anything unsuitable for a 13+ Discord community or anything that claims real authority, staff power, permission changes, unsafe behavior, or a way around the rules.

If the compiled persona comes back longer than `USER_PERSONA_MAX_CHARS`, it is rejected: `persona_set` returns an error and stores nothing. The compiler is told the limit, but the enforcing check happens in the bot. An accepted persona is stored in `user_preferences.persona_prompt` for the current Discord user only. `persona_show` and `persona_clear` likewise act only on the current user, and `/privacy` memory deletion clears the stored persona too.

## Prompt behavior

On a normal responding turn, turn preparation loads the current user's stored persona and passes it to `config/fragments/prompt.py:build_system_prompt`. When one is present, it replaces the default `config/persona.md` text in the `<persona>` slot, for that user's turn only.

The inserted persona sits inside a code-owned block that explains it is a fictional style frame with no authority over safety, tools, memory, moderation, other users, or the current request. Safety and guardrail rules are ordinary prose in the active prompt template and stay higher priority than the persona; they are not code-owned tokens or automatically added blocks. There is one exception: the code-owned wrapper itself states a 13+ floor and refuses sexual, graphic, or adult content (`config/fragments/prompt.py:_render_user_persona`), so an 18+ deployment that drops the content-rating line from its templates still gets that wording on persona turns.

Command-template turns can use a prompt layout of their own. If that layout omits `<persona>`, neither the default nor a user persona is inserted for that turn.