# Code execution

`run_code` lets Kimi run inline Python, an inline shell script, or a file from the caller's workspace. It only works on Linux and is off by default.

The default access level is `member`. Set `CODE_EXEC_MIN_TIER=regular` or `CODE_EXEC_MIN_TIER=staff` if you want to restrict it. Changing the tier requires a restart.

Turning on `CODE_EXEC_ENABLED` is only a request. Before the tool appears, Kimi runs a real test program through the sandbox and network mode you configured. If that test fails, `run_code` stays unavailable, the startup log warns that the probe failed, and `scripts.sandbox_probe` (see [Check your deployment](#check-your-deployment)) tells you which check broke. Kimi never exposes a half-working sandbox.

Each run is Python, a shell script, or a workspace file run directly (`auto` picks from the file extension). In a networked mode, a run can `pip install` public packages into a `.venv` that stays in the workspace for later runs. Files the run creates also stay in the workspace; the model must name each one it wants to attach with `queue_file`.

The optional [durable coding agent](coding-agent.md) uses the same sandbox for longer jobs. A coding job may get more wall-clock and CPU time, but no extra files, network access, syscalls, quota, or credentials. For both ordinary runs and coding jobs, systemd also gets its own deadline (`RuntimeMaxSec`), so a run that somehow outlives Kimi's timer is still killed.

## Pick a network mode

`CODE_EXEC_NETWORK_MODE` applies to the whole deployment. The model cannot change it for a call.

| Mode | Internet | Private-network protection | Privileged helper |
|---|---|---|---|
| `none` | No | Fresh empty network namespace | No |
| `host` | Yes, through the bot server | None beyond the server's own routing and firewall | No |
| `netns` | Yes, through an operator-provisioned namespace | Must pass a private-target isolation test | Yes |

The tracked defaults are:

```dotenv
CODE_EXEC_ENABLED=false
CODE_EXEC_MIN_TIER=member
CODE_EXEC_NETWORK_MODE=none
```

A fresh checkout therefore has no code execution and no network access from code.

### `none`

Use `none` when code only needs the standard library, an optional read-only package environment, and files already in the workspace. Bubblewrap gives the run an empty network namespace. `pip_install` is rejected.

### `host`

Use `host` only when code needs the internet and you accept the server's network exposure. Traffic leaves from the server's public IP and can reach everything the server can reach, which often includes:

- loopback services;
- LAN, VPN, container, and private-cloud routes;
- link-local services and cloud metadata; and
- other internal services allowed by the host firewall.

The filesystem, process, syscall, resource, and credential protections still apply. They do not make host networking private-safe. The startup test proves DNS and real TLS work, but it cannot prove an internal address is unreachable. If internal routes must stay out of reach, use a dedicated VM or an independently audited egress policy.

### `netns`

Use `netns` when code needs internet access but must not use the server's normal routes. You provide a persistent network namespace with a VPN or another restricted uplink. Kimi does not depend on a particular VPN provider.

Before the tool appears, Kimi launches a test through the real privileged path and checks that:

1. seccomp is installed and a blocked syscall returns `EPERM`;
2. the namespace has a route;
3. DNS works through its resolver;
4. a real TLS connection works; and
5. `CODE_EXEC_NETWORK_PROBE_BLOCKED_IP`, set to a known-open private `host` or `host:port`, is unreachable.

The namespace should block loopback, private, link-local, metadata, and unnecessary outbound services such as SMTP. It must also fail closed. If the tunnel goes down, traffic must not fall back to the host route.

## The sandbox boundary

Every network mode gets the same core protections:

- a transient `systemd-run --user` cgroup limits tasks, memory, swap, and CPU for the whole process tree;
- Bubblewrap creates user, pid, IPC, UTS, and cgroup namespaces, and Kimi checks that code inside cannot create further user namespaces;
- libseccomp blocks high-risk kernel features such as `bpf`, io_uring, userfaultfd, perf, ptrace/process-vm, keyrings, mounts, namespaces, module loading, NUMA policy, and personality calls;
- `prlimit` caps address space, CPU time, file size, open files, and core dumps;
- `/tmp` is a private size-limited tmpfs;
- `/usr` is read-only, system Python package folders are masked, and the environment is cleared; and
- the caller's workspace is the only writable mount, available at `/work`.

The run cannot see a home directory, the repository checkout, the bot environment, database, config, SSH keys, or provider credentials.

Seccomp is extra protection, not the whole boundary. It uses a deny list because Python and build tools need a broad, changing set of syscalls. The real boundary is the combination of namespaces, cgroups, mounts, dropped privileges, and host isolation.

The sandbox still shares the host kernel and runs as the bot's Unix user. A successful kernel escape would land in that account. For a large or hostile user base, put Kimi in a dedicated VM with no credentials it does not need.

### Core dumps

A zero core-dump limit is not enough when `kernel.core_pattern` starts with `|`. In that setup Linux sends crash memory to a host collector anyway. The startup test checks for that pipe and leaves `run_code` unregistered while it is present.

Use a plain file pattern instead. Ubuntu's Apport commonly installs the piped handler and may restore it after a reboot. Disable the collector and set a plain pattern persistently under `/etc/sysctl.d/`. A one-time `sysctl` change may not survive. The same check protects both code execution and the persistent browser.

## Why netns launches differently

`none` and `host` use `systemd-run --user --scope`. That does not work for netns when the main bot service has `NoNewPrivileges=yes`, because no child process of the service can use `sudo`, and entering the namespace needs root.

Netns uses a transient user service started by the user's systemd manager:

```text
systemd-run --user --pipe --wait --collect --unit=sandbox-net-<id>
  OpenFile=<runtime bpf file>:seccomp:read-only
  sudo -n -C 4 <root-owned helper>
  prlimit ... bwrap --unshare-all --share-net --seccomp 3 ...
```

The manager opens the seccomp program as file descriptor 3. `sudo -C 4` keeps it open across the privilege boundary. The root-owned helper enters one fixed namespace, drops back to the caller's uid and gid, then runs the `prlimit` and Bubblewrap chain as the bot user.

The namespace name is not a bot setting or a sudo argument. It is fixed inside the helper, which keeps the privileged interface and sudoers rule narrow. See the [generic provisioning templates](../bot/deploy/code-exec-netns/README.md).

Only one netns run can be active at a time. When it finishes, Kimi stops the transient unit and confirms it is inactive before letting the next run start. If Kimi cannot confirm cleanup finished, it stops accepting netns runs until the bot restarts. That is safer than assuming no process is left in the namespace.

## Packages and build files

### Packages installed by a run

`pip_install` accepts up to 16 ordinary package requirement strings. Flags, URLs, local paths, whitespace, and editable installs are rejected. Installation uses argv instead of a shell, but package build and install code still runs inside the sandbox. Treat packages as untrusted code.

The tool creates `/work/.venv` with copied files instead of symlinks. A healthy workspace venv provides Python and puts `.venv/bin` on `PATH` for later runs, including offline `none` runs. `.pio` and `.pio-core` hold regenerable build caches.

These directories have their own byte and file-count quotas. They stay out of normal listings, archives, and artifact diffs. Quota and TTL cleanup can remove them as complete units. Ordinary workspace tools cannot change them.

### Workspace checks while code runs

Kimi scans the whole workspace before launch, every `CODE_EXEC_WORKSPACE_QUOTA_POLL_SECONDS` during the run, and once more before returning output. The default interval is five seconds. A job may go over a limit briefly, but the final scan stops unchecked output from being returned.

Files can disappear mid-scan while package managers and test runners are working. When a scan hits a file that vanished (`ENOENT` or `ESTALE`), it retries up to `CODE_EXEC_WORKSPACE_QUOTA_SCAN_RETRIES` attempts in total, four by default and ten at most. Permission errors and other non-temporary failures stop the run immediately, and so does a temporary error that survives every retry. The log line names the area, errno, relative path, and attempt count, but never the full workspace path.

### Shared read-only packages

`CODE_EXEC_VENV_DIR` points to one package environment shared by every workspace. Kimi mounts it read-only and never writes to it. Only the operator can add packages:

```bash
# once, as the bot user
/usr/bin/python3 -m venv /opt/kimi/code-exec-venv
/opt/kimi/code-exec-venv/bin/pip install numpy pillow
```

Set `CODE_EXEC_VENV_DIR` to that directory and restart. Later package installs into the same venv are visible on the next run without restarting. Only changing the path needs a restart.

Three details matter:

- Build it with an interpreter the sandbox mounts. `/usr/bin/python3` is the safe default. A venv based on pyenv, uv, or Homebrew may point outside `/usr`; either bind that base path with `CODE_EXEC_EXTRA_RO_BINDS` or use the system Python.
- `<venv>/bin/python3` must exist. A typo leaves `run_code` unavailable instead of falling back to `CODE_EXEC_PYTHON_BIN`.
- The venv appears at the same absolute path inside and outside the sandbox.

Host Python package directories are hidden, so this venv is the only preinstalled package set until a workspace creates `/work/.venv`, which takes priority in Python mode. In a networked mode, the selected interpreter must also be able to create a pip-enabled venv. See [Host requirements](#host-requirements).

Keep the shared venv separate from Kimi's own environment. Never put credentials in it because every code-exec user can read it.

## Workspaces, quotas, and output files

A run holds the same per-workspace lock used by `write_file`, `edit_file`, imports, archive extraction, and other writing tools. Those tools cannot replace a path with a symlink while code is running.

Kimi watches the size and file count of the normal workspace and of the environment folders (`.venv`, `.pio`) separately. Crossing a limit kills the whole process tree. Files the run created are then deleted. Files that existed before the run are left alone, because rolling them back without a snapshot could destroy the user's work. Environment folders can be regenerated, so those may be removed even if they existed before the run. Cleanup opens directories relative to a held file descriptor, never follows symlinks, and re-checks each directory's identity, so a symlink or rename swapped in during cleanup cannot point it outside the workspace.

The workspace limits are monitoring and cleanup limits, not hard disk quotas. Polling is not instant, and the file-size limit applies per file rather than to the total, so a hostile program can write more than the configured total before the monitor kills it.

For untrusted users, put `WORKSPACE_DIR` on its own size-limited filesystem or apply an OS filesystem/project quota. Size that hard limit for your allowed concurrency, and keep it separate from the repository, database, logs, and root filesystem. Without that host-level boundary, do not expose code execution to a hostile public population.

After a run, Kimi reports up to 50 changed file names. Files remain in the workspace and are never attached automatically. The model must select each deliverable with `queue_file`. The result says whether a changed path was already queued and reminds the model when changed files remain unqueued.

## Weekly network limit

Each `host` or `netns` run counts one against the user's weekly allowance. The count is taken after Kimi has validated the arguments and paths but before any sandbox work starts. `CODE_EXEC_NETWORK_WEEKLY_LIMIT` defaults to 100 over a rolling seven-day window. Set it to `0` to disable the limit. `STAFF` is exempt.

A run still counts if installation or execution fails afterwards, because it used network and build capacity. Offline `none` runs never count. This allowance is separate from model and paid-tool costs.

When both code execution and the [persistent browser](browser.md) use `netns`, they share the same VPN namespace and take turns holding it. A browser turn keeps it until the turn finishes. If one of them already holds it within a turn, the other refuses rather than deadlocking the turn by waiting on itself. `CODE_EXEC_NETWORK_MODE=none` never touches the namespace, and is the expected setup when only browser traffic needs the VPN.

## Host requirements

Every mode needs Linux, Bubblewrap, util-linux `prlimit`, libseccomp, `systemd-run`, a working user systemd manager, unprivileged user namespaces, and a Bubblewrap version that can disable nested user namespaces.

Unprivileged user namespaces must keep their capabilities. Ubuntu 24.04 strips them (`kernel.apparmor_restrict_unprivileged_userns=1`) unless the creating binary is confined by an AppArmor profile that grants `userns`, and the `bubblewrap` package ships no such profile. Install the one from `apparmor-profiles` and keep the restriction on:

```bash
sudo apt-get install -y apparmor-profiles
sudo install -m 0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
```

Without it `bwrap` fails with `loopback: Failed RTM_NEWADDR: Operation not permitted` or `Creating new namespace failed`, and `run_code` stays unavailable; `python -m scripts.sandbox_probe` prints the relevant sysctls next to the failure. The CI `sandbox` job installs the same profile and asserts the restriction stays on.

The user bus that `systemd-run --user` connects through comes from `dbus-user-session`; minimal server images may not have it installed.

Kimi must run as a normal Unix user. Root (UID 0) is rejected at startup and again before every run. Root inside a container still counts as root.

The account also needs lingering so its systemd user manager stays available. Replace `kimi` with the real account:

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

The last command should report `running` or `degraded`; either means the user bus is reachable. Kimi can work out those two environment variables on its own, but it cannot start a user manager that does not exist.

A networked mode needs an interpreter that can create a venv with pip. That is `CODE_EXEC_VENV_DIR/bin/python3` when a shared environment is configured, otherwise `CODE_EXEC_PYTHON_BIN`. On Debian and Ubuntu you usually need the matching `python3-venv` package. The startup test builds a disposable venv and leaves `run_code` unavailable if it fails.

`WORKSPACE_DIR` must allow execution because workspace venv interpreters and `direct` files run from there. A `noexec` mount fails the startup test.

Netns also needs `sudo`, `nsenter`, `setpriv`, the root-owned helper, a narrow sudoers rule, the namespace, and its resolver file. The main bot service can keep `NoNewPrivileges=yes`; the transient service crosses the boundary before the helper drops privileges again.

## Configuration and startup

All settings and defaults live in [`.env.example`](../bot/.env.example). The main groups are:

- `CODE_EXEC_ENABLED`, `CODE_EXEC_MIN_TIER`, and `CODE_EXEC_NETWORK_MODE`;
- `CODE_EXEC_PYTHON_BIN`, `CODE_EXEC_VENV_DIR`, and `CODE_EXEC_EXTRA_RO_BINDS`;
- `CODE_EXEC_BWRAP_BIN`, `CODE_EXEC_PRLIMIT_BIN`, and `CODE_EXEC_SYSTEMD_RUN_BIN`;
- `CODE_EXEC_MAX_*`, `CODE_EXEC_CPU_QUOTA_PERCENT`, `CODE_EXEC_TMP_SIZE_MB`, and `CODE_EXEC_WALL_TIMEOUT_SECONDS`;
- `CODE_EXEC_WORKSPACE_QUOTA_POLL_SECONDS` and `CODE_EXEC_WORKSPACE_QUOTA_SCAN_RETRIES`;
- `CODE_EXEC_ENV_DIR_MAX_MB` and `CODE_EXEC_ENV_DIR_MAX_FILES`;
- `CODE_EXEC_NETWORK_WEEKLY_LIMIT`; and
- for netns: `CODE_EXEC_SUDO_BIN`, `CODE_EXEC_NETNS_HELPER_BIN`, `CODE_EXEC_NETNS_RESOLV_CONF`, and `CODE_EXEC_NETWORK_PROBE_BLOCKED_IP`.

A netns configuration with a missing value fails settings validation, so the bot does not start. A complete setup whose live test fails still lets the bot start, but leaves `run_code` unavailable, logs a startup warning naming the failed mode, and marks the tool unavailable in the capability summary.

## Check your deployment

Before exposing the tool:

1. On the host, as the bot user, run the probe with the same `ENV_FILE` the
   service uses:

   ```bash
   .venv/bin/python -m scripts.sandbox_probe
   ```

   It reads `runtime.env` under the config home and the operator `settings.md`
   overlay the same way startup does, builds the sandbox profile from your
   `CODE_EXEC_*` and `WORKSPACE_DIR` settings, and runs the prerequisite checks
   in startup's order. It names the first check that fails. Exit status 0 means
   a jailed process really started with that profile, including the network
   legs of `netns` or `host`.
2. Run the live-jail tests. `KIMI_REQUIRE_SANDBOX_TESTS=1` turns a skipped
   sandbox test into a failure, so a broken sandbox cannot pass quietly:

   ```bash
   KIMI_REQUIRE_SANDBOX_TESTS=1 .venv/bin/python -m pytest -q tests/test_sandbox_required.py tests/test_sandbox_runner.py tests/test_code_exec_tool.py tests/test_skill_sandbox.py
   ```

   CI runs the same job on a provisioned `ubuntu-24.04` runner
   (`.github/workflows/ci.yml`, job `sandbox`).
3. Confirm `run_code` appears in the startup capability summary at the tier you selected.
4. Run an offline profile and confirm network connections fail.
5. If using `host`, verify the public egress IP and audit every private route available to the server.
6. If using `netns`, verify the tunnel's egress IP, confirm the known-open private target is unreachable, and confirm stopping the tunnel makes startup fail without falling back to `host`.
7. Confirm code cannot see the checkout, `.env`, database, home directory, or another user's workspace.
8. Confirm CPU, memory, task, timeout, output, tmpfs, and workspace limits kill hostile runs and leave no transient systemd unit behind.
9. Fill the workspace filesystem or project quota with a multi-file test and confirm the OS refuses more allocation without affecting the database, checkout, logs, or root filesystem.

If registration fails, the usual causes are:

- Kimi is running as UID 0;
- a required program is missing;
- the selected Python cannot build a pip-enabled venv;
- the workspace is mounted `noexec`;
- the systemd user bus is unavailable;
- the core-dump handler uses a pipe;
- libseccomp is missing;
- the kernel or Bubblewrap refuses the user-namespace flags;
- netns files or sudoers permissions are missing;
- DNS or TLS is broken; or
- a private target has become reachable.

Fix the failed check and restart. Do not work around the startup test. It is telling you that the sandbox you configured is not the sandbox Kimi would actually be running code in.