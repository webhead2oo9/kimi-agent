# Getting Kimi Up and Running on Ubuntu

This guide walks you through setting up Kimi on an Ubuntu server from start to finish.

**Before you begin:**
- This was tested on Ubuntu Server 26.04.1 LTS (64-bit) with Python 3.14.
- You'll run everything as a regular sudo-enabled user. Never run as root.
- The current dependency lock only works reliably on `amd64`/`x86_64`. ARM64 hosts will hit snags right now.
- Your private config, secrets, database, and workspaces live **outside** the checkout so updates stay safe.
- Replace every `<placeholder>` with your real values.
- Commands marked **workstation** run on the computer you use to manage the server. Commands marked **server** run after you SSH in.



---

## 1. Make SSH Nice and Easy (No Passwords Every Time)

Skip this section if you already have password-free SSH working.

### Why do this?
You want a secure key so you can connect without typing a password every time, and without ever sending passwords over the network.

**On the server console** (if SSH isn't running yet):

```sh
sudo apt-get install --yes openssh-server
sudo systemctl enable --now ssh
sudo ss -ltnp | grep ':22'
```

You should see sshd listening on port 22.

Record the server's host key fingerprint so you can verify it later:

```sh
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

**On your workstation**, make the first password login and compare that fingerprint character-by-character before accepting the host key:

```sh
ssh <deployment-user>@<server-address>
```

Now create a dedicated SSH key for this server (it won't touch any keys you already have):

```sh
ssh-keygen -t ed25519 -f "$HOME/.ssh/kimi-install-ed25519" -C kimi-install
ssh-copy-id -i "$HOME/.ssh/kimi-install-ed25519.pub" \
  <deployment-user>@<server-address>
```

If your workstation doesn't have `ssh-copy-id`, do it manually during the password session:

```sh
scp "$HOME/.ssh/kimi-install-ed25519.pub" \
  <deployment-user>@<server-address>:/tmp/kimi-install-ed25519.pub
ssh <deployment-user>@<server-address> \
  'umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; grep -qxF -f /tmp/kimi-install-ed25519.pub ~/.ssh/authorized_keys || cat /tmp/kimi-install-ed25519.pub >> ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; rm -f /tmp/kimi-install-ed25519.pub'
```

Add this to your workstation's `~/.ssh/config` so you can just type `ssh kimi-install`:

```sshconfig
Host kimi-install
    HostName <server-address>
    User <deployment-user>
    IdentityFile ~/.ssh/kimi-install-ed25519
    IdentitiesOnly yes
```

Test that it truly won't fall back to a password:

```sh
ssh -o BatchMode=yes -o PasswordAuthentication=no kimi-install 'id -un; hostname'
```

It should print your username and the server hostname with zero prompting. You're good.

---

## 2. Take a Quick Baseline of Your Server

Before installing anything, let's see what we're working with. Run this on the **server**:

```sh
cat /etc/os-release
uname -m
dpkg --print-architecture
id
python3 --version || true
df -hT /
free -h
ps -p 1 -o comm=
systemctl --version | head -n 1
```

**What you're checking:**
- Linux + Python 3.14 or newer
- You're not root (good!)
- Enough disk space and memory for the features you want
- The browser features will need a little extra room for Node and Chromium

---

## 3. Install the Packages Kimi Needs

### Core packages
On the **server**:

```sh
sudo apt-get update
sudo apt-get install --yes git python3-venv
```

### Sandbox packages (browser, code execution, skills)
These features run inside isolated sandboxes, so we need a few extra tools:

```sh
sudo apt-get install --yes bubblewrap util-linux
```

### Browser + visual rendering packages
The browser and chart tools also need Node 22.18+, npm, unzip, and some shared libraries:

```sh
sudo apt-get install --yes nodejs npm unzip \
  libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
  libasound2t64 libxdamage1 libatspi2.0-0t64
node --version
npm --version
```

Ubuntu 26.04.1 ships Node 22.22. Perfect. Don't continue on anything older than 22.18.

---

## 4. Clone the Bot

Do this as your normal deployment user on the **server**:

```sh
git clone https://github.com/webhead2oo9/kimi-agent.git kimi-agent
cd kimi-agent
```

If you want a branch other than `main`, add `--branch <name>` to the clone command.

---

## 5. Create the Python Environment

We'll create a virtual environment and install the bot into it. If `uv` is
already installed, it uses the repository lock; otherwise standard pip resolves
the dependencies declared by the local projects.

Run this as the deployment user:

```sh
cd "$HOME/kimi-agent/bot"

if command -v uv >/dev/null 2>&1; then
  echo "Using installed uv"
  uv sync --locked
  .venv/bin/python -m ensurepip
  .venv/bin/python -m pip --disable-pip-version-check install \
    --no-deps --editable ./packages/kimi-agent-module-api --editable .
else
  echo "Using standard venv and pip"
  python3 -m venv .venv
  .venv/bin/python -m pip --disable-pip-version-check install \
    --editable ./packages/kimi-agent-module-api --editable .
fi

test -x .venv/bin/python
.venv/bin/python --version
.venv/bin/python -m pip check
```

**What success looks like:**
- `.venv/bin/python` exists and is Python 3.14+
- pip says "No broken requirements found."

If you want `uv` later, install it with its official standalone installer
(never with sudo).

---

## 6. Prepare User Services and Sandbox Tools

Some features (browser, code execution) run as transient systemd user services, so your user's systemd manager has to stay running even after you log out.

Enable lingering:

```sh
sudo loginctl enable-linger "$(id -un)"
sudo systemctl start "user@$(id -u).service"
```

### Ubuntu 26.04.1: Disable Apport

Ubuntu's crash reporter (Apport) pipes crash dumps to a collector, which defeats the sandbox's core-dump limit, so Kimi refuses to run code while it is active. Check:

```sh
sysctl kernel.core_pattern
```

If the output starts with `|`, turn it off:

```sh
sudo sed -i 's/^enabled=.*/enabled=0/' /etc/default/apport
sudo systemctl disable --now apport.service
sysctl kernel.core_pattern
```

Only continue once the value no longer starts with `|`.

### Install the Browser Runtime

The browser and chart tools need a pinned Chromium runtime:

```sh
sudo sh ./deploy/betterwright/install.sh
.venv/bin/python -m deploy.betterwright.smoke_test
```

A successful run ends with `browser smoke passed`.

The runtime is installed at `/opt/kimi/betterwright` (owned by root). Leave it there.

---

## 7. Create Your Private Folders

Your config, secrets, database, and workspaces live outside the public checkout so updates stay safe.

Run this as the deployment user:

```sh
umask 077
install -d -m 700 \
  "$HOME/.config/kimi-agent/config" \
  "$HOME/.config/kimi-agent/config/prompts/commands" \
  "$HOME/.config/kimi-agent/secrets" \
  "$HOME/.local/share/kimi-agent/data" \
  "$HOME/.local/share/kimi-agent/workspaces" \
  "$HOME/.local/share/kimi-agent/skills" \
  "$HOME/.local/share/kimi-agent/browser_profiles" \
  "$HOME/.local/state/kimi-agent/logs" \
  "$HOME/.cache/kimi-agent/attachments"
```

Copy the default prompt and model files:

```sh
install -m 600 config/prompt.md config/persona.md \
  "$HOME/.config/kimi-agent/config/"
install -m 600 config/prompts/commands/*.md \
  "$HOME/.config/kimi-agent/config/prompts/commands/"
install -m 600 config/models.example.yaml \
  "$HOME/.config/kimi-agent/config/models.yaml"
printf '%s\n' '{}' > "$HOME/.config/kimi-agent/secrets/skills.yaml"
chmod 600 "$HOME/.config/kimi-agent/secrets/skills.yaml"
```

### Where things live

| Path | Purpose |
|---|---|
| `~/.config/kimi-agent/config` | Prompts, models, guild settings |
| `~/.config/kimi-agent/kimi.env` | Discord token + main settings (mode 600) |
| `~/.config/kimi-agent/secrets` | Skill secrets, Codex auth |
| `~/.local/share/kimi-agent` | Database, workspaces, skills, browser profiles |
| `~/.local/state/kimi-agent/logs` | Tool event logs |
| `~/.cache/kimi-agent/attachments` | Temporary files |

### Create `kimi.env`

Print your home path first:

```sh
printf '%s\n' "$HOME"
```

Then create the file (replace every placeholder):

```sh
cat > "$HOME/.config/kimi-agent/kimi.env" <<'EOF'
# Discord and the explicitly activated sandbox guild.
DISCORD_BOT_TOKEN=<discord-bot-token>
BOT_NAME=Kimi
OWNER_USER_ID=<operator-user-id>
STAFF_USER_IDS=<operator-user-id>
ALLOWED_GUILD_IDS=<guild-id>

# Required by the Discord logging module used in this guide.
MESSAGE_CONTENT_INTENT=true
MEMBERS_INTENT=true

# User-installed personal chat and DM access.
USER_APP_CHAT_ENABLED=true
USER_APP_STAFF_IDS=<operator-user-id>
USER_APP_MEMBER_IDS=
USER_APP_REGULAR_IDS=
USER_APP_CHAT_TIMEOUT_SECONDS=840
USER_APP_DM_ENABLED=true

# Generic key-backed provider. Use the exact variable named by models.yaml.
MODEL_API_KEY=<provider-api-key>
CODEX_TOKEN_FILE=<home-directory>/.config/kimi-agent/secrets/codex-auth.json

# Private configuration and runtime state.
CONFIG_DIR=<home-directory>/.config/kimi-agent/config
SKILLS_DIR=<home-directory>/.local/share/kimi-agent/skills
DATABASE_PATH=<home-directory>/.local/share/kimi-agent/data/bot.db
WORKSPACE_DIR=<home-directory>/.local/share/kimi-agent/workspaces
ATTACHMENT_STORE_DIR=<home-directory>/.cache/kimi-agent/attachments
PERSONAL_SKILLS_DIR=<home-directory>/.local/share/kimi-agent/personal_skills
TOOL_EVENT_LOG_ENABLED=true
TOOL_EVENT_LOG_PATH=<home-directory>/.local/state/kimi-agent/logs/events.jsonl
TOOL_EVENT_LOG_CONTENT_MODE=metadata
SECRETS_FILE=<home-directory>/.config/kimi-agent/secrets/skills.yaml
BROWSER_PROFILES_DIR=<home-directory>/.local/share/kimi-agent/browser_profiles
BROWSER_RUNTIME_DIR=/opt/kimi/betterwright

# Separately installed application modules, by entry-point name.
KIMI_MODULES=
EOF
chmod 600 "$HOME/.config/kimi-agent/kimi.env"
```

**Important:** Do not leave any angle-bracket placeholders in the file. Add only the extra keys your deployment actually needs (internet search, for example, activates when you set at least one of `TINYFISH_API_KEY`, `EXA_API_KEY`, or `BRAVE_API_KEY`).

When creating a separate installation, only bring over config and credential files. For an intentional migration or restore, stop the bot first and copy the database with its WAL sidecars, workspaces, personal skills, and any browser profiles you want to preserve. Never copy the venv; logs are optional.

---

## 8. Configure the Discord Application

Go to the Discord Developer Portal for the application that owns your bot token.

### Bot settings
1. On the **Bot** tab, create or reset the token and store it only in `kimi.env`.
2. Enable **Message Content Intent** only if a module or feature you enable requires it (we set `MESSAGE_CONTENT_INTENT=true` in the example env file).
3. Enable **Server Members Intent** only if a module or feature you enable requires it (the optional discord-logging example needs it).
4. Keep **Guild Install** enabled with the `bot` and `applications.commands` scopes.
5. Install the bot only into your sandbox guild and give it the minimum permissions it needs.

For normal chat the bot needs: View Channel, Send Messages, Read Message History, Embed Links, Attach Files, and Use Application Commands.

Turn on Developer Mode in Discord so you can right-click → Copy ID for guilds, channels, users, and roles. The `ALLOWED_GUILD_IDS` setting only activates the guilds you list. Empty does **not** mean "every guild."

### Enable user-installed chat and DMs
In the same installation settings:

1. Enable **User Install** as an installation context.
2. Give it the `applications.commands` scope.
3. Install the user-install link on each allowlisted account.
4. Leave Guild Install enabled (it's a separate surface).

The `USER_APP_*` settings let the operator use `/chat` and expose `/privacy`, `/memory`, and `/stop` on the personal surface. DMs are ignored for anyone not on the allowlist.



---

## 9. Configure Model Providers

Edit the model routing file:

```sh
nano "$HOME/.config/kimi-agent/config/models.yaml"
```

Replace every `.example.invalid` URL and model ID. Set realistic context windows and capabilities. The `chat` role must support text + tool calling; `compaction` only needs text. Don't declare `image_input` until you've actually tested images on that route.

A minimal working example:

```yaml
providers:
  primary:
    type: openai_compat
    base_url: https://api.example.com/v1
    api_key_env: MODEL_API_KEY

models:
  primary-chat:
    provider: primary
    model: <provider-model-id>
    context_window: <provider-context-window>
    capabilities: [text, tool_calling]

roles:
  chat: primary-chat
  chat_fallbacks: []
  compaction: primary-chat
  compaction_fallbacks: []

selectable_chat_models: [primary-chat]

overrides:
  channels: {}
  guilds: {}
  users: {}
  commands: {}
```

Secrets stay in `kimi.env`; `models.yaml` just names the environment variable.

If your model route uses `codex`, run the login helper:

```sh
./scripts/codex-login
```

It will prompt for confirmation and then start the device authentication flow.

---

## 10. Create a Runtime Profile

Here's a baseline for a 4 vCPU / 4 GiB RAM machine.

```sh
cat > "$HOME/.config/kimi-agent/runtime.env" <<'EOF'
BROWSER_ENABLED=true
BROWSER_NETWORK_MODE=host
BROWSER_MAX_TOTAL_MEMORY_MB=1536
BROWSER_MAX_TASKS=128
BROWSER_CPU_QUOTA_PERCENT=200
BROWSER_TMP_SIZE_MB=256
BROWSER_MAX_PROFILE_MB=256

CODE_EXEC_ENABLED=true
CODE_EXEC_MIN_TIER=member
CODE_EXEC_NETWORK_MODE=none
CODE_EXEC_PYTHON_BIN=/usr/bin/python3
CODE_EXEC_MAX_MEMORY_MB=2048
CODE_EXEC_MAX_TOTAL_MEMORY_MB=1536
CODE_EXEC_MAX_TASKS=96
CODE_EXEC_CPU_QUOTA_PERCENT=200
CODE_EXEC_TMP_SIZE_MB=256
CODE_EXEC_ENV_DIR_MAX_MB=512
CODE_EXEC_ENV_DIR_MAX_FILES=50000
EOF
chmod 600 "$HOME/.config/kimi-agent/runtime.env"
```

`BROWSER_NETWORK_MODE=host` is the straightforward public-browser path. Code execution stays offline. The test needed `BROWSER_MAX_TASKS=128` because Chromium creates renderer threads. Lowering it without re-testing can cause failures.

Don't blindly copy resource limits or network modes from another host.

---

## 11. (Optional) Discord Logging Module Example

Kimi supports separately installed application modules. The discord-logging module is one example of what a module can do. It is completely optional. You only need it if you want edit/delete/invite/member logging in a channel.

If you enable it, it requires the two privileged intents. You can review the module's code to see exactly what it does and what permissions it needs. The steps below show how to install and configure it as an example.

```sh
module_dir="$HOME/kimi-agent-discord-logging"
module_commit="<reviewed-module-commit>"
test ! -e "$module_dir"
git clone https://github.com/webhead2oo9/kimi-agent-discord-logging.git \
  "$module_dir"
git -C "$module_dir" checkout --detach "$module_commit"
git -C "$module_dir" status --short --branch
test "$(git -C "$module_dir" rev-parse HEAD)" = "$module_commit"

cd "$HOME/kimi-agent/bot"
.venv/bin/python -m pip --disable-pip-version-check install \
  --no-deps --editable "$module_dir"
.venv/bin/python - <<'PY'
from importlib.metadata import entry_points, version

names = {item.name for item in entry_points(group="kimi_agent.modules")}
assert "discord_logging" in names
print("discord-logging-version=" + version("kimi-agent-discord-logging"))
print("discord-logging-entry-point=present")
PY
```

Then set the installed entry-point name in `kimi.env` before startup:

```dotenv
KIMI_MODULES=discord_logging
```

After any later `uv sync`, repeat the editable install.

Create a minimal guild config for the sandbox:

```sh
install -d -m 700 \
  "$HOME/.config/kimi-agent/config/guild-modules/<guild-id>"
cat > "$HOME/.config/kimi-agent/config/guild-modules/<guild-id>/discord_logging.md" <<'EOF'
---
logging_channel_id: <logging-channel-id>
log_edits: true
log_deletes: true
log_bulk_deletes: true
log_invite_create: true
log_invite_delete: true
log_member_joins: true
ignored_channel_ids: []
snapshot_retention_days: 30
---
EOF
chmod 600 \
  "$HOME/.config/kimi-agent/config/guild-modules/<guild-id>/discord_logging.md"
```

The logging channel needs View Channel, Send Messages, and Embed Links. The bot needs View Channel (and ideally Read Message History) in observed channels. Put sensitive channels in the ignored list.

Follow the module's own README for the full field list and privacy notes.

---

## 12. Run Preflight Checks (No Discord Connection Yet)

Run the preflight helper:

```sh
./scripts/preflight
```

It checks provider credentials, model routing, Codex authentication (if used),
and the browser runtime when `BROWSER_ENABLED=true`.

A missing or revoked required Codex credential will cause the script to exit with an error. Temporary network problems will show a warning. The bot will retry automatically on first use.

These checks do not connect the bot to Discord.

---

## 13. Install the systemd Service

Run the installer from the bot directory:

```sh
./scripts/install-service
```

It will create the service file with the correct paths, enable lingering if needed, and enable the service.

You can start it later with:
```sh
systemctl --user start kimi-agent.service
```

---

## 14. Start the Service

If nothing else is using the token, just start it:

```sh
systemctl --user start kimi-agent.service
systemctl --user status kimi-agent.service --no-pager
journalctl --user -u kimi-agent.service -n 150 --no-pager
```

---

## 15. Day-to-Day Service Management

Handy commands:

```sh
systemctl --user start kimi-agent.service
systemctl --user stop kimi-agent.service
systemctl --user restart kimi-agent.service

systemctl --user status kimi-agent.service --no-pager
journalctl --user -u kimi-agent.service -n 200 --no-pager
journalctl --user -u kimi-agent.service -b --no-pager
journalctl --user -u kimi-agent.service \
  --since '30 minutes ago' --no-pager

# Follow live logs until you press Ctrl-C
journalctl --user -u kimi-agent.service -f
```

Restart after changing `kimi.env`, `runtime.env`, `models.yaml`, or module registration. Most prompt and fragment files reload live, but a restart is still the safest validation.

A quick restart can leave a stale scheduler lease for up to 60 seconds. Always prove there's only one `bot.py` process. The "another scheduler runner holds the lease" message is only okay if you then see "Module scheduler resumed" and the module becomes healthy within roughly 80 seconds.

Check boot setup:

```sh
loginctl show-user "$(id -un)" -p Linger
systemctl --user is-enabled kimi-agent.service
systemctl --user is-active kimi-agent.service
```

---

## 16. Upgrading

These steps assume the v1-to-v2 upgrade is complete, including v2 extension
packages and database schema v7. Fresh databases and existing v7 databases
start normally; older database schemas are rejected. No further migration is
needed for an already-upgraded installation. See [Database](database.md#schema-upgrades)
for the supported schema boundary and backup requirements.

### 1. Stop the service
```sh
systemctl --user stop kimi-agent.service
```

### 2. (Optional) Quick backup
If you want a safety net:
```sh
umask 077
backup_dir="$HOME/kimi-agent-backups/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$backup_dir"
backup_items=()
for item in \
  .config/kimi-agent \
  .config/systemd/user/kimi-agent.service \
  .config/systemd/user/kimi-agent.service.d \
  .local/share/kimi-agent \
  .cache/kimi-agent \
  .local/state/kimi-agent
do
  if [[ -e "$HOME/$item" ]]; then
    backup_items+=("$item")
  fi
done
if (( ${#backup_items[@]} == 0 )); then
  echo "No standard deployment state found; identify custom paths before continuing" >&2
  exit 1
fi
tar -C "$HOME" -czf "$backup_dir/private-state.tar.gz" "${backup_items[@]}"
chmod 600 "$backup_dir/private-state.tar.gz"
```

Back up any deployment-owned environment, credential, database, workspace,
attachment, skill, browser-profile, or log path outside these standard trees
separately, preserving its original path and permissions.

### 3. Update the code
Make sure your checkout is clean, then pull:
```sh
cd "$HOME/kimi-agent"
git status --short --branch
git pull --ff-only
```

Compare any prompt changes against your private copies.

### 4. Reinstall Python packages
```sh
cd "$HOME/kimi-agent/bot"
if command -v uv >/dev/null 2>&1; then
  uv sync --locked
  .venv/bin/python -m ensurepip
  .venv/bin/python -m pip install --no-deps \
    --editable ./packages/kimi-agent-module-api --editable .
else
  python3 -m venv .venv
  .venv/bin/python -m pip install \
    --editable ./packages/kimi-agent-module-api --editable .
fi
```

### 5. Optional: discord-logging module
If you installed the optional discord-logging module, update it too:
```sh
.venv/bin/python -m pip install --no-deps --editable "$HOME/kimi-agent-discord-logging"
```

### 6. Run preflight and start
```sh
./scripts/preflight
systemctl --user start kimi-agent.service
```

---

## 17. Validation Checklist

A healthy installation should show:

1. Service is `active/running` with `Result=success` and no restart loop.
2. Discord connected, your guild is active, slash commands synced.
3. Database and every module initialized; `discord_logging` reports healthy.
4. Browser and code execution registered under your chosen isolation modes.
5. No error-level journal entries, unexplained warnings, permission problems, or missing files.
6. A clean restart produces the same result.

When testing interaction, stay inside the sandbox guild: one normal response, `/modules status`, an edit/delete logging event, private `/chat`, public `/chat`, and an allowlisted DM. Note anything you didn't exercise.

For automatic startup, reboot only after the first connection is solid:

```sh
sudo reboot
```

After reconnecting:

```sh
ssh kimi-install
systemctl --user is-active kimi-agent.service
systemctl --user status kimi-agent.service --no-pager
journalctl --user -u kimi-agent.service -b --no-pager
```

Make sure your persistent data survived.

---

## 18. Troubleshooting

| Symptom | What it usually means & what to check |
|---|---|
| `.venv/bin/python` is missing | Install `python3-venv` and repeat step 5. |
| `uv: command not found` | uv is optional. Use the venv/pip path or install uv from its official site. |
| `No module named pip` after `uv sync` | uv intentionally prunes pip. Run `.venv/bin/python -m ensurepip` before installing extra modules. |
| BetterWright says `unzip extract failed` | Install `unzip` and rerun the installer. |
| Chromium can't create threads | Raise `BROWSER_MAX_TASKS` (test needed 128) and rerun the smoke test. |
| Browser/code probe fails | Check `sysctl kernel.core_pattern`. A leading `|` bypasses the sandbox's zero-core limit. Also verify Bubblewrap, user manager, and workspace mounts. |
| `DISCORD_BOT_TOKEN is not set` | The `ENV_FILE` is missing, unreadable, or doesn't contain the token. Check paths and permissions (don't print the value). |
| Provider credentials unavailable | A chat, fallback, or compaction route is missing its key/token. |
| Model routing file not found | `<CONFIG_DIR>/models.yaml` is missing. Kimi never falls back to the example template. |
| Discord rejects privileged intents | The portal and your `kimi.env` settings disagree. Enable both sides or turn off the feature that needs them. |
| Bot is online but ignores the guild | The guild is inactive, its fragment is invalid, `bot_active: false` wins, or the bot lacks channel permissions. |
| `discord_logging` entry point missing | Reinstall the module after any `uv sync` or environment recreation. |
| Logging module is soft-disabled | Both required intents aren't enabled. `/modules status` will tell you exactly what's missing. |
| Module is healthy but posts nothing | Check the exact guild-module file path, logging channel ID, ignored list, event toggles, and permissions. |
| `systemctl --user` can't reach the bus | Run it as the deployment user. Confirm lingering and `user@<uid>.service` are working. Never use sudo for user units. |
| Service restarts over and over | Stop it, fix the first journal error, run `systemctl --user reset-failed`, then start once. |
| Scheduler pauses after a fast restart | Prove there's only one `bot.py` process. A stale lease can take up to 60 seconds to clear; wait for the "resumed" message and a healthy module within ~80 seconds. |
| Skill secrets file warning | Non-fatal. Create a mode-600 `{}` file when you don't intend to use executable skill secrets. |

Run the diagnostics helper for a quick overview:

```sh
./scripts/diagnostics
```

Never dump the full environment or print credential files while debugging.

---



That is the whole process. Take it one section at a time. When something does not go to plan, the journal and the troubleshooting table above are the first places to look.
