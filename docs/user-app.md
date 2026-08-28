# Discord user-app personal chat

The optional user-install surface gives an approved Discord user one personal
conversation with the assistant through `/chat`. It is deliberately disabled
by default and does not change the bot's normal mention/reply behavior.

## What is exposed

When `USER_APP_CHAT_ENABLED=true`, the application registers these commands for
**User Install** only:

- `/chat message:<text> attachment:<optional file> public:<false by default>`
- `/chat-reset`

It also makes the existing self-service `/privacy`, `/memory`, and `/stop`
commands available to both Guild Install and User Install. `/chat` itself is
ID-allowlisted. `/chat-reset`, `/privacy`, `/memory`, and `/stop` remain
caller-scoped so a user can clear or control their data even after access is
removed.

Discord user installs are command-only. Installing the app on a user account
does **not** let it receive arbitrary channel messages, replies, or an ambient
`hey Kimi` trigger in servers where the bot is absent. Use `/chat` there.

## Developer Portal setup

In the Discord Developer Portal for the application:

1. Enable **User Install** as an installation context.
2. Give the User Install flow the `applications.commands` scope.
3. Configure or copy the user-install link and install it on each permitted
   Discord account.
4. Keep the ordinary Guild Install flow for the bot's existing server surface.

The code defaults the command tree to Guild Install only. User-install command
metadata is added explicitly when this feature is enabled, so adding another
guild command later does not accidentally expose it to user accounts.

## Enable and grant access

Configure either `bot/.env` or the ignored operator overlay
`<CONFIG_DIR>/settings.md`:

```dotenv
USER_APP_CHAT_ENABLED=true
USER_APP_MEMBER_IDS=700000000000000201,700000000000000202
USER_APP_REGULAR_IDS=700000000000000203
USER_APP_STAFF_IDS=700000000000000204
USER_APP_CHAT_TIMEOUT_SECONDS=840
```

The ID lists are independent from guild roles and normal guild trust. If an ID
appears in multiple lists, the highest tier wins: Staff, then Regular, then
Member. `OWNER_USER_ID` is automatically Staff and still satisfies owner-only
tool gates. Enabling the feature without an owner or any listed user fails
startup. IDs must be numeric Discord user IDs.

Settings and command registration are read at startup. Restart after enabling,
disabling, or changing membership. Disabling the surface unregisters the
user-install commands but does not silently delete existing personal data; the
user can clear it before disable or use the normal privacy-deletion workflow.

## Conversation, tools, and workspace scope

Each Discord user has exactly one owner-only transcript, keyed internally as
`userchat:<user_id>`. It follows them across servers, channels, group DMs, and
DMs; it is never keyed by channel. Concurrent turns serialize on that root.

The assistant uses the ordinary agent and tool registry rather than a second
chat implementation. Trust and owner-only tool gates still apply. Deployment-
wide tool blocks and tool configuration still apply, while the guild/channel
pins, blocks, model overrides, and instructions of the invocation location do
not leak into the personal thread. Thread-handoff actions are unavailable
because a slash interaction is not a Discord message root.

Personal chat is guild-less for every trust, policy, and data-scope decision,
in both directions. The invocation location grants no standing: a tool scoped
to specific guilds is not dispatchable, and tools whose target is a guild or
deployment artifact are unavailable rather than silently aimed at wherever the
command was typed. Those are community memory (`teach`, `recall_community`,
`reflect_community`) and shared skill management (`skill_create`, `skill_edit`,
`skill_delete`). User-app trust is granted by ID allowlist independently of
guild roles, so letting it reach into a guild's shared state would hand someone
standing in a server they hold no role in. Personal skills and the user's own
long-term memory remain available and are the personal-surface equivalents.

The scopes are intentionally split:

- Transcript, prompt/model routing, usage, and recalled long-term memory are
  personal/global rather than tied to the current guild.
- Workspace files live at `<user_id>__userapp`, shared by that user's `/chat`
  turns and isolated from their guild workspaces.
- Tools read a logical scope that is guild-less here, so anything keyed by
  guild resolves to "no guild" rather than to the invocation location. The
  actual invoking member, guild, and channel travel separately for genuinely
  location-bound work (a member permission check, a jump URL) and confer no
  authority of their own.
- Auto-retained facts from this personal transcript are global to that user.

## Tone and instructions

The tracked default is:

```text
<CONFIG_DIR>/prompts/commands/chat.md
```

It uses a mature personal-chat tone while remaining non-NSFW. To customize it
without creating a Git change in an in-checkout deployment, create:

```text
<CONFIG_DIR>/prompts/commands/chat.local.md
```

`chat.local.md` is gitignored and wins over `chat.md` at runtime. It is read on
each turn, so prompt edits do not require a restart. Deployments with a private
`CONFIG_DIR` may edit their private `chat.md` directly instead. A command prompt
is a complete system-prompt layout, not a fragment; copy every policy, persona,
skills, and tool-routing section the deployment wants to keep.

The tracked chat layout deliberately includes the normal `<persona>` slot. This
keeps the deployment's established voice instead of creating a second persona
stack:

- With no per-user persona, `<persona>` loads `<CONFIG_DIR>/persona.md`, exactly
  as ordinary guild chat does.
- With a compiled per-user persona, that user-selected persona replaces the
  default persona for `/chat` too. Its code-owned boundaries, including its
  rating restrictions, still apply even if `chat.md` is more permissive.
- The chat template adds personal-surface instructions; it does not override or
  weaken persona-level boundaries, system safety, moderation, or tool policy.

Operators who want different wording for the personal surface can remove or
replace `<persona>` in `chat.local.md`. Because command prompts are complete
layouts, doing so affects only `/chat` and does not change the guild persona.

## Visibility, attachments, and limits

Responses are private/ephemeral by default. `public:true` posts only a
successful result publicly; access, consent, validation, moderation, timeout,
provider, and reset messages stay private. Public capability is checked before
the model turn. Mentions are disabled on all result messages.

One optional Discord attachment can accompany the text and travels through the
same bounded image/file import pipeline as ordinary chat. Generated files and
embeds use the existing validation rails. Discord user-installed apps allow at
most five followups, so delivery uses the deferred response plus that bounded
budget; an unusually long response is attached as `response.md` instead of
silently truncating it.

The whole turn is capped by `USER_APP_CHAT_TIMEOUT_SECONDS`, whose maximum is
840 seconds. This leaves cleanup/delivery room under Discord's 15-minute
interaction-token lifetime.

## Consent, stopping, reset, and deletion

If the deployment privacy-consent gate is enabled, an unconsented `/chat`
request receives the same global privacy preference through an ephemeral
Accept/Decline prompt. Accept resumes the retained request and its requested
visibility.

User-installed `/stop` targets `userchat:<user_id>` for `current`, or all work
owned by that user for `all`. An app installed to both the user and the guild
reports both integration owners, so the invoking context is ambiguous; `current`
then cancels both the personal root and the invoking channel's conversation.
Scoping stays limited to the caller's own work in every case. `/chat-reset` first cancels and drains active work,
takes the same root lock as a turn, and then transactionally removes only the
caller's personal transcript and conversation-owned records. It is idempotent.
Reset keeps long-term memory, preferences, and the `__userapp` workspace.

The full `/privacy` deletion remains the complete control: it removes the
owner-only transcript and all of that user's workspaces (including
`__userapp`) along with the other user-scoped stores described in
[Privacy and data lifecycle](privacy.md).
