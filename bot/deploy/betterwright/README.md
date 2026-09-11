# BetterWright runtime installer

This guide walks through installing the reviewed BetterWright runtime that the bot's persistent browser and visual-rendering tools need. The runtime lives outside the bot checkout at `/opt/kimi/betterwright`, root-owned and not writable by the bot account, so a compromised bot can't tamper with the files it executes.

**Quick heads-up before you begin:**

- You need a Linux host with Node `>=22.18.0`, npm, `unzip`, Bubblewrap, util-linux, and a working per-user systemd manager.
- This was tested on the same Ubuntu versions the main setup covers.
- Run the installer with `sudo` from the deployment account. It requires root to create `/opt/kimi/betterwright` and its temporary sibling staging/backup directories. Run the bot and smoke tests unprivileged.
- The bot account needs read and traverse on the install directory so it can execute the immutable Node and BetterChromium files; it does not need write.
- Browser and visual-rendering calls do not install packages or load Mermaid from a CDN once the runtime is in place.

---

## Step 1. Install the system packages the runtime needs

The runtime bundles its own Node and BetterChromium, but it still needs a few shared libraries and host tools.

### Why do this?
BetterChromium links against these libraries at startup. Without them, the runtime starts up but the first real browser launch fails with an opaque `ldd` error. The installer can also detect missing libraries at install time and tell you exactly which one is wrong, but installing the obvious set up front is faster.

### What to run
On the **server**:

```sh
sudo apt-get update
sudo apt-get install --yes bubblewrap util-linux unzip \
  libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
  libasound2t64 libxdamage1 libatspi2.0-0t64
```

The `t64` suffix is common on supported Ubuntu/Debian hosts. Some hosts provide the same libraries without it; both work. If you skip this step and the installer later complains about a missing `.so`, the error message names the exact library.

---

## Step 2. Run the installer

### Why do this?
The installer stages the runtime in a sibling tree, runs `npm ci` against the committed `package-lock.json`, verifies everything, and atomically renames the result into place. Running it once gives you a verified, pinned, root-owned runtime that the bot can execute from.

### What to run
From the bot checkout, on the **server**:

```sh
sudo sh ./deploy/betterwright/install.sh
```

### What you're checking
A successful run prints the staged packages, the version checks, and a final message confirming the runtime is in place at `/opt/kimi/betterwright`. A failed run leaves the previous install untouched, so you can rerun safely.

---

## Step 3. Pick a Node/npm location if yours isn't in `/usr/bin`

### Why do this?
Some hosts install Node through a version manager (nvm, fnm, asdf) and put the binary somewhere unexpected. The installer needs to know which Node and npm to use when it runs `npm ci`.

### What to run
Skip this step if `node` and `npm` are both in `/usr/bin` and Node is at least
22.18.0. Otherwise, use this command instead of Step 2, with absolute paths:

```sh
sudo env NODE_BIN=/absolute/path/to/node NPM_BIN=/absolute/path/to/npm \
  PATH=/absolute/path/to/node-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  sh ./deploy/betterwright/install.sh
```

This doesn't replace the system Node installation; it just tells the installer which binaries to use for the staged `npm ci`. The bot still uses the Node bundled in `/opt/kimi/betterwright` at runtime.

---

## Step 4. Keep the configured runtime path fixed

### Why do this?
The install destination is fixed at `/opt/kimi/betterwright`; the installer
accepts no path argument. Point `BROWSER_RUNTIME_DIR` at that location in the
bot's active environment file.

### What to configure
Add the runtime path to the bot's active `ENV_FILE` (normally `kimi.env`):

```dotenv
BROWSER_RUNTIME_DIR=/opt/kimi/betterwright
```

---

## Step 5. Bump the runtime version (only when you're ready)

### Why do this?
You adopt a newer BetterWright or Mermaid release by editing `deploy/betterwright/package.json`, regenerating the lockfile, and rerunning the installer. Doing it deliberately avoids surprise upgrades.

### What to run
1. Update the `betterwright` or `mermaid` dependency in
   `deploy/betterwright/package.json`.
2. Update the matching `VERSION` or `MERMAID_VERSION` constant in
   `deploy/betterwright/install.sh`. For Mermaid, also update
   `_MERMAID_VERSION` in `web_browser/visual_service.py`.
3. Regenerate `package-lock.json` with `npm install --package-lock-only` from
   `deploy/betterwright/`.
4. Re-run the installer from Step 2.

Don't use an unbounded npm range in production. npm and network access are install-time requirements only; browser and visual-rendering calls do not install packages or load Mermaid from a CDN.

---

## Step 6. Enable lingering for the bot account

### Why do this?
The runtime's transient browser and code-exec units need a user manager that survives logout. Without lingering, those units stop the moment the bot account logs out, and the next bot run finds the runtime offline.

### What to run
On the **server**:

```sh
sudo loginctl enable-linger <bot-user>
```

Replace `<bot-user>` with the account the bot runs under. The unit file itself is [`../kimi.service.example`](../kimi.service.example).

---

## Step 7. Run the smoke test before enabling the service

### Why do this?
The smoke test exercises the runtime boundary without Discord: real browser navigation, profile persistence across worker switches, cross-user isolation, chart and Mermaid rendering with host-side validation. Any of those failing would surface the first time a user actually uses the bot, so it pays to catch them here.

### What to run
From `bot/` on the **server**, after completing the private configuration and
runtime profile in the [setup guide](../../../docs/setup.md):

```sh
./scripts/preflight
```

With `BROWSER_ENABLED=true`, preflight loads the same `ENV_FILE`, `RUNTIME_ENV`,
and operator overlay as the installed service before running the smoke test.
For a development environment whose complete profile is in `.env.dev`, run
`ENV_FILE=.env.dev .venv/bin/python -m deploy.betterwright.smoke_test` instead.
The direct smoke-test command does not load the separate runtime environment.

### What you're checking
The output shows public navigation, profile persistence, cross-user isolation through real worker switches, and rendering of a chart and a Mermaid diagram. The synthetic profiles and outputs are removed at the end. Any failed runtime or sandbox check makes it exit nonzero.

You're good when it exits clean. Then start the service.
