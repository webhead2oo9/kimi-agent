# Private Discord dashboard

The optional dashboard is a Discord Embedded Activity with saved conversations
private to the member who started them. This guide is for the operator enabling
and supporting it. The dashboard runs in the existing bot process and is disabled
globally and per server by default.

Members use it for private chats, files, coding work, and scheduled-task review.
It has no staff view of other members' chats or dashboard controls for changing
bot configuration. Normal host-operator access still applies; see the
[privacy policy](privacy-policy.md).

## Operator setup

For a development host using a Cloudflare-managed domain, follow the
[Cloudflare Tunnel walkthrough](dashboard-cloudflare.md), including its service
template, portal settings, verification, and stopping procedure.

Use a separate Discord application and isolated instance paths for the first live
smoke test; follow [development](development.md) and
[instance-data guidance](instance-data.md). This feature needs an HTTPS origin
reachable by Discord and the application's OAuth client secret in addition to its
existing bot token. Add it to that instance's existing service. Never put either
secret in frontend files or URL mappings.

1. Build the frontend from `bot/dashboard` using Node 22.18.0, the version used
   in CI, or a compatible newer version:

   ```bash
   npm ci
   npm run build
   ```

   The bot serves `bot/dashboard/dist` by default. Deployment must build it or
   copy that build to the deployed checkout. Rebuild after frontend changes;
   pulling source updates alone does not refresh `dist`. A missing build or empty
   client secret prevents the bot from completing startup when the dashboard is
   enabled. A nonempty but incorrect secret instead fails at sign-in.
   The built files are publicly served and must contain no secrets. Fonts are
   bundled under `assets/`; avatars are supplied by the bot, so neither needs an
   extra Discord URL mapping.

2. In the Discord Developer Portal for that same application, enable Activities
   and configure a root URL mapping (`/`) to your HTTPS reverse-proxy hostname.
   Ensure Guild Install is supported. Add an OAuth2 Redirect URI (Discord
   documents `https://127.0.0.1` as a placeholder for SDK-only authorization;
   the SDK handles the redirect). Enable Developer Mode for the test account;
   undistributed Activities are limited to the application owner/developer team.
   Enable the Web, iOS, and Android platforms you intend to support. Discord's
   [Activity setup](https://docs.discord.com/developers/activities/building-an-activity)
   and [mobile guidance](https://docs.discord.com/developers/activities/development-guides/mobile)
   describe the portal controls and testing requirements.

3. Set these values in the bot instance's private environment file. Restart after
   completing the proxy and server configuration below:

   ```dotenv
   DASHBOARD_ENABLED=true
   DASHBOARD_CLIENT_SECRET=your-application-oauth-client-secret
   DASHBOARD_HOST=127.0.0.1
   DASHBOARD_PORT=8088
   ```

   Keep the listener on loopback when the proxy is on the same host. Terminate
   TLS at the proxy and forward both ordinary HTTP and WebSocket upgrades. Route
   all dashboard paths unchanged, including `/api/` and `/assets/`. Set a body
   limit large enough for the [configured upload limit](#files-and-retention).
   Preserve cookies and the `Origin` header, respect `Cache-Control: no-store`,
   and apply a public-edge request limit for login attempts. Keep this route free
   of interactive proxy logins or browser challenges that prevent Discord from
   reaching it. Do not log request bodies, cookies, OAuth codes, or tokens.

   For Caddy on the same host, the essential proxy configuration is:

   ```caddyfile
   dashboard.example.com {
       reverse_proxy 127.0.0.1:8088
   }
   ```

4. In `<CONFIG_DIR>/servers/<guild_id>.md`, add the following to the existing
   frontmatter. The server must also be active through normal bot setup:

   ```yaml
   dashboard:
     enabled: true
     # Optional hot-read guild-specific launch roles:
     allowed_role_ids: [123456789012345678]
   ```

   Activate the server through validated `bot_active: true` frontmatter or the
   normal `ALLOWED_GUILD_IDS` setting; an explicit `bot_active: false` disables it.
   The dashboard flag is read on access and must be the YAML boolean `true`;
   strings such as `"true"` do not enable it. Existing channel boundaries still
   apply. All `DASHBOARD_*` settings are environment-only, require a restart,
   and cannot be set in `settings.md`.

5. Before upgrading an existing instance to dashboard database changes, take a
   consistent [database backup](database.md#backing-up-the-database). Startup
   applies migrations automatically. Restart the bot and wait for the dashboard
   listener and normal global command sync. The commands should include
   `/dashboard` and the application's **Launch** entry point. A guild-only sync
   does not update the global launcher. Complete the
   [live verification](#verification-and-current-limits) before widening access.

### Restricting who can use the dashboard

Set `DASHBOARD_ALLOWED_USER_IDS` to a comma-separated list of Discord user IDs
to limit testing to selected people. Keep real IDs in the private environment
file. To admit guild-specific Discord roles without changing their trust tier,
set `dashboard.allowed_role_ids` in that guild's server fragment. This list is
hot-read on every access check, accepts at most 100 unique numeric role IDs, and
fails closed when malformed. `DASHBOARD_MIN_TIER` independently sets a minimum
server trust tier: `member`, `regular`, or `staff`.

| Audience | `DASHBOARD_ALLOWED_USER_IDS` | `DASHBOARD_MIN_TIER` |
| --- | --- | --- |
| Selected testers | Their comma-separated user IDs | `member` |
| Regulars and staff | Empty | `regular` |
| Staff | Empty | `staff` |
| All otherwise eligible members | Empty | `member` |

When a guild role allowlist is present, a member must be in that guild's role
list or the global invited-user list. The resulting member must still satisfy
the minimum tier. Neither allowlist grants a trust tier, bypasses channel
permissions, or overrides a user block. Staff and the bot owner do not bypass
configured admission lists. With no user or guild-role list, defaults preserve
access for otherwise eligible members in enabled servers. Environment settings
require a bot restart, which clears existing dashboard sessions; server-fragment
role edits do not.

The Activity launcher may remain visible. Unapproved users who open it receive
an access-denied message before a dashboard session is delivered. `/dashboard`
also checks access, and API requests and WebSocket connections use the same
authorization boundary. This application policy applies in addition to
Discord's separate restrictions on launching undistributed Activities.

The remaining environment settings are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DASHBOARD_FRONTEND_DIR` | empty | Optional alternate directory containing the built `index.html` and `assets/`. Relative paths use the bot's working directory. |
| `DASHBOARD_SESSION_SECONDS` | `3600` | Session lifetime, 300–86400 seconds. Reopen to authenticate again. |
| `DASHBOARD_MAX_SESSIONS` | `2048` | Separate caps on active sessions and pending login challenges, 1–100000 each. |
| `DASHBOARD_TURN_TIMEOUT_SECONDS` | `840` | Whole foreground response deadline, 1–3600 seconds. |
| `DASHBOARD_MAX_MESSAGE_CHARS` | `32000` | Maximum input text length, 1–100000 characters. |

### Channel access and privacy

Launch with `/dashboard` or the application's Activity entry point in a server
text channel or thread. DMs, group DMs, voice channels, and forum parent channels
are unsupported; a thread under a forum can be used.

Chats belong to one Discord user in one server. Other Activity participants,
including staff, cannot read them through the dashboard. Discord can still show
that the Activity was launched: its Discord-handled **Launch** entry point posts
a launch message in the channel. The conversation itself stays in the dashboard.
See Discord's [entry-point behavior](https://docs.discord.com/developers/activities/building-an-activity#default-entry-point-command).

The member and bot must both have **View Channel** and **Read Message History**
in the launch channel and in a saved chat's original channel. New chats, replies,
branches, and task actions also require permission to send messages; threads must
be open and unlocked. Private-thread membership is required for each unless that
member or bot has **Manage Threads**. Current thread-parent permissions and the
bot's configured channel boundaries also apply.

Opening from another channel in the same server still lists the member's saved
chats, but does not change their original channel or its access requirements.
Titles can remain in the sidebar after access to an original channel is lost.
The member can delete such a chat by reopening from another eligible channel;
reading its history requires restoring the original channel access.

Model routing, persona, server instructions, trust tiers, consent, moderation,
tool restrictions, and capacity limits use the existing bot configuration.
Enabling the dashboard does not enable [coding tasks](coding-agent.md) or
[scheduled tasks](scheduled-tasks.md); configure those features separately.

## What members get

The launch screen shows the configured `BOT_NAME` and the bot's Discord avatar
once the bot identity loads. Conversation history shows placeholders while
loading. Messages use the bot's and member's Discord avatars, with an initial
when an image is unavailable.

| Control | Behavior |
| --- | --- |
| **New chat** | Starts separate conversation history. The member's server workspace and existing memory settings still apply. |
| Sidebar | Opens, renames, or deletes saved chats and loads older chats. Reopening remembers the last selected chat on that device when available. |
| Composer | Sends text and attachments. **Stop response** requests cancellation; completed file edits are kept. |
| Plans and copying | Plans update in the conversation and stay in history. **Copy response** copies Markdown; **Copy code** preserves code whitespace. A manual-copy dialog is available when clipboard access fails. |
| Work panel | Shows file previews, **View original** downloads, **Server workspace**, coding progress and input, and scheduled-task review. |

**Branch from here** copies saved conversation history through the selected
message into a new chat. Parent and **Open branch** links connect the two chats,
and dividers mark copied history. **Bring to parent** adds a completed response
back as user-provided context, without starting a model response or repeating
tool actions. Wait for the parent's current response to finish before bringing
a result back. The branch keeps a **Brought to parent** marker.

Branches keep the same owner, server, and originating channel. Running work,
approvals, and tool state are not copied. Up to 1,000 saved messages can be copied
in one branch; choose an earlier message if the history exceeds that limit.
All of the member's chats in that server share a workspace with ordinary guild
chat, so edits affect files used by other chats and branches. The guild-less
`/chat` workspace is separate. Discord thread creation, leaving, and reply-pause
controls are unavailable in the dashboard.

For scheduled drafts, review the destination channels, schedule, instructions,
and any Python source in the Work panel. **Test preview** runs a preview;
**Approve** authorizes that exact revision's configured server posts, and
**Reject** rejects it. Approved schedules survive chat deletion and must be
managed through the [scheduled-task controls](scheduled-tasks.md).

## Dashboard instructions and tool policy

Dashboard chats use the full prompt
[`config/prompts/commands/dashboard.md`](../bot/config/prompts/commands/dashboard.md).
The shipped template includes server instructions and omits the originating
channel's conversational instructions. Channel access, trust, tool restrictions,
and moderation still apply. Put instructions needed by dashboard chats in the
server fragment or a dashboard prompt override.

To customize it, copy the complete template to one of these paths under
`CONFIG_DIR`, in order of precedence:

1. `prompts/commands/dashboard/<guild_id>.md` for one server.
2. `prompts/commands/dashboard.local.md` for the deployment.
3. `prompts/commands/dashboard.md` as the shared template.

The first two override paths are gitignored in the checkout. An external config
tree without a dashboard template falls back to the shipped dashboard prompt.
Overrides replace the complete prompt, so preserve its safety instructions,
private-chat guidance, and required placeholders; see
[prompt templates](../bot/config/prompts/README.md).

The selected file also supports tool policy in its YAML frontmatter:

```yaml
---
pinned_tools: [extract_document_text]
blocked_tools: [build_discord_embed]
---
```

Use registered tool names, at most 64 per list. Pins add to server and channel
pins; blocks add to existing restrictions. A pin cannot override a block, trust
requirement, or unavailable tool. The prompt and policy are read on each turn,
so edits need no restart. Invalid policy reloads retain that file's last valid
policy, or fail the turn if none has loaded; check the bot log if a policy edit
appears ineffective. These overrides apply to dashboard turns and the work panel’s
direct coding steer/cancel controls.
Model routing can target `overrides.commands.dashboard` in `models.yaml`.

## Authentication and private delivery

Members sign in inside Discord. The dashboard requests the `identify` OAuth
scope, verifies the signed-in user's membership in the Activity with Discord,
and then applies the server, user, and channel access rules above. Opening the
public hostname in an ordinary browser cannot sign a member in.

Login sessions live in the bot process's memory and expire after
`DASHBOARD_SESSION_SECONDS`; restarting the bot clears them. Keep the dashboard
route pointed at that bot process. The OAuth token is used for Discord sign-in
and is not persisted by the dashboard.

The proxy must preserve the host-only session cookie's `Secure`, `HttpOnly`,
`SameSite=None`, and `Partitioned` attributes. Requests that change data and
WebSocket connections must have the Activity origin
`https://<application_id>.discordsays.com`. The frontend supplies the required
session CSRF token automatically. See Discord's
[networking guide](https://docs.discord.com/developers/activities/development-guides/networking).

During an open chat, the connection rechecks Activity membership and channel
access about every 15 seconds. Temporary verification failures reconnect with
increasing delays and resume saved updates. Expired sessions and denied access
require reopening; reopening does not restore a removed permission. Initial
sign-in failures show an error with reopening instructions. A fourth open chat connection
disables mutations and asks you to close another tab, then close and reopen this
Activity. Session expiry and launch failures also require closing and reopening
from Discord. Reloading the iframe cannot reliably repeat Discord’s single-use
ready handshake. Unsent drafts stay visible until the Activity is closed.

The application limits unauthenticated bootstrap and authentication requests
jointly to 60 per minute per transport peer and 240 per minute overall, before
allocating challenges or contacting Discord. It never trusts forwarded IP
headers for this limit. A local tunnel shares a peer bucket across its users;
public-edge limits remain useful defense in depth. Throttled HTTP responses
include `Retry-After: 60`. Deleting one member's data revokes only their sessions
and earlier login attempts; other members' sign-ins remain valid.

Closing the Activity or losing its connection does not cancel accepted responses
or task actions. Use **Stop response** or the coding card's **Stop task** to stop
work; neither undoes completed file changes. After a bot restart, reopen the
Activity and inspect the chat before sending anything again. Foreground responses
and task actions still recorded as unfinished are marked interrupted and are
not automatically rerun. Task controls stay pending through HTTP acceptance
until the matching saved action result arrives. **Retry action** resends the same
request identifier when a network failure leaves acceptance uncertain.

Assistant transcript rows and completed dashboard results commit together.
Coding handoff release shares the acknowledgement transaction, and the coding
worker polls for released tasks even if a notification is lost. Background coding
results also commit their transcript, visible event, and delivery receipt together.

Coding tasks follow their separate [restart recovery](coding-agent.md) rules.
An acknowledged coding handoff can recover with delivery still directed to its
private chat. If private delivery is unavailable, the task does not fall back to
posting in a Discord channel. A handoff whose acknowledgement was not saved is
abandoned during recovery.

## Files and retention

Chat deletion first renames private snapshots into a quarantine. A failed database
deletion restores them; startup restores quarantines for surviving chats and
finishes removal for deleted chats. Branch file copying runs outside the database
write transaction, then revalidates its saved source before publishing context
and file metadata together.

Foreground and coding output bytes are captured under the source workspace lease.
Private copies are then staged under the file quota and maintenance lease, with
metadata committed in the same transaction as the result and transcript. Failed
publication removes staged copies and leaves no quota records; coding delivery
can retry after restart without duplicating published snapshots. A process exit
before commit can leave inaccessible generated files until normal file expiry,
but no durable dashboard quota rows.

Uploads are staged privately before the message is sent. They pass through the
existing attachment handling and input moderation before tools can use them.
Dashboard file limits come from workspace settings:

| Limit | Setting and default |
| --- | --- |
| Attachments per message | At most 10 different files. |
| Combined attachment bytes per message | Smaller of `WORKSPACE_TOOL_MAX_IMPORT_BYTES` (25 MiB) and `WORKSPACE_TOOL_MAX_FILE_BYTES` (50 MiB): 25 MiB by default. Each upload must also fit this limit. |
| Output or workspace copy | `WORKSPACE_TOOL_MAX_FILE_BYTES`: 50 MiB per file. |
| Private snapshot quota per member per server | `WORKSPACE_TOOL_MAX_USER_BYTES`: 150 MiB, plus a fixed cap of 1,000 file records within the file retention window. Includes uploads and copies made by branches and previews. |

These snapshot checks are separate from the shared workspace's admission quota.
The ordinary workspace sweeper can also remove files to enforce
`WORKSPACE_MAX_SIZE_MB` (150 MiB by default).

Output and workspace previews use immutable private copies with opaque IDs.
Every download rechecks ownership and channel access. Images are decoded before
inline display. Text, Markdown, CSV, and supported document text are previewed;
original files remain downloadable.
HTML and SVG are never executed in the preview. Remote Markdown images appear
as links, so merely opening a response does not fetch them.
Unsafe file paths, symlinks, hardlinks, and files outside the authorized workspace
or conversation outputs are refused. **File unavailable** means an output could
not be copied for delivery. A later download can report that a file expired or is
no longer available; that message alone does not establish why it disappeared.

Idle chats use `TRANSCRIPT_RETENTION_DAYS` (30 days by default; 0 disables automatic
transcript expiry). Snapshots use `WORKSPACE_FILE_TTL` (604800 seconds, or seven
days, by default) and expire independently of both the chat and the original
workspace file. Reading a snapshot does not refresh its file modification time.
Cleanup runs periodically, and size cleanup can remove files sooner. A saved
message can therefore outlive its attachment.

| Action | Result |
| --- | --- |
| Delete a chat | Stops its active work, then removes its transcript and private snapshots. Shared workspace files and approved schedules remain. If work is still stopping, retry deletion shortly. |
| Delete a parent or branch | Other chats and responses already brought into them remain, including their own copies of available attachments. The surviving branch loses its link to a deleted parent. |
| Full privacy deletion | Revokes dashboard sessions and uses the existing transcript, workspace, memory, and task deletion pipeline. Choose **Delete my data** under `/privacy` in Discord; see [privacy controls](privacy.md). |

Chat deletion does not clear separately retained long-term memory. Disabling the
dashboard also keeps saved data; normal retention still applies while the bot runs.

## Updates and disabling access

For upgrades, back up the database before migrations, rebuild the frontend from
the deployed source version, and restart the bot. Members must reopen afterward.
If rolling back code across a schema change, follow the
[database restore guidance](database.md#backing-up-the-database) and restore a
compatible backup when required.

To disable access for one server, change its frontmatter to
`dashboard: {enabled: false}`. New access checks reject it without a restart;
open connections detect the change on their next check. To stop the listener and
remove the dashboard commands on normal global command sync, set
`DASHBOARD_ENABLED=false` and restart the bot.

Removing access or stopping a proxy is not a work-cancellation control. Stop
active work first if that is the intent. Approved schedules are managed
separately through the scheduler.

## Verification and current limits

After startup, check the following through both the local listener and the public
route. The [Cloudflare walkthrough](dashboard-cloudflare.md#7-configure-discord-and-launch)
provides commands for its deployment:

| Check | Expected result |
| --- | --- |
| `/` and built `/assets/` files | HTTP 200. This proves static serving only. |
| `/api/bootstrap` | HTTP 200 after bot initialization; issues a login challenge. Inspect status without sharing its body or cookies. |
| `/api/session` without a login cookie | HTTP 401 after initialization. |
| Real Discord launch | Sign-in succeeds, eligible server chats load, and a new message receives a response. |

Before rollout, use the isolated test application and test accounts on Discord
desktop and each mobile platform you intend to support:

1. Launch, complete consent when enabled, and exchange a message. Confirm an
   excluded tester is denied and a second eligible member cannot see the first
   member's chats.
2. Upload, preview, download, and copy a response. Branch a chat and bring a
   completed result to its parent.
3. With the relevant features enabled, test, reject, and approve disposable
   schedules whose destinations you control; answer and stop coding work.
4. Close and reopen during work, then test a bot restart. Confirm saved history
   remains and unfinished foreground work is not silently repeated.
5. Revoke original-channel access and confirm history and downloads are denied.
   Restore access, reopen, and verify the chat is usable again.
6. Delete a disposable chat and check that its branches, shared workspace, and
   approved schedules remain. Use a disposable account for full privacy deletion.

[Developer checks](development.md#discord-dashboard-frontend) use simulated APIs
and do not prove a real Discord launch. SDK authentication, proxy cookies, mobile
downloads, and Activity lifecycle behavior require the live checks above.

## Troubleshooting

| Symptom | Operator action |
| --- | --- |
| Bot fails to start after enabling the dashboard | Check the private log for an empty client secret, missing frontend build, or occupied listener port. |
| Page loads but API returns 503 | Wait for bot initialization or Discord reconnection; inspect the service log if it persists. |
| Page opens in a browser but cannot sign in | Launch inside Discord using `/dashboard` or the App Launcher. |
| Activity is missing | Check the Portal Activities setting, supported platform, developer-team access for an undistributed app, Developer Mode, and global command sync. |
| Sign-in fails inside Discord | Check that the OAuth secret and bot token belong to the same app, the redirect and root mapping are set, and the proxy preserves cookies and origin. Retry after fixing the cause. |
| Access denied | Check active server status, both enable switches, user blocks, allowlist, minimum tier, and member/bot channel permissions. Reopening alone cannot override them. |
| Saved title is visible but history is denied | Check that chat's original channel as well as the launch channel. |
| History opens but sending or task actions fail | Check send permissions, archived/locked threads, consent, and the relevant tool configuration. |
| Updates keep reconnecting | Check WebSocket forwarding and Discord connectivity. Close extra dashboard tabs; the server permits at most three update connections per user. |
| HTTP 429 | Wait and retry. Check for excess tabs or repeated requests; authenticated API traffic is capped at 120 requests per user per minute, with separate work-capacity and login-session limits. |
| Upload rejected or file quota reached | Check the limits above and the proxy's body limit. Delete unneeded chats to remove their snapshots or wait for file expiry. |
| Attachment unavailable | Check retention, size cleanup, and file permissions. If the original still exists, open it from **Server workspace** to make a fresh preview copy. |
