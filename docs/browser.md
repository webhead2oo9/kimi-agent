# Persistent browser

Kimi can expose a member-tier `browser` tool backed by
[BetterWright](https://github.com/CuriosityOS/betterwright), for interactive
sites that ordinary search or a plain HTTP fetch cannot handle. The deployment
pins BetterWright **1.10.0** together with its managed BetterChromium runtime.

## How a browser turn works

Each Discord user gets their own profile under `BROWSER_PROFILES_DIR`, in a
directory named `user-` plus a truncated SHA-256 digest of their Discord id, so
no plaintext id sits on disk. A rooted conversation maps to a stable
BetterWright session, so cookies and tabs carry across tool calls within that
conversation while profiles never cross users. Only one profile worker runs at
a time, so when a different user takes a turn, the previous worker is closed
before the next profile opens.

Per call, the model sends one bounded async Playwright snippet to
`web_browser/bridge.mjs`, which returns BetterWright's structured JSON.
Screenshots are accepted only from the current profile's artifact directory,
checked for containment, magic bytes, and size, then copied into the user's
generated workspace. A model that accepts images is shown them; a `proof`
screenshot is also queued for the Discord reply. Everything a page returns is
untrusted input, never instructions.

BetterWright's credential vault, automatic credential capture, downloads,
public-search fallback, and live-view server are all switched off in the bridge,
and its network policy blocks loopback and private ranges in both deployment
modes. The daemon and cloud browser providers are never reached: the bridge
drives BetterWright in-process.

## Isolation boundary

Every worker runs inside Bubblewrap under a seccomp filter, in its own mount,
process, IPC, UTS, and cgroup namespaces. It sees a private `/tmp`, a read-only
`/usr` alongside the pinned runtime, certificates, and font configuration, and
exactly one writable directory: its own profile, mounted at `/work`. A transient
systemd scope or service enforces aggregate memory, process, and CPU limits,
while `prlimit` bounds open files and output file size. The runtime at
`/opt/kimi/betterwright` must be root-owned and neither group- nor
world-writable, but it still has to be readable and traversable by the
unprivileged bot account; the installer normalizes any owner-only directories
the archive created so that this holds.

System fonts are mounted read-only because Chromium needs ordinary font
discovery to lay out and render pages at all.

## Network modes

`BROWSER_NETWORK_MODE` is a choice you make for the whole deployment; the model
can't select it:

- `host` inherits the bot host's network routes. BetterWright still blocks
  private and loopback targets, but treat this mode as browsing with the host's
  own public identity and routing.
- `netns` launches the whole worker inside the operator-provisioned VPN
  namespace through the fixed, root-owned helper. Startup must prove that a
  known-open private target is unreachable. If the namespace, resolver, helper,
  probe, or VPN is unhealthy, registration fails closed; there is no fallback
  to `host`.

The browser and networked code execution share one process-wide namespace
lease, so the two surfaces can never use the single physical VPN namespace at
the same time. `CODE_EXEC_NETWORK_MODE=none` never claims that lease, and is
the expected pairing when only browser traffic needs the VPN.

The generic helper and sudoers boundary documented in
[code-exec netns deployment](../bot/deploy/code-exec-netns/README.md) serves the
browser too: point `BROWSER_NETNS_HELPER_BIN`, `BROWSER_NETNS_RESOLV_CONF`, and
`BROWSER_NETWORK_PROBE_BLOCKED_IP` at the same fixed paths and target. The
helper never accepts a namespace name from the model.

## Install and enable

On the Linux host, install Node `>=22.18.0`, npm, Bubblewrap, util-linux, the
Chromium shared-library dependencies listed in the
[installer guide](../bot/deploy/betterwright/README.md), and a working user
systemd manager. From `bot/`:

```sh
sudo sh ./deploy/betterwright/install.sh
```

The installer pins `betterwright@1.10.0`, runs `betterwright setup`, verifies
the Linux BetterChromium binary and the import entry point, and locks the
external runtime to root ownership. Installing the runtime doesn't register
anything on its own, so you still need to switch the tool on with either:

```dotenv
BROWSER_ENABLED=true
BROWSER_NETWORK_MODE=host
```

or the VPN profile, filling in this instance's own private paths and target:

```dotenv
BROWSER_ENABLED=true
BROWSER_NETWORK_MODE=netns
BROWSER_NETNS_HELPER_BIN=<root-owned namespace helper>
BROWSER_NETNS_RESOLV_CONF=<namespace resolver file>
BROWSER_NETWORK_PROBE_BLOCKED_IP=<private host:port that must be unreachable>
CODE_EXEC_NETWORK_MODE=none
```

Point the probe at a private endpoint that really is listening. An address
nothing answers on would pass the check for the wrong reason and prove nothing
about the namespace. Restart the bot after changing startup settings. At boot,
`browser` registers only if the runtime and the complete sandbox and network
probe pass.

## Limits and lifecycle

The startup caps are listed in [Configuration](configuration.md). Live
guild/channel tool configuration can only tighten the per-call code size, calls
per turn, returned characters, and screenshots per turn; it can never raise
them. A worker closes after `BROWSER_IDLE_TTL_SECONDS` and is recycled before
the next call once it passes `BROWSER_WORKER_MAX_LIFETIME_SECONDS`.

A profile that grows past `BROWSER_MAX_PROFILE_MB` is deleted outright, not
trimmed, and the call that pushed it over reports that the profile was reset.
The user starts clean on the next call, so any logged-in sessions in that
profile are gone. Size the cap with that in mind.

Profiles expire after `BROWSER_PROFILE_TTL_SECONDS` of inactivity in the normal
filesystem sweeper. `/privacy` → **Delete my data** waits for active work,
closes the user's browser worker, and removes the profile immediately. Give
`data/browser_profiles/` the same private storage, backup, access, and deletion
policy as workspaces. See [Privacy](privacy.md).

## Upgrade procedure

Don't change the installer to `latest`. Instead, review a candidate release
first: its Node floor, setup command, browser path, network defaults,
changelog, tests, and npm audit. Then update the pinned installer version and
this page together, deploy to a test instance, confirm whichever of the `host`
and `netns` startup probes apply to you, and only then replace the production
runtime.
