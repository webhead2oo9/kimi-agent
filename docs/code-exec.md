# Code execution

`run_code` is the `MEMBER`-tier tool for running code: inline Python, an inline
shell script, or a file from the caller's own workspace. It is Linux-only and off
by default. Setting `CODE_EXEC_ENABLED` is not enough on its own: the tool
registers only after the exact sandbox and network profile you configured
survives a real end-to-end run at startup. A profile that fails that probe leaves
`run_code` unregistered rather than degraded, so the model never sees a
half-working sandbox.

Runs happen in `auto`, `python`, `shell`, or `direct` mode. In a networked mode a
run can also install validated public packages into a `.venv` that persists in
the workspace, so a later run starts with them already there. Small files a run
creates or changes are attached to the reply; a large build stays in the
workspace until the model names the deliverable with `queue_file`.

The optional [durable coding agent](coding-agent.md) reuses this exact boundary
for long managed jobs. It can apply larger wall/CPU ceilings, but does not gain
a wider filesystem, network, syscall, quota, or credential surface. Both normal
runs and managed jobs pass a manager-side `RuntimeMaxSec` to systemd in addition
to application cancellation and timeout handling.

## Choose a network mode

`CODE_EXEC_NETWORK_MODE` is a choice you make for the whole deployment, not an
argument the model passes in. No run can widen, narrow, or switch the boundary
you picked.

| Mode | Internet | Private-network boundary | Privileged helper |
|---|---|---|---|
| `none` | No | Fresh empty network namespace | No |
| `host` | Yes, through the bot host | **None beyond the host's own routing/firewall** | No |
| `netns` | Yes, through an operator-provisioned namespace | Must pass a private-target isolation probe | Yes |

The tracked defaults are `CODE_EXEC_ENABLED=false` and
`CODE_EXEC_NETWORK_MODE=none`, so cloning the public repository gives you neither
execution nor host egress until you ask for both.

### `none`

Use `none` when code needs nothing beyond the standard library, the optional
read-only packages environment, and files already in the workspace. Bubblewrap's
`--unshare-all` hands the run an empty network namespace, and `pip_install` is
rejected outright.

### `host`

Use `host` only when runs need the internet and you accept what host networking
means. Bubblewrap keeps the bot process's own network namespace with
`--share-net`, so traffic leaves from the server's public IP and can reach
anything the host can reach. That usually includes:

- services bound to loopback;
- LAN, VPN, container, and private-cloud routes;
- link-local services and cloud metadata endpoints;
- other internal infrastructure the host firewall allows.

The filesystem, process, syscall, resource, and credential boundaries all stay in
place, but none of them turns host networking into a private-network boundary.
The startup probe confirms that DNS and real TLS egress work; it cannot prove
anything is unreachable. If those private destinations must be off limits,
put the bot in a dedicated VM or put a separately audited egress policy in front
of it.

### `netns`

Use `netns` when your users are untrusted and internet access must not travel
over the bot host's routes. You provision a persistent network namespace holding
a VPN or another constrained uplink; Kimi does not care which provider it is.

Registration succeeds only if a single probe, run through the real privileged
launch chain rather than a simulation of it, proves all of the following:

1. seccomp is installed and a denied syscall returns `EPERM`;
2. a route exists;
3. DNS works through the namespace resolver;
4. a real TLS connection succeeds; and
5. `CODE_EXEC_NETWORK_PROBE_BLOCKED_IP`, configured as a known-open private
   `host` or `host:port`, is unreachable.

The namespace itself should reject loopback, private, link-local, and metadata
destinations, along with outbound services the deployment does not need. SMTP is
the usual one. It must also fail closed: if the tunnel disappears, never leave a
fallback route through the host.

## Sandbox boundary

Whichever network mode you choose, every run stands on the same layers:

- a transient `systemd-run --user` cgroup capping tasks, memory, swap, and CPU
  across the whole process tree rather than just the first process;
- Bubblewrap user, pid, IPC, UTS, and cgroup namespaces, with nested user
  namespaces disabled and then asserted unavailable;
- a libseccomp deny-list over high-value kernel surfaces: `bpf`,
  io_uring, userfaultfd, perf, ptrace and process-vm, the keyring, and the mount,
  namespace, module-loading, NUMA-policy, and personality calls;
- per-process rlimits on address space, CPU time, file size, and open files, plus
  a hard-zero core dump limit, all applied through `prlimit`;
- a private, size-capped tmpfs at `/tmp`;
- read-only `/usr`, masked system Python package directories, a cleared
  environment, and no home directory, repository checkout, bot environment,
  database, configuration, SSH key, or provider credential mounted anywhere;
- the caller's own workspace as the single writable mount, at `/work`.

The seccomp filter is defense in depth, not the boundary itself. It is a deny
list rather than an allow list because ordinary Python and build workloads touch
a broad and constantly moving set of syscalls, and an allow list tight enough to
be worth having would break them. The boundary rests on the Bubblewrap
namespaces, the cgroup, the mount layout, the privilege drop, and whatever
isolates the host.

The sandbox shares the host kernel and runs under the bot's own uid, so a
successful kernel escape lands in the bot account. For a large or hostile
population, the recommended next boundary is a dedicated VM carrying no
credentials it does not need.

### Core dumps

Linux ignores `RLIMIT_CORE=0` when `kernel.core_pattern` pipes crashes to a host
collector, so the payload's memory can still reach that collector. Kimi
refuses to register or run code while the pattern begins with `|`. Configure a
plain file pattern instead; the hard-zero limit then does its job and no dump
gets written.

On Ubuntu, Apport commonly installs such a piped handler. This can be surprising
after a reboot: the sandbox may have worked before the restart, then disappear
when Apport restores its handler during boot. Disable the crash collector and
set a plain `kernel.core_pattern` persistently in `/etc/sysctl.d/`; changing it
with a one-off `sysctl` command only lasts until something changes it again.
The same host check protects both `run_code` and the persistent browser.

## Why netns uses a transient service

Ordinary `none` and `host` runs use `systemd-run --user --scope`. That breaks
down for netns: a hardened bot service may set `NoNewPrivileges=yes`, which stops
any descendant scope from using `sudo`. Netns runs therefore go through a
transient user *service*, forked by the user's own systemd manager rather than by
the bot:

```text
systemd-run --user --pipe --wait --collect --unit=sandbox-net-<id>
  OpenFile=<runtime bpf file>:seccomp:read-only
  sudo -n -C 4 <root-owned helper>
  prlimit ... bwrap --unshare-all --share-net --seccomp 3 ...
```

The systemd manager opens the seccomp program as fd 3, and `sudo -C 4` is what
keeps that descriptor alive across the privilege boundary. The helper enters the
one namespace whose name is baked into the root-owned file, drops straight back
to the sudo caller's uid and gid, and executes the supplied `prlimit`/Bubblewrap
chain as the bot user.

The namespace name is neither a bot setting nor a sudo argument.
Keeping it out of both means the privileged interface never varies and the
sudoers rule stays scoped to exactly one helper path. See
[the generic provisioning templates](../bot/deploy/code-exec-netns/README.md).

Netns runs serialize on a process-wide lease. Cleanup stops the transient unit
and confirms it is inactive before releasing that lease; if it cannot prove
teardown happened, it poisons the lease and every later netns call fails until
the bot restarts. Failing every later call is the safer choice, since an
unproven teardown means some process may still be inside the namespace.

## Persistent packages and build state

`pip_install` takes up to 16 ordinary requirement strings. Flags, URLs, local
paths, whitespace, and editable installs are all rejected. Installation goes
through argv rather than a shell, but that only removes shell injection: a
package still runs its own build and install machinery inside the sandbox, so
treat it as untrusted code like anything else that runs there.

The tool creates `/work/.venv` with copied files rather than symlinks. A healthy
workspace venv supplies Python and puts `.venv/bin` on `PATH` for later runs,
including `none`-mode runs that use installed packages. `.pio` and
`.pio-core` do the same for regenerable build caches. All three directories:

- have their own byte and entry quotas, separate from the document quota;
- stay hidden from normal listings, archives, artifact diffs, and
  auto-attachment;
- are removed as whole units by quota and TTL cleanup; and
- cannot be written by ordinary workspace tools.

### Workspace accounting during a run

The sandbox performs a full workspace accounting scan before launch, every
`CODE_EXEC_WORKSPACE_QUOTA_POLL_SECONDS` while the process runs, and once more
before returning output. The default in-flight interval is five seconds. A job
may therefore exceed a workspace ceiling briefly, but the final scan prevents
unchecked output from being released.

Package managers and test runners rename and remove files while the scan walks
the tree. An entry disappearing between `scandir` and `stat` is a normal race,
not evidence of quota evasion. `ENOENT` and `ESTALE` scans are retried up to
`CODE_EXEC_WORKSPACE_QUOTA_SCAN_RETRIES` total attempts (four by default, ten
maximum), with a short bounded backoff. Permission failures and other non-transient errors still fail
immediately; a transient error that persists through every attempt also fails
closed. Logs record the affected area, errno, relative path, and attempt count
without exposing the absolute workspace path.

### The shared packages environment

`CODE_EXEC_VENV_DIR` is something else: one packages environment that every
workspace shares. Kimi never writes to it. It is bound read-only into every
run, so `pip_install` from inside a run cannot reach it, and adding a package is
an operator action on the host:

```bash
# once, as the bot user
/usr/bin/python3 -m venv /opt/kimi/code-exec-venv
/opt/kimi/code-exec-venv/bin/pip install numpy pillow
```

Point `CODE_EXEC_VENV_DIR` at that directory and restart. From then on, adding a
package is just another `pip install` into the same venv: the bind is by path, so
the next run sees it without a restart. Only changing the path itself needs one,
because the sandbox profile is built when the tool registers.

Whether that venv works at all comes down to three things:

- **Build it on an interpreter the sandbox mounts.** Apart from the paths you add
  through `CODE_EXEC_EXTRA_RO_BINDS`, `/usr` is the only host filesystem a run
  gets, so the system Python is the safe base. A venv built on a pyenv, uv, or
  Homebrew interpreter under `/opt` or a home directory keeps pointing at that
  base install through its `pyvenv.cfg`, so bind the base interpreter's directory
  in as well, or build against `/usr` instead.
- **`<venv>/bin/python3` has to exist.** Setting `CODE_EXEC_VENV_DIR` makes that
  path the interpreter for every run, and the startup probe checks it. A typo
  leaves `run_code` unregistered rather than quietly falling back to
  `CODE_EXEC_PYTHON_BIN`.
- **It appears at its host path.** The venv is bound at the same absolute path
  inside the sandbox as outside, so a script can reference it directly.

The host's own system package directories are masked with empty read-only mounts,
so this venv is the only preinstalled set of packages a run starts with, until a
workspace grows its own `/work/.venv`, which takes precedence for `python` mode.

In a networked mode this is also the interpreter that has to be able to create
venvs, which is a separate prerequisite. See
[Host requirements](#host-requirements).

Keep it well apart from the bot's own environment, and never put credentials in
it: every user of every workspace can read everything inside.

## Workspace concurrency, quota, and artifacts

A run holds the same per-workspace lock as `write_file`, `edit_file`, imports,
archive extraction, and the other mutating tools. That closes the gap between
resolving a path and writing to it: model tools cannot swap a path for a symlink
while payload code is running.

While a run is active the runner watches ordinary workspace bytes, ordinary entry
count, environment bytes, and environment entry count. Crossing a limit kills the
process tree. Files the run created are then pruned, but documents that existed
beforehand are left alone, because rolling those back without content snapshots
could destroy work the user cares about. Regenerable environment roots are the
exception, and can be removed whole even when they predate the run. Cleanup walks
fd-relative with no-follow opens and verifies directory identity as it goes, so
symlink and rename races cannot steer it outside the owned tree.

Treat the workspace byte and entry ceilings as enforcement and cleanup limits
rather than a filesystem allocation boundary. Kimi polls the writable host
bind while a run is active, and the per-file size rlimit applies to each file
rather than to their sum, so a payload can allocate past the configured total
before the monitor notices and kills it. For untrusted users, put the workspace
tree on its own size-bounded filesystem or enforce an OS filesystem or project
quota on it, and size that hard limit for the concurrency you allow. Keep it
independent of the repository, database, logs, and host root filesystem. Until
that host-level boundary exists, treat code execution as unsafe to expose to a
hostile public population.

After the run, changed files are reported by name, up to fifty of them. If more
than six files changed, Kimi takes that as a sign of a build rather than
artifact production and attaches nothing; the model has to pick the deliverable
with `queue_file`. Below that threshold, files are queued automatically up to the same
`WORKSPACE_TOOL_MAX_ATTACHMENTS` limit every other tool shares, and anything too
large or of an unsupported type comes back marked as skipped rather than quietly
disappearing.

## Weekly network budget

`host` and `netns` runs each reserve one per-user usage marker, taken after
argument and path validation but before any sandbox work starts. The window rolls
over seven days, `CODE_EXEC_NETWORK_WEEKLY_LIMIT` defaults to 100, `0` disables
the cap, and `STAFF` is exempt. A reserved run counts even if the install or the
execution later fails, because it consumed network and build capacity either way.
`none` runs never touch this budget. Markers are stored separately from LLM and
paid-tool spend, so they never appear in cost totals.

When both code execution and the [persistent browser](browser.md) use `netns`,
they share one process-wide lease on the single physical namespace. A rooted
browser turn holds that lease until the turn's finalizer runs, so each surface
checks whether the other has already claimed it within the turn and refuses
rather than blocking on a lease its own turn owns. `CODE_EXEC_NETWORK_MODE=none`
never claims the lease, and is the expected pairing when only browser traffic
goes over the VPN.

## Host requirements

Every mode needs Linux, Bubblewrap, util-linux `prlimit`, libseccomp,
`systemd-run`, a working user systemd manager, unprivileged user namespaces, and
a Bubblewrap that can disable nested user namespaces.

Kimi itself has to run as an unprivileged service account. UID 0 is rejected
by the startup probe and again at execution time, and containers get no
exception: container root is still the wrong account for this boundary. That
account also needs lingering enabled, because otherwise its systemd user manager
and bus are not running when a run needs them. Set both up and check them before starting
Kimi, replacing `kimi` with the real account:

```bash
bot_user=kimi
bot_uid=$(id -u "$bot_user")
sudo loginctl enable-linger "$bot_user"
sudo systemctl start "user@${bot_uid}.service"
sudo -u "$bot_user" env \
  XDG_RUNTIME_DIR="/run/user/${bot_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${bot_uid}/bus" \
  systemctl --user is-system-running
```

That last command should report `running` or `degraded` with the user bus
reachable. Kimi works out those two environment variables for itself at
runtime, but it cannot start a user manager that was never there.

A networked mode has to be able to build a venv with bundled pip, using whichever
interpreter is in play: `CODE_EXEC_VENV_DIR/bin/python3` when the shared
packages environment is configured, `CODE_EXEC_PYTHON_BIN` otherwise. On Debian
and Ubuntu that usually means installing the `python3-venv` package matching that
interpreter's base install. The startup probe builds a throwaway pip-enabled venv
inside the sandbox and leaves `run_code` unregistered if it cannot.

The filesystem holding `WORKSPACE_DIR` has to allow execution, because persistent
`.venv/bin/python3` interpreters and `direct`-mode workspace files run from it. A
`noexec` mount is caught at startup: Kimi writes a mode-0700 probe file into
the real workspace root, checks `X_OK`, removes it again, and leaves `run_code`
unregistered if the check fails.

Netns adds `sudo`, `nsenter`, `setpriv`, a root-owned helper, a narrow sudoers
rule, the provisioned namespace, and a namespace-specific resolver file. The bot
service can keep `NoNewPrivileges=yes`; the transient user service exists to
cross that boundary before the tightly scoped helper drops privileges again.

## Configuration and startup behavior

Every setting and its default lives in [`.env.example`](../bot/.env.example).
These are the ones that decide the shape of a deployment:

- `CODE_EXEC_ENABLED` and `CODE_EXEC_NETWORK_MODE`;
- `CODE_EXEC_PYTHON_BIN`, `CODE_EXEC_VENV_DIR`, and
  `CODE_EXEC_EXTRA_RO_BINDS`;
- `CODE_EXEC_BWRAP_BIN`, `CODE_EXEC_PRLIMIT_BIN`, and
  `CODE_EXEC_SYSTEMD_RUN_BIN`;
- the `CODE_EXEC_MAX_*`, `CODE_EXEC_CPU_QUOTA_PERCENT`,
  `CODE_EXEC_TMP_SIZE_MB`, and `CODE_EXEC_WALL_TIMEOUT_SECONDS` limits;
- `CODE_EXEC_WORKSPACE_QUOTA_POLL_SECONDS` and
  `CODE_EXEC_WORKSPACE_QUOTA_SCAN_RETRIES`;
- `CODE_EXEC_ENV_DIR_MAX_MB` and `CODE_EXEC_ENV_DIR_MAX_FILES`;
- `CODE_EXEC_NETWORK_WEEKLY_LIMIT`; and
- for netns, `CODE_EXEC_SUDO_BIN`, `CODE_EXEC_NETNS_HELPER_BIN`,
  `CODE_EXEC_NETNS_RESOLV_CONF`, and
  `CODE_EXEC_NETWORK_PROBE_BLOCKED_IP`.

An enabled but incomplete netns configuration fails settings validation.
A complete one whose live probe fails leaves `run_code` unregistered, emits a
startup warning naming the mode that failed, and reports the tool as unavailable
in the capability summary.

## Verification and troubleshooting

Before exposing the tool to users:

1. Run the repository CI suite on Linux.
2. Confirm `run_code` appears in the startup capability summary.
3. Run a `none` profile and verify network connections fail.
4. If supporting `host`, verify its egress IP and separately audit every private
   route available to the bot host.
5. If using `netns`, verify the egress IP belongs to the intended tunnel, the
   configured known-open private target is unreachable, and stopping the tunnel
   makes the startup probe fail without falling back to host networking.
6. Confirm a script cannot see the checkout, `.env`, bot database, home
   directory, or another user's workspace.
7. Confirm the CPU, memory, task, timeout, output, tmpfs, and workspace quotas
   terminate adversarial runs and leave no transient unit behind.
8. Fill the workspace filesystem or project quota with an adversarial multi-file
   allocation and confirm the operating system refuses further allocation without
   touching the database, checkout, logs, or host root filesystem.

If registration fails, remember that it fails closed by design, and the cause is
almost always one of these:

- Kimi is running as UID 0;
- a required binary is missing;
- the sandbox interpreter cannot build a pip-enabled venv;
- the workspace mount is `noexec`;
- the systemd user bus is unavailable;
- the core-dump handler is piped;
- libseccomp is absent;
- the kernel or Bubblewrap refuses the user-namespace flags;
- netns files or sudoers permission are missing;
- DNS or TLS egress is broken; or
- a private target has quietly become reachable.

Repair the boundary that failed and restart. Don't route around the probe; it
is telling you the sandbox you configured isn't the one you'd be exposing.
