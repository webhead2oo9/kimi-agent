# Deployment files

Host-side pieces applied by an operator when the matching feature is enabled.
[docs/setup.md](../../docs/setup.md) walks a first deployment end to end.

`kimi.service.example` is a public, path-neutral user-service starting point for
the bot process itself. Copy it into the unprivileged bot account's systemd user
directory, replace `/srv/kimi`, and keep the concrete unit and its environment
files in private deployment configuration. Its `TasksMax`, `MemoryMax`, and
`CPUQuota` directives cap the complete service cgroup, including executable
skill descendants; tune them to the host instead of removing the aggregate
backstop.

| Directory | Applies when |
|---|---|
| [`betterwright/`](betterwright/README.md) | The browser tool is enabled. Installs the pinned BetterWright runtime outside the checkout. |
| [`code-exec-netns/`](code-exec-netns/README.md) | Code execution or the browser runs in a fixed VPN namespace. Templates for the privileged seam only. |
| [`hindsight/`](hindsight/README.md) | Long-term memory is enabled. Runs the backend as a container on any Docker host the bot can reach. |
