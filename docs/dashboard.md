# Private Discord dashboard

The optional dashboard is a Discord Embedded Activity with saved, owner-only
conversations. It runs in the existing bot process and uses the same foreground
turn runner as guild chat. It is disabled globally and per server by default.

## What members get

Launch with `/dashboard` or the application's Activity entry point in a server
text channel or thread. Each **New chat** starts a separate conversation, like a
fresh “hey kimi.” The sidebar supports rename, delete, and older history. Reopening
remembers the last selected chat on that device when it still exists.

Choose **Branch from here** on a saved message to explore a separate path with
the conversation copied only through that message. A parent link stays above the
branch, and the parent gets an **Open branch** link. **Bring to parent** copies a
completed response back as user-provided context; it does not start a model turn
or repeat tool actions. Running work, approvals, and tool state are not copied.
Branches keep the same owner, server, and channel access checks. They share the
user's server workspace, so edits to workspace files affect both conversations.
Up to 1,000 saved messages can be copied in one branch; larger histories require
choosing an earlier message.

The composer supports text, attachments, and Stop. Plans appear directly in the
conversation, update as work progresses, and stay with the response in history.
The work panel contains file previews and downloads, the member's server
workspace, coding task progress and input, and scheduled-task Test preview /
Approve / Reject controls.
Approving a scheduled task authorizes its configured server posts through the
existing scheduler. Deleting a chat does not delete its approved schedules.

Chats belong to one Discord user in one server. Other Activity participants
cannot see them, including staff. The originating channel stays with each saved
chat, and current access to that channel is checked before reading or continuing
it. The member and bot must have viewing and history permissions; continuing also
requires permission to post and an open, unlocked thread. Private-thread
membership and fresh parent overwrites are checked. Unsupported launch contexts
are rejected with an explanation.

Model routing, persona, server instructions, trust tiers, consent,
moderation, tool policy, and turn admission use the existing bot services. Files
share the same `(user, server)` workspace as ordinary guild chat. This is separate
from the guild-less `/chat` workspace. Message-bound thread controls are hidden in
the Activity because there is no public Discord message to move or pause.

## Dashboard instructions and tool policy

Dashboard turns use their own full prompt, `config/prompts/commands/dashboard.md`.
It explains the private saved-chat surface, inline plans, file previews, task
approvals, and why Discord thread controls are unavailable. It uses web Markdown,
including tables. The default includes server instructions and intentionally
omits the originating channel's conversational instructions; channel access,
trust, tool restrictions, and moderation still apply.

Copy the complete template to
`<CONFIG_DIR>/prompts/commands/dashboard.local.md` to customize this deployment,
or `prompts/commands/dashboard/<guild_id>.md` for one server. Both override paths
are gitignored in the checkout. The server-specific file wins over `.local.md`,
which wins over `dashboard.md`. External config trees without a dashboard
template use the shipped dashboard default rather than a Discord channel prompt.
Full overrides replace the whole layout, so preserve the required safety and
surface instructions; see [prompt templates](../bot/config/prompts/README.md).

The selected file supports frontmatter just like channel fragments:

```yaml
---
pinned_tools: [extract_document_text]
blocked_tools: [build_discord_embed]
---
```

Use registered tool names (at most 64 per list). Pins add to existing server and
channel pins and remain subject to tool availability and trust checks. Blocks
add to global, server, channel, and fixed dashboard restrictions; pinning a tool
cannot override a block or enable Discord thread controls. Invalid policy reloads
retain the last valid policy, or fail the turn if no valid policy has been loaded.
The body and policy are read on each turn; edits need no restart. Ordinary Discord
chat does not use this dashboard configuration. Model routing can target this
surface through `overrides.commands.dashboard` in `models.yaml`.

## Operator setup

For a development host using a Cloudflare-managed domain, follow the
[Cloudflare Tunnel walkthrough](dashboard-cloudflare.md), including its service
template, portal settings, verification, and stopping procedure.

Use a separate Discord application and isolated instance paths for the first live
smoke test; follow [development](development.md) and
[instance-data guidance](instance-data.md). This feature needs an HTTPS origin
reachable by Discord and the application's OAuth client secret in addition to its
existing bot token. Never put either secret in frontend files or URL mappings.

1. Build the frontend from `bot/dashboard` using Node 22.18 or newer:

   ```bash
   npm ci
   npm run build
   ```

   The bot serves `bot/dashboard/dist` by default. Deployment must build it or
   copy that build to the deployed checkout. The listener refuses to start with
   the feature enabled if the build or client secret is missing. The build
   bundles its own font files under `assets/`, so the page loads nothing from
   third-party hosts and needs no extra Discord URL mapping.

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

3. Set these environment values for the bot instance, then restart:

   ```dotenv
   DASHBOARD_ENABLED=true
   DASHBOARD_CLIENT_SECRET=your-application-oauth-client-secret
   DASHBOARD_HOST=127.0.0.1
   DASHBOARD_PORT=8088
   ```

   Keep the listener on loopback when the proxy is on the same host. Terminate
   TLS at the proxy and forward both ordinary HTTP and WebSocket upgrades. Route
   all dashboard paths unchanged, including `/api/` and `/assets/`. Set a body
   limit compatible with `WORKSPACE_TOOL_MAX_IMPORT_BYTES` and
   `WORKSPACE_TOOL_MAX_FILE_BYTES`; apply a public-edge request limit for login
   attempts. Do not log request bodies, cookies, OAuth codes, or tokens.

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
   ```

   This server flag is read on access. It must be the YAML boolean `true`;
   strings such as `"true"` do not enable it. Existing channel boundaries still
   apply. Global settings are environment-only and require a restart.

5. Let the bot complete normal command sync. When enabled, the same global
   command replacement includes `/dashboard` and one type-4, Discord-handled
   primary entry point. Guild-only sync leaves the global entry point alone.
   When disabled, normal global replacement removes these dashboard commands.
   The compatibility code lives in `KimiCommandTree.sync` because the installed
   discord.py version does not model primary entry-point commands. Recheck it
   when upgrading discord.py against Discord's
   [application-command reference](https://docs.discord.com/developers/interactions/application-commands).

### Restricting who can use the dashboard

Set `DASHBOARD_ALLOWED_USER_IDS` to a comma-separated list of Discord user IDs
to limit testing to selected people. Keep real IDs in the private environment
file. `DASHBOARD_MIN_TIER` sets a minimum server trust tier: `member`, `regular`,
or `staff`.

| Audience | `DASHBOARD_ALLOWED_USER_IDS` | `DASHBOARD_MIN_TIER` |
| --- | --- | --- |
| Selected testers | Their comma-separated user IDs | `member` |
| Regulars and staff | Empty | `regular` |
| Staff | Empty | `staff` |
| All otherwise eligible members | Empty | `member` |

When both restrictions are configured, users must satisfy both. The allowlist
does not grant a trust tier, bypass channel permissions, or override a user
block. Staff and the bot owner do not bypass it. Defaults preserve access for
otherwise eligible members in enabled servers. These settings are
environment-only and require a bot restart, which clears existing dashboard
sessions.

The Activity launcher may remain visible. Unapproved users who open it receive
an access-denied message before a dashboard session is delivered. `/dashboard`
also checks access, and API requests and WebSocket connections use the same
authorization boundary. This application policy applies in addition to
Discord's separate restrictions on launching undistributed Activities.

The remaining environment settings are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DASHBOARD_FRONTEND_DIR` | empty | Optional alternate directory containing the built `index.html` and `assets/`. |
| `DASHBOARD_SESSION_SECONDS` | `3600` | Session lifetime, 300–86400 seconds. Reopen to authenticate again. |
| `DASHBOARD_MAX_SESSIONS` | `2048` | Bounds active sessions and pending login challenges. |
| `DASHBOARD_TURN_TIMEOUT_SECONDS` | `840` | Whole foreground response deadline, 1–3600 seconds. |
| `DASHBOARD_MAX_MESSAGE_CHARS` | `32000` | Maximum input text length, up to 100000. |

## Authentication and private delivery

The frontend waits for Discord's Embedded App SDK, requests the `identify` OAuth
scope, and sends the authorization code to the backend. The backend exchanges the
code, fetches `/users/@me`, and verifies the Activity instance through Discord's
application API. The authenticated user must appear in that instance, which must
belong to this application and a server channel. Client-supplied user, guild,
channel, and trust values do not grant access. This follows Discord's
[Activity lifecycle](https://docs.discord.com/developers/activities/how-activities-work)
and [backend instance verification](https://docs.discord.com/developers/activities/development-guides/multiplayer-experience).

The OAuth token is used for the SDK handshake and is not persisted. The backend
keeps short-lived sessions in memory and uses a host-only Secure, HttpOnly,
SameSite=None, Partitioned cookie. Mutations require the exact Activity origin
and a session CSRF token. WebSockets also verify origin, periodically recheck
instance membership and channel access, and close when access expires. APIs use
relative same-origin URLs through Discord's proxy, following its
[networking guide](https://docs.discord.com/developers/activities/development-guides/networking).
There is no production development-login bypass.

Accepted foreground requests and task actions run independently of their HTTP
request or WebSocket. A persistent event journal supports reconnect and replay.
Closing the Activity does not cancel accepted work. Foreground responses left
unfinished by a bot restart become **interrupted**, with no automatic rerun.
Coding tasks use the existing durable recovery path, with their private delivery
surface persisted in storage. They never fall back to posting results in a
Discord channel if private delivery is unavailable. A coding handoff starts only
after its acknowledgement has been saved; unacknowledged handoffs are abandoned
on recovery.

The UI receives selected public fields, not raw provider responses, tool
arguments, checkpoints, environment variables, or server paths. Chat and coding
results enter the canonical transcript without fabricated Discord message IDs.

## Files and retention

Uploads first go into a private staging area outside the model's workspace.
Accepted attachments pass through the existing image handling, input moderation,
and chat-attachment staging path before tools can use them. The maximum is ten
files per message, with combined bytes bounded by the smaller configured import
and per-file limit. Private dashboard snapshots also have per-user/server byte
and file-count quotas.

Output and workspace previews use immutable private copies with opaque IDs.
Every download rechecks ownership and channel access. Symlinks, hardlinks,
traversal, and files outside the authorized workspace or conversation outputs
are refused. Images are decoded before inline display. Text, Markdown, CSV, and
supported document text are previewed; original files remain downloadable.
HTML and SVG are never executed in the preview. Remote Markdown images appear
as links, so merely opening a response does not fetch them.
Both relative and absolute `WORKSPACE_DIR` paths are supported. If an output cannot
be copied for delivery, its card keeps the filename and says **File unavailable**;
this does not mean that the file's retention period has elapsed.

Idle chats use `TRANSCRIPT_RETENTION_DAYS` (30 by default). File copies follow the
existing workspace file expiry and privacy cleanup. An old message can therefore
outlive its attachment; downloads then report that the file expired. Deleting a
chat stops its active work, removes its private snapshots and transcript, and
keeps the shared server workspace. Branches and returned results keep their own
copies of available attachments, subject to the same file quotas and retention.
Deleting the source chat does not delete those copies. Already expired files
remain marked expired. Full privacy deletion revokes sessions and
removes owned transcripts, task records, and generated jobs through the existing
deletion pipeline. Operators retain the access described in the
[privacy policy](privacy-policy.md).

## Verification and current limits

Local checks cover authentication and ownership boundaries, current permission
checks, replay and revocation, file isolation, shared turn orchestration,
moderation, cancellation, private durable coding delivery, and scheduled approval
behavior. Frontend tests cover request retries and rendering; Playwright exercises
saved chats, task review, previews, and navigation at desktop and phone sizes.

From `bot/dashboard`:

```bash
npm test
npx playwright install chromium
npm run test:browser
```

The browser test fixture is only a Vite development page. It is excluded from the
production build. It simulates the API and does not prove a real Discord launch.
Before rollout, test with the isolated application in Discord desktop, iOS, and
Android: launch and consent; upload and download; approve/reject/test a draft;
answer and stop coding work; close and reopen during work; restart the bot; revoke
channel access; delete a chat and then perform full privacy deletion. Real mobile
download behavior, SDK authentication, proxy cookies, and Activity lifecycle
still require that live test.
