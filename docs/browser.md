# Persistent browser

Kimi can give members a real browser for sites that a search or a plain page fetch cannot handle: pages that need JavaScript, logins, or clicking through. It runs inside the same hardened sandbox used for code execution. The deployment pins one BetterWright release and the matching Chromium build. The same install also brings in Mermaid for the separate chart and diagram tools.

Enabling the browser adds real attack surface. It is off by default. Only turn it on when you have reviewed the isolation boundary, chosen a network mode, and tested the startup probe on your host.

## How a browser turn works

Each Discord user gets their own profile. The folder name is a hash of their user id, not the id itself. Cookies and local storage persist in that user's profile across conversations, while tabs stay inside one conversation. Nothing mixes across users. Only one worker runs at a time; switching users closes the current worker before the next one starts.

Each tool call sends one short, size-limited Playwright snippet to the bridge process and gets structured JSON back. Screenshots are accepted only from the current profile's artifact directory. They are checked for path, type, and size, then copied into the user's workspace. If the model can see images, it receives them. When the model takes a proof screenshot (`screenshot({kind:'proof'})`), that image is also attached to the Discord reply so the user can see what the bot saw. All page content is treated as untrusted data.

BetterWright's credential vault, automatic downloads, public-search fallback, and live view are disabled. The bridge blocks loopback and private networks. Cloud and daemon providers are never used.

## The isolation boundary

Every worker runs inside Bubblewrap under a seccomp filter, with its own mount, process, IPC, UTS, and cgroup namespaces. Inside, it sees a private `/tmp`, a read-only `/usr` plus the pinned runtime, certificates, and fonts, and one writable directory: its own profile, mounted at `/work`. A temporary systemd scope enforces memory, process-count, and CPU limits, and `prlimit` caps open files and output size.

This protects the host in several ways:

- The worker cannot see other users' profiles, the bot's own files, the repository, or the database.
- It cannot reach the host's normal network routes unless you explicitly choose `host` mode.
- Even if Chromium is compromised, the namespaces, seccomp filter, and dropped privileges limit what the escape can reach.
- The runtime at `/opt/kimi/betterwright` is root-owned and not group- or world-writable. The unprivileged bot user can only read and traverse it.

System fonts are mounted read-only because Chromium needs them to render pages. The bot service can keep `NoNewPrivileges=yes`.

The sandbox shares the host kernel. A successful escape would land in the bot account. For a hostile user population, consider a dedicated VM with no credentials it does not need.

## Choosing a network mode

`BROWSER_NETWORK_MODE` applies to the whole deployment. The model cannot change it per call.

| Mode | Internet access | Private network protection | Notes |
|---|---|---|---|
| `host` | Yes, through the server | Blocks loopback and private targets at the bridge, but traffic uses the server's normal routes and identity | Simple. Everything else sees your server's public IP. |
| `netns` | Yes, through an operator-provisioned VPN namespace | Startup must prove a known-private target is unreachable. No fallback to `host`. | Requires a root-owned helper, namespace resolver, and a working VPN. Fails closed if the probe or tunnel is unhealthy. |

If both the browser and code execution use `netns`, they take turns: only one of them can hold the VPN namespace at a time. A coding job that needs it can ask an idle browser worker to close and will wait up to 30 seconds for that. The namespace name is fixed inside the root-owned helper; the model never picks it.

## Installing and turning it on

You need Node `>=22.18`, npm, Bubblewrap, util-linux, the Chromium libraries listed in the installer, and a working user systemd manager.

From the `bot/` directory:

```sh
sudo sh ./deploy/betterwright/install.sh
```

The installer pins the runtime and replaces the root-owned copy at `/opt/kimi/betterwright`. Installing the runtime does not enable the tool.

You still need to set the environment variables and restart:

```dotenv
BROWSER_ENABLED=true
BROWSER_NETWORK_MODE=host
```

or the netns profile:

```dotenv
BROWSER_ENABLED=true
BROWSER_NETWORK_MODE=netns
BROWSER_NETNS_HELPER_BIN=<root-owned namespace helper>
BROWSER_NETNS_RESOLV_CONF=<namespace resolver file>
BROWSER_NETWORK_PROBE_BLOCKED_IP=<private host:port that must be unreachable>
CODE_EXEC_NETWORK_MODE=none
```

Point the probe at a real private endpoint that must stay unreachable, for example an internal service on your LAN. At startup, the `browser` tool only appears if the runtime, sandbox, and network probe all pass. The chart and diagram tools then appear if the exact Mermaid file is present and root-owned.

## Limits, profiles, and privacy

The startup limits are listed in the [configuration guide](configuration.md). Per-server and per-channel config can lower the per-call limits but never raise them. A worker closes after `BROWSER_IDLE_TTL_SECONDS` without a call. Once a worker has been alive longer than `BROWSER_WORKER_MAX_LIFETIME_SECONDS`, it is replaced with a fresh one before the next call.

If a profile grows past `BROWSER_MAX_PROFILE_MB`, it is deleted (not trimmed). Any logged-in sessions in that profile are lost. The next call for that user starts fresh. Profiles also expire after `BROWSER_PROFILE_TTL_SECONDS` of inactivity.

When a user chooses `/privacy` → **Delete my data**, Kimi waits for any browser work in progress, closes the worker, and deletes the profile right away.

Treat `data/browser_profiles/` with the same private storage, backup, access, and deletion policy as workspaces. See the [privacy policy](privacy-policy.md).

## Upgrading the runtime

Do not change either locked package to `latest`. Before moving to a new BetterWright or Mermaid release, check its Node requirement, setup command, browser and bundle paths, network defaults, changelog, security advisories, and test results, and run `npm audit`.

Update `package.json`, `package-lock.json`, the installer's version checks, and the docs together. Deploy to a test instance first and run the full browser and visual smoke test, plus the `host` or `netns` probe you use in production. Only then replace the production runtime.
