# Use a PIA VPN for code execution and browsing

Use this guide when you want Bram's code execution or browser traffic to leave
through Private Internet Access (PIA). Discord and model-provider traffic keep
using the server's normal connection.

Bram connects to an existing Linux network namespace: an isolated network with
its own routes and DNS. The repository supplies the helper and sudo rule that
let Bram enter it. It does **not** supply a PIA namespace installer or a service
that maintains the tunnel. You need that host setup before enabling this mode.
Installing PIA on the host alone does not satisfy this requirement.

## Before you start

You need:

- A Linux server with systemd and administrator access.
- Bram running as a dedicated non-root account, with the
  [sandbox host prerequisites](code-exec.md#host-requirements) working.
- A PIA account and a separately enrolled WireGuard peer for this server.
- A service that creates and maintains the VPN namespace, including its
  firewall, DNS, and readiness file.
- A private TCP service that is reachable from the host, for testing that the
  sandbox cannot reach private networks.

If you are still setting up the namespace service, complete
[Prepare the VPN namespace](#prepare-the-vpn-namespace) first. The remaining
steps connect that service to Bram.

Keep credentials, generated WireGuard configuration, and installed host files
outside the checkout. Use your private deployment notes for real host values.

## 1. Collect your deployment values

Write down these values once. Commands below use example names; substitute
your installation's values consistently.

| Item | Example | Used by |
|---|---|---|
| Bot account | `bram` | sudo rule and service commands |
| Namespace | `bram-vpn` | VPN service and helper |
| VPN interface inside it | `wg0` | tunnel checks |
| Readiness file | `/run/bram-vpn.ready` | VPN service and helper |
| Helper installation | `/usr/local/sbin/bram-netns` | sudo rule and Bram settings |
| Namespace resolver | `/etc/netns/bram-vpn/resolv.conf` | Bram settings |
| Private test target | An actual listening private IP and TCP port | startup isolation probe |

The readiness file means the service has checked the tunnel, DNS, firewall,
and private-network isolation. Creating the file manually bypasses those checks.

## 2. Check the VPN before configuring Bram

Run these as an administrator, using your namespace and interface names:

```bash
sudo ip netns exec bram-vpn wg show wg0
sudo ip netns exec bram-vpn ip route
sudo ip netns exec bram-vpn nft list ruleset
sudo ip netns exec bram-vpn curl --fail --show-error https://example.com/
sudo ip netns exec bram-vpn curl --fail --show-error https://ifconfig.me/ip
```

Check that the tunnel has a recent handshake after traffic, HTTPS works, and
the reported public IP belongs to the VPN rather than the host connection.
The namespace's only internet route must use the VPN interface.

Also test your private target: it must accept a connection from the host and
be unreachable from the namespace. A closed port is not a valid isolation test.
Use a dedicated test listener rather than a production service with credentials.

Do not continue if these checks fail. Bram cannot repair the tunnel or firewall.

## 3. Install Bram's helper and sudo rule

The files to copy are in
[`bot/deploy/code-exec-netns/`](../bot/deploy/code-exec-netns/README.md):

- [`code-exec-netns-helper.template`](../bot/deploy/code-exec-netns/code-exec-netns-helper.template)
- [`sudoers-code-exec-netns.template`](../bot/deploy/code-exec-netns/sudoers-code-exec-netns.template)

Make private working copies outside the checkout and replace the tokens:

| Template token | Replacement for the examples above |
|---|---|
| REPLACE_NETNS_NAME | `bram-vpn` |
| REPLACE_READY_FILE | `bram-vpn.ready` |
| REPLACE_ABSOLUTE_HELPER_PATH | `usr/local/sbin/bram-netns` (the template already supplies `/`) |
| REPLACE_BOT_USER | `bram` |

Install the edited helper as `/usr/local/sbin/bram-netns`, owned by `root:root`
with mode `0755`. Install the edited sudo rule as
`/etc/sudoers.d/bram-netns`, owned by `root:root` with mode `0440`. Validate the
edited rule with `visudo -cf` before installing it, then check the installed
configuration:

```bash
sudo visudo -cf /etc/sudoers.d/bram-netns
sudo visudo -c
```

Both checks must succeed. The rule must allow only the installed helper.
The helper enters the fixed namespace and drops back to the bot account before
running a command. Keep that privilege drop intact.

The helper and resolver must be real files, owned by root, with no group or
other write permission. Their parent directories must also be root-controlled.
Bram rejects symlinks for these files.

## 4. Enable the tools you need

Edit the private configuration used by your Bram service. For code execution:

```dotenv
CODE_EXEC_ENABLED=true
CODE_EXEC_NETWORK_MODE=netns
CODE_EXEC_NETNS_HELPER_BIN=/usr/local/sbin/bram-netns
CODE_EXEC_NETNS_RESOLV_CONF=/etc/netns/bram-vpn/resolv.conf
CODE_EXEC_NETWORK_PROBE_BLOCKED_IP=REPLACE_WITH_PRIVATE_IP:PORT
```

For the persistent browser:

```dotenv
BROWSER_ENABLED=true
BROWSER_NETWORK_MODE=netns
BROWSER_NETNS_HELPER_BIN=/usr/local/sbin/bram-netns
BROWSER_NETNS_RESOLV_CONF=/etc/netns/bram-vpn/resolv.conf
BROWSER_NETWORK_PROBE_BLOCKED_IP=REPLACE_WITH_PRIVATE_IP:PORT
```

Replace the test-target value before restarting. Enable either tool or both.
When both use the same namespace, Bram serializes their access. You can leave
code execution in offline `none` mode when only the browser needs the VPN.

## 5. Verify Bram and restart

From `bot/`, run the probe as the **bot account**, using the same configuration
and environment as the running service:

```bash
.venv/bin/python -m scripts.sandbox_probe
```

This checks the code-execution profile; exit status `0` means its sandbox
started and the configured network checks passed. For a browser-only setup,
check the browser startup probe and capability summary after restart.

Restart your Bram service using your normal service-management command. Confirm
the enabled tools appear as available in its startup capability summary. A
failed live probe can leave the bot online while the affected tool is unavailable.

During a maintenance window, stop the VPN namespace service and verify that
VPN traffic fails and Bram cannot start a new workload through it. The service
must remove its readiness file on stop. Restart the VPN service, repeat the
checks, and restart Bram before making the tools available again.

Finally, reboot once and repeat the checks. A successful manual start does not
prove that the VPN starts before Bram at boot.

## Troubleshooting

| Symptom | Check |
|---|---|
| No handshake or HTTPS fails in step 2 | VPN service logs, peer configuration, endpoint reachability, routes, and namespace firewall. |
| DNS fails but IP connectivity works | The namespace resolver and firewall exception for the peer's assigned DNS server. |
| Helper says the namespace is not ready | VPN service status and readiness-file ownership. Let the service publish readiness after its checks pass. |
| Bram rejects a helper or resolver | Absolute path, root ownership, write permissions, symlinks, and parent directories. |
| sudo rejects `-C` or `closefrom_override` | Bram needs sudo support for preserving its seccomp file descriptor. Some Ubuntu hosts use `sudo-rs`; if classic sudo is installed as `/usr/bin/sudo.ws`, configure `CODE_EXEC_SUDO_BIN` and `BROWSER_SUDO_BIN` to that path and validate with the matching `visudo.ws`. |
| User systemd manager is unavailable | Follow the lingering and user-manager commands in [host requirements](code-exec.md#host-requirements). |
| Private-target check fails | Confirm the target is listening from the host and blocked from the namespace. |
| Works manually but fails after reboot | VPN service ordering, readiness checks, and boot logs. |
| Tool stays unavailable after repairing the VPN | Repeat the probe and restart Bram; uncertain workload cleanup can require a restart. |

## Disable or rotate the VPN

To stop using the affected tools, set `CODE_EXEC_ENABLED=false` and/or
`BROWSER_ENABLED=false`, then restart Bram. To keep offline code execution,
set `CODE_EXEC_NETWORK_MODE=none` instead. Switching to `host` would give
workloads the server's normal network access.

For peer rotation, enroll a new peer into a new private configuration file.
Test it during maintenance, update the VPN service, and repeat steps 2 and 5.
Keep the previous configuration until the new tunnel passes. Remove obsolete
sudo rules only after workloads using them have stopped.

## Prepare the VPN namespace

This section is the host setup checklist for whoever maintains your VPN service.
There is no ready-to-install namespace service in this repository.

Obtain a fresh WireGuard peer using PIA's
[manual connection scripts](https://github.com/pia-foss/manual-connections).
Follow that project's current setup instructions and inspect the revision you
run as root. Keep the generated file root-owned with mode `0600`. Enroll each
host separately; sharing an account does not require sharing a tunnel key.

Your namespace service needs to:

1. Remove its readiness file before starting or stopping the tunnel.
2. Create a persistent namespace. Create the WireGuard interface in the host
   namespace and move it into the VPN namespace, so encrypted transport can
   use the host connection.
3. Apply the peer's tunnel address, MTU, configuration, and default route.
   Provide no veth or other fallback route to the host.
4. Install the assigned DNS resolver under `/etc/netns/NAME/resolv.conf`.
5. Apply a namespace firewall that blocks private, link-local, CGNAT, metadata,
   host/public-subnet, and other deployment-private destinations. Block IPv6
   unless the tunnel and firewall both support it. Permit only necessary
   outbound services; block SMTP unless explicitly needed.
6. Allow the assigned DNS server narrowly on TCP/UDP port 53 before blocking
   private ranges. Allow namespace-local loopback for the browser's internal
   bridge, without providing a route to host loopback.
7. Check handshake, DNS, HTTPS, VPN egress, and private-target isolation, then
   create the root-owned readiness file in a root-controlled directory.
8. Supervise failures, remove readiness on teardown, and start before Bram.

Use default-drop input and forwarding rules. The namespace must lose internet
access when the tunnel fails. If a dedicated private probe listener needs a
dummy interface, manage both in a long-running service; a socket unit depending
on a normal interface service can create a systemd boot-ordering cycle.

For the underlying sandbox behavior and additional deployment checks, see
[Code execution](code-exec.md) and [Browser](browser.md).
