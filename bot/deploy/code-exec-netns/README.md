# Generic code-exec network namespace templates

These templates show the privileged seam required by `CODE_EXEC_NETWORK_MODE=netns`.
They do not create a VPN, firewall, resolver, or namespace: those details are
provider- and host-specific and remain operator-owned. Read
[`docs/code-exec.md`](../../../docs/code-exec.md) before installing anything.

## Boundary

The helper accepts the sandbox command to execute, but it accepts **no namespace
selector**. The namespace path and readiness marker are baked into the root-owned
file. It enters that one namespace as root and immediately drops back to the
invoking sudo user's nonzero uid/gid before executing `prlimit` and Bubblewrap.
It rejects a root sudo caller rather than letting a deployment mistake turn the
privilege drop into a no-op.

The sudo rule therefore grants processes already running as the bot account one
ability: enter the preselected namespace and run a command as that same account.
It must not grant arbitrary root commands, accept a model-controlled namespace,
or omit the privilege drop.

## Provisioning checklist

1. Satisfy the generic host prerequisites before anything netns-specific:
   - run Kimi under a dedicated non-root service account, with lingering
     enabled and its `systemctl --user` manager reachable. The commands are in
     [`docs/code-exec.md`](../../../docs/code-exec.md#host-requirements);
   - keep `WORKSPACE_DIR` on an exec-capable filesystem; and
   - install the sandbox interpreter's venv support (`python3-venv` on
     Debian/Ubuntu), so the network probe can build a pip-enabled workspace venv.
2. Create a persistent network namespace with the intended VPN/constrained
   uplink. Give it no host fallback route.
3. Apply a namespace-local firewall that rejects loopback, RFC1918/private,
   link-local, CGNAT, metadata, IPv6 unless intentionally supported, and outbound
   services the deployment does not need (commonly SMTP).
4. Create its resolver file outside the repository, for example under
   `/etc/netns/<name>/resolv.conf`.
5. Publish the readiness marker last, once routing, firewall, DNS, TLS egress,
   and private-target isolation all check out. It must be a real file rather than
   a symlink, root-owned, with no group or other write bits, sitting directly
   under a root-controlled directory. Remove it before teardown.
6. Copy `code-exec-netns-helper.template`, replace all `REPLACE_*` tokens, inspect
   it, and install it `root:root` mode `0755` at a stable absolute path.
7. Copy `sudoers-code-exec-netns.template`, replace the bot user and helper path,
   install it `root:root` mode `0440` under `/etc/sudoers.d/`, and validate it
   with `visudo -cf <path>`.
8. Set `CODE_EXEC_NETNS_HELPER_BIN`, `CODE_EXEC_NETNS_RESOLV_CONF`, and
   `CODE_EXEC_NETWORK_PROBE_BLOCKED_IP` in private deployment configuration.
9. Restart and confirm the complete startup probe succeeds.

Startup rejects helpers and resolver files that are symlinks, not root-owned,
group/other-writable, or reached through a non-root-controlled parent directory.

Use a known-open private service for `CODE_EXEC_NETWORK_PROBE_BLOCKED_IP`.
Testing a closed port cannot distinguish real isolation from an ordinary
connection refusal.

## Updating and rollback

Stage helper and sudoers changes under new paths, validate them, then update the
private environment and restart. To disable immediately, set
`CODE_EXEC_ENABLED=false` or `CODE_EXEC_NETWORK_MODE=none` and restart. Remove a
sudoers entry only after no running bot process uses it; validate the remaining
sudoers configuration afterward.

Never commit the installed helper, real namespace name, readiness path, resolver,
private probe target, VPN credentials, tunnel keys, or host service details.
