# Develop the Discord dashboard through Cloudflare Tunnel

This guide adds an HTTPS endpoint to a Linux development bot using a
dashboard-managed Cloudflare Tunnel and a domain in your Cloudflare account.
The tunnel runs on the same host as Kimi and forwards to `127.0.0.1:8088`.
Cloudflare handles public HTTPS; the bot's HTTP port stays on loopback.

Follow [development setup](development.md) first if a development bot does not
already exist. Use a separate Discord application and separate instance data
from production. If the host already runs your development bot, add the
dashboard to that service instead of launching another process with its token.
The [dashboard reference](dashboard.md) covers authentication, per-server
access, and the full live test procedure.

Commands below run as the unprivileged bot account unless marked with `sudo`.
Replace example paths, service names, server IDs, and hostnames with your own.
Keep the concrete deployment configuration, tokens, and test results outside
the public checkout; this guide contains only reusable instructions.

## 1. Record the existing instance

Before changing a service, identify its working directory and environment files:

```bash
systemctl --user show kimi-agent.service \
  -p ActiveState -p WorkingDirectory -p ExecStart -p EnvironmentFiles
python3 --version
node --version
npm --version
ss -ltn
free -h
df -h /
```

Python must be 3.14 or newer and the dashboard requires Node 22.18 or newer.
Check that port 8088 is unused. Inspect the selected dotenv and runtime files
locally without printing credentials into shared logs. `ENV_FILE` selects the
dotenv file, defaulting to `.env` relative to the bot's working directory.
Values injected by systemd's `EnvironmentFile` override dotenv values.

Record `CONFIG_DIR`, the service name, and the existing state paths in private
deployment notes. Preserve existing model routing and module settings when
adding the dashboard to an established development instance.

## 2. Build and check the frontend

From the checkout's `bot/dashboard` directory:

```bash
npm ci
npm run build
npm test
```

The build creates `dist/index.html` and `dist/assets/`. Kimi serves these files
from the bot process. Repeat the build after frontend changes; an ordinary
`git pull` does not create or refresh `dist`.

On a small development host, limit the Node heap if needed:

```bash
NODE_OPTIONS=--max-old-space-size=512 npm run build
```

This build and the mocked frontend tests do not prove Discord authentication.
The live launch check remains necessary.

## 3. Prepare the dashboard settings

Use a private environment file dedicated to the dashboard. This keeps the
existing bot token, models, and module settings in their established files.
Create `~/.config/kimi-agent/dashboard.env` as the bot account, with mode 600:

```bash
install -d -m 700 "$HOME/.config/kimi-agent"
touch "$HOME/.config/kimi-agent/dashboard.env"
chmod 600 "$HOME/.config/kimi-agent/dashboard.env"
```

Edit that file to contain:

```dotenv
DASHBOARD_ENABLED=true
DASHBOARD_ALLOWED_USER_IDS=
DASHBOARD_MIN_TIER=member
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8088
DASHBOARD_CLIENT_SECRET=replace-with-this-bot-applications-oauth-client-secret
```

Enter the client secret through an editor on the host. It belongs to the same
Discord application as `DISCORD_BOT_TOKEN`. Keep the file readable only by the
bot account. While waiting for the secret, leave its value empty and use
`DASHBOARD_ENABLED=false`. Do not restart with the feature enabled and a
placeholder: startup requires both a client secret and a frontend build.

For a limited test, fill `DASHBOARD_ALLOWED_USER_IDS` with the selected testers'
comma-separated Discord user IDs in this private file before enabling the
dashboard. Leave `DASHBOARD_MIN_TIER=member` to admit those testers regardless
of their server trust tier. To later admit regulars and staff, clear the ID list
and set `DASHBOARD_MIN_TIER=regular`. An empty list with `member` admits all
otherwise eligible members. Both settings require a restart; neither overrides
server activation, channel access, or user blocks. See
[dashboard access policy](dashboard.md#restricting-who-can-use-the-dashboard).

Create the service drop-in directory:

```bash
install -d -m 700 "$HOME/.config/systemd/user/kimi-agent.service.d"
```

Create `~/.config/systemd/user/kimi-agent.service.d/dashboard.conf` with:

```ini
[Service]
EnvironmentFile=%h/.config/kimi-agent/dashboard.env
```

This adds an environment file to the existing service without replacing its
unit or other environment files. Use the actual unit name if yours differs.
Reload systemd after installing the drop-in:

```bash
systemctl --user daemon-reload
```

Dashboard environment settings require a bot restart and are not supported in
`settings.md`. A manually launched bot must receive the same dashboard settings
in its selected dotenv or shell environment; the service drop-in applies only
to systemd launches.

In `<CONFIG_DIR>/servers/<test_guild_id>.md`, merge these keys into the existing
YAML frontmatter, preserving other settings and the Markdown instructions:

```yaml
---
bot_active: true
dashboard:
  enabled: true
---
```

Enable only the servers selected for dashboard testing. The per-server flag is
read on access and must be the boolean `true`, not the string `"true"`.

## 4. Install the tunnel connector

On Ubuntu/Debian, use Cloudflare's
[signed package repository](https://pkg.cloudflare.com/index.html):

```bash
sudo install -d -m 755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  -o /tmp/kimi-cloudflare-main.gpg
sudo install -m 644 /tmp/kimi-cloudflare-main.gpg \
  /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update
sudo apt-get install --yes cloudflared
cloudflared --version
```

Prepare an empty private token file only if it does not already exist:

```bash
install -d -m 700 "$HOME/.config/kimi-agent"
touch "$HOME/.config/kimi-agent/dashboard-tunnel.token"
chmod 600 "$HOME/.config/kimi-agent/dashboard-tunnel.token"
```

## 5. Configure the Cloudflare web UI

Follow Cloudflare's
[dashboard-managed tunnel instructions](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/):

1. Open **Networking > Tunnels** and create a Cloudflared tunnel for this
   development instance.
2. Copy its connector token into `~/.config/kimi-agent/dashboard-tunnel.token`
   using an editor on the host. Store only the token, not the installation
   command. This setup uses a user service, so skip the generated root service
   installation command.
3. If the setup wizard waits for a connector, start the service from step 6,
   then return to the wizard. On the tunnel's **Routes** tab, select
   **Add route > Published application**. Use a hostname such as
   `kimi-dev.example.com`, no path restriction, service type **HTTP**, and
   service URL **127.0.0.1:8088**.

Discord must be able to reach this hostname. An additional Cloudflare Access
interactive login would sit in front of the Activity's existing Discord login
and prevent the normal launch flow. Keep the route accessible to Discord's
proxy. Do not add a cache-everything rule to `/api/*`; allow WebSocket upgrades.

The private tunnel token is separate from the Discord bot token and OAuth
client secret. Keep all three out of frontend files and public documentation.

## 6. Run the tunnel as a user service

From the repository root, install the provided service:

```bash
install -d -m 700 "$HOME/.config/systemd/user"
install -m 600 bot/deploy/kimi-dashboard-tunnel.service.example \
  "$HOME/.config/systemd/user/kimi-dashboard-tunnel.service"
sudo loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user enable --now kimi-dashboard-tunnel.service
```

The unit reads the token from a file, keeping it out of process arguments. An
empty or missing token file causes the unit to be skipped. Metrics listen on
`127.0.0.1:20241`; change that port in the private unit if already occupied.

```bash
systemctl --user is-active kimi-dashboard-tunnel.service
curl --fail --silent --show-error http://127.0.0.1:20241/ready
journalctl --user -u kimi-dashboard-tunnel.service -n 30 --no-pager
```

Confirm that Cloudflare reports a connected tunnel. Restart this service after
rotating the token. The readiness endpoint returns HTTP 200 and a positive
`readyConnections` count once the connector is connected to Cloudflare. This
does not yet prove the dashboard is running: the public route may return 502
until the bot's dashboard listener starts. The connector is updated through apt.

## 7. Configure Discord and launch

In the Developer Portal for the development bot application:

| Setting | Value |
| --- | --- |
| Installation contexts | Guild Install enabled |
| Activities settings | Activities enabled; select the intended Web/iOS/Android platforms |
| Activity URL mapping | Prefix `/`, target `kimi-dev.example.com` |
| OAuth2 redirect URI | `https://127.0.0.1` for Embedded App SDK authorization |

The hostname is the published Cloudflare route from step 5. Discord's
[Activity setup guide](https://docs.discord.com/developers/activities/building-an-activity)
explains the SDK redirect placeholder and portal controls. Enable Developer
Mode on the test account. Before distribution, only the application owner or
developer team can launch the Activity.

Before the first restart on a branch with database changes, take a consistent
private [database backup](database.md#backing-up-the-database). Dashboard tables
are created by the bot's normal startup migrations; do not edit the schema
ledger manually. Restoring older code may require restoring its matching
database backup.

Once the real client secret and both dashboard switches are configured, restart
the existing dev bot service:

```bash
systemctl --user restart kimi-agent.service
systemctl --user is-active kimi-agent.service
curl --fail --silent --show-error --output /dev/null \
  --write-out '%{http_code}\n' http://127.0.0.1:8088/
curl --fail --silent --show-error --output /dev/null \
  --write-out '%{http_code}\n' https://kimi-dev.example.com/
```

Service activation can precede application readiness. Wait for the dashboard
listener and command sync in the private service journal. During initialization
the HTML page may already load while the API still returns 503.

Check each layer separately:

| Check | Expected result |
| --- | --- |
| Tunnel metrics `/ready` | HTTP 200 and a positive `readyConnections` count |
| Local and public `/` | HTTP 200 with the built dashboard page |
| Local and public `/api/bootstrap` | HTTP 200 after bot initialization |
| `/api/session` without a login cookie | HTTP 401 after bot initialization |
| Root page and built assets through `https://<application_id>.discordsays.com` | HTTP 200, confirming the Discord root mapping |
| Global Discord command sync | Includes `/dashboard` and the type-4 `Launch` entry point |

The bootstrap API returns a login challenge and sets a cookie. Check its HTTP
status without copying response bodies or cookie headers into public logs. The
login cookie should retain `Secure`, `HttpOnly`, `SameSite=None`, and
`Partitioned` through both proxies. A command-line request may be treated
differently by Cloudflare security rules; use its Security Events to distinguish
an edge rejection from a response produced by the bot.

Successful HTML and bootstrap requests prove routing and static/API serving.
They do not prove the OAuth client secret or a real Discord session. Opening the
hostname in an ordinary browser does not authenticate a dashboard session.
Complete a real launch with `/dashboard` from the enabled test server, after
global command sync. Verify consent, a model response, an attachment, and
close/reopen before testing the remaining
[live scenarios](dashboard.md#verification-and-current-limits).

Keep any deployment progress log private. Public documentation should use
reserved example hostnames and placeholder IDs, and omit real domains, IPs,
account names, application/server/tunnel IDs, secrets, and captured logs. Record
which checks were performed and which still need a live user test without
publishing the instance identity or its data.

## Troubleshooting and stopping

| Symptom | Check |
| --- | --- |
| Bot fails when dashboard is enabled | Real OAuth client secret, built `dist`, and port availability |
| Tunnel service is skipped | Token file exists and is nonempty |
| Cloudflare returns 502 | Bot is listening at the route's HTTP host and port |
| HTML loads but the API returns 503 | Bot initialization and Discord connection have finished |
| Public requests return 403 before the dashboard loads | Cloudflare Security Events and any Access or challenge rules affecting the route |
| Page loads but login fails | Same Discord application for both credentials, OAuth redirect, root mapping, and cookies reaching the API |
| Activity is missing | Portal Activities switch, development-team access, Developer Mode, and global command sync |
| Dashboard is disabled in the server | Active server plus both global and per-server dashboard switches |
| Activity opens with an access-denied message | User is on any configured `DASHBOARD_ALLOWED_USER_IDS` list and meets `DASHBOARD_MIN_TIER` |

To remove public access, stop the tunnel:

```bash
systemctl --user disable --now kimi-dashboard-tunnel.service
```

To disable the dashboard itself, set `DASHBOARD_ENABLED=false` in
`~/.config/kimi-agent/dashboard.env` and restart the bot. Retain the existing
instance data and saved conversations. Remove the Cloudflare route separately
if retiring the hostname.
