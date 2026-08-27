# Shared skill stores

`skills/builtin/` contains globally available instruction skills shipped with
the bot. These files are part of the repository, validated at startup, and
read-only through Discord. A built-in skill may contain `SKILL.md` and an
optional `reference/` tree; it cannot declare guild scoping, executable tools,
or secrets. Built-in names are reserved. If the private store contains the same
name, the built-in is used, the private copy is left untouched, and the collision
is logged.

Built-in descriptions and bodies may use the single `{{bot_name}}` placeholder.
The catalog replaces it with the configured, single-line bot name before showing
the skill to the model. Unknown built-in placeholders fail startup validation.
Private skills are loaded literally and receive no substitution.

## Private store

`skills/store/` is deployment-owned runtime data. It is intentionally not part
of the repository, and a clean clone may have no store directory at all. The bot
treats a missing store as an empty skill catalog; the first successful
`skill_create` creates it lazily.

For production, set `SKILLS_DIR` to a durable host path and mount that path into
the bot container or service. Give the bot process read/write access, keep access
limited to the deployment operator, and provision any initial `SKILL.md`,
`reference/`, or `scripts/` files there before startup. Do not rely on ignored
files surviving a checkout replacement, image rebuild, or `git clean`.

Back up the complete configured store, including reference files and scripts,
and test restoration independently of the source checkout. The store may contain
private community knowledge, so backups should receive the same access controls
and retention treatment as other production data.

Discord-created skills are instruction-only,
but an operator can place `tools:` declarations and scripts in the store. Review
those additions like application code before deployment, keep a versioned copy
in a separate private repository or artifact system, grant only required
secrets, and never install scripts sourced from untrusted skill content.

Executable skill support is Linux-only and fail-closed. When a stored skill has
a `tools:` declaration, startup requires an unprivileged service account,
working `bwrap` (Bubblewrap) and `prlimit` (util-linux) binaries, and a namespace
smoke test. UID 0 is rejected. The bot does not register or run the script
without that boundary.

Each invocation gets a new user, mount, PID, IPC, UTS, cgroup, and (normally)
network namespace; all capabilities are dropped and nested user namespaces are
disabled. The skill and interpreter runtime are read-only, `/tmp` is a private
size-capped tmpfs, `/proc` contains only sandbox processes, and one fresh
per-call job directory is bind-mounted read/write at `/workspace`. It is not the
user's whole workspace and does not persist between calls. The host root, other
workspaces, service home, deployment config, `/sys`, and host `/etc` are not
mounted. The PID namespace reaps descendants even if a script starts a new
session.

What the script sees: the skill directory is mounted read-only at `/skill` and
is the working directory; the tool's arguments arrive as one JSON object on
stdin and the result is read from stdout (stderr is captured separately, both
size-capped); the environment is reduced to `PATH`, `TMPDIR=/tmp`,
`HOME`/`XDG_*` pointing at the private tmpfs, `WORKSPACE_DIR=/workspace`, and
the skill's declared secrets by name.

Network is denied by default. A reviewed tool that genuinely needs it must opt
in explicitly:

```yaml
tools:
  - name: fetch_service
    description: Fetch data from the configured service
    availability: search
    script: scripts/fetch.py
    network: true
```

`network: true` shares the service's host network namespace and mounts the
minimal resolver/CA files needed by clients. It is not a destination allowlist:
the script can reach public, private, and loopback services available to the bot
host. Use a controlled egress proxy or service-level firewall when destination
restriction matters.

`prlimit` applies per-process ceilings for virtual memory, CPU time, file size,
open files, process count, and core files; wall time, concurrency, captured
output, output files, and tmpfs have separate caps. These are inherited
per-process limits, not an aggregate cgroup budget. The process-count limit is also
per-real-UID. Executable-skill startup requires the whole bot to run as a
dedicated unprivileged service user. The tracked
[`deploy/kimi.service.example`](../deploy/kimi.service.example) adds
`TasksMax=128`, `MemoryMax=4G`, and `CPUQuota=200%` to the bot's service cgroup,
which bounds the aggregate bot and executable-skill process trees. Tune those
ceilings to the host and deployment concurrency, and apply equivalent container
limits when not using systemd. Service-level egress policy remains necessary
when network-enabled skills need destination restrictions.

This is a hostile-code containment boundary, and Bubblewrap relies on the host
kernel. Review and pin
operator scripts, patch the host, grant only required secrets, and keep the
service boundary as a second layer. A script necessarily sees any declared
secret it is asked to use; it can transform that value to bypass exact-value
output scrubbing, and a network-enabled script can transmit it.
