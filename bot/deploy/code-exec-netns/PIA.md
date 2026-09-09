# Provider VPN network namespace (PIA example)

This runbook provisions the operator-owned side of Kimi's `netns` network
profile with a commercial VPN tunnel. Private Internet Access (PIA) is the
worked example because it supplies Linux WireGuard peers, but the boundary is
provider-neutral. It applies to the
`run_code` sandbox, durable coding jobs, and the persistent browser. It does not
route the bot process, Discord gateway, model providers, search providers, or
offline visual renderer through PIA.

Read the generic [network namespace boundary](README.md) and
[code-execution threat model](../../../docs/code-exec.md) first. Keep every
live name, address, key, credential, probe target, and installed service outside
the repository.

PIA publishes [manual Linux connection instructions](https://helpdesk.privateinternetaccess.com/hc/en-us/articles/46565791890971-Linux-Manual-Connection-Scripts)
and the source for its [manual connection scripts](https://github.com/pia-foss/manual-connections).
WireGuard is preferable here because the generated configuration is reusable
and the kernel interface can be moved into a persistent network namespace.

## Provider contract

Any replacement provider is suitable when it supplies a Linux-compatible
WireGuard peer or OpenVPN profile and permits the intended automated workload.
The operator-owned namespace service must translate that provider artifact into
the same invariant Kimi consumes: one persistent namespace, one root-controlled
readiness marker, one namespace resolver, no host fallback route, and a fixed
helper that immediately drops back to the bot account.

Provider enrollment, authentication, endpoint discovery, renewal, and tunnel
health belong to the namespace service—not Kimi. Kimi should never receive VPN
credentials or choose a provider, region, endpoint, namespace, or route. A
provider change therefore replaces the private tunnel owner and configuration,
while the helper and Kimi settings remain stable.

Prefer a fresh per-host peer. Confirm account connection limits, automation
terms, DNS behavior, IPv6 support, endpoint rotation, key lifetime, reconnect
behavior, and whether a generated profile remains reusable. OpenVPN can satisfy
the same boundary, but its process must be supervised inside the namespace and
the namespace must still have no non-tunnel default route.

## Record the production design

Before changing a second host, inventory the existing production deployment.
Record configuration shape, not secret values:

```bash
sudo ip netns list
sudo systemctl list-unit-files | grep -Ei 'pia|vpn|netns'
sudo find /etc/systemd/system /etc/netns /etc/sudoers.d /usr/local/sbin \
  -maxdepth 3 -type f -iname '*pia*' -o -iname '*netns*'
sudo ip netns exec <namespace> ip -brief address
sudo ip netns exec <namespace> ip route
sudo ip netns exec <namespace> nft list ruleset
```

Also record the PIA region, interface MTU, DNS address, unit dependency order,
readiness-marker path, helper path, resolver path, and Kimi environment variable
names. Redact WireGuard private keys, PIA credentials/tokens, peer addresses,
and public IPs before putting an inventory in an issue or chat.

Do not copy a live production WireGuard configuration to another host. Enroll
the second host separately so simultaneous connections do not reuse a private
key and tunnel address. Reusing the same PIA account is distinct from reusing a
WireGuard peer configuration.

## Host prerequisites

Install the generic sandbox prerequisites plus `curl`, `jq`, `nftables`, and
`wireguard-tools`. Confirm the bot's systemd user manager works before touching
the tunnel:

```bash
sudo loginctl enable-linger <bot-user>
sudo -u <bot-user> \
  XDG_RUNTIME_DIR=/run/user/<bot-uid> \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<bot-uid>/bus \
  systemctl --user is-system-running
```

Use one fixed namespace name throughout the private deployment. The examples
call it `<namespace>` and the WireGuard link `<interface>` deliberately; do not
add real deployment names to this repository.

Kimi's netns launcher uses sudo's `-C` option and the command-scoped
`closefrom_override` sudoers setting to preserve its seccomp file descriptor.
Confirm the installed sudo implementation supports both. Ubuntu installations
that select `sudo-rs` through alternatives may also have classic sudo installed
as `/usr/bin/sudo.ws`; use that explicit binary for `CODE_EXEC_SUDO_BIN` and
`BROWSER_SUDO_BIN`, validate the rule with its matching `visudo.ws`, and do not
install a rule that the system's active sudo parser rejects.

## Generate a fresh PIA peer

Use PIA's interactive script so credentials are not embedded in a command,
shell history, repository file, or systemd unit. Pin and inspect the exact
upstream revision before running it as root:

```bash
git clone https://github.com/pia-foss/manual-connections.git
cd manual-connections
git checkout <reviewed-commit>
git status --short
sudo env PIA_CONNECT=false \
  PIA_CONF_PATH=/etc/wireguard/<private-config-name>.conf \
  ./run_setup.sh
```

Choose WireGuard and the same region policy used in production unless there is
an operational reason to differ. Do not enable port forwarding; the sandbox
does not accept unsolicited inbound traffic. Ensure the resulting file is
owned by root and readable only by root:

```bash
sudo chown root:root /etc/wireguard/<private-config-name>.conf
sudo chmod 0600 /etc/wireguard/<private-config-name>.conf
```

The PIA script may offer to manage host DNS or IPv6. The sandbox instead uses a
namespace-specific resolver and namespace-local firewall. Do not change the
host's default route or resolver for this deployment.

## Build the namespace fail closed

The persistent root service that owns the namespace must perform these actions
in order:

1. Remove the readiness marker.
2. Create the named network namespace.
3. Create the WireGuard link in the host network namespace, then move it into
   the target namespace. A WireGuard link retains its UDP socket in the network
   namespace where it was created, allowing encrypted transport to use the
   host route while cleartext traffic exists only in the target namespace.
4. Apply the peer configuration, tunnel address, MTU, and default route from
   the freshly generated root-only PIA configuration.
5. Enable only the namespace's own loopback when the persistent browser is in
   scope; BetterWright uses it for its internal bridge. This is not host
   loopback and must have no route or forwarding path to the host namespace.
6. Install the namespace-local firewall before permitting workloads.
7. Verify DNS, HTTPS, PIA egress, and private-target isolation.
8. Create the root-owned readiness marker last.

There must be no veth pair, macvlan, physical interface, or ordinary default
route from the namespace to the host. The WireGuard link is its only egress.
If the tunnel loses its peer or handshake, the link may retain a default route,
but packets cannot fall back to the host network.

Use a namespace-local `nftables` policy with default-drop input and forwarding.
Allow input and output on the namespace-local loopback interface when the
browser is enabled. On the VPN interface, output must reject at least these
destinations before its final allow rule:

- loopback and unspecified ranges;
- RFC1918 private space;
- link-local and CGNAT ranges;
- cloud metadata endpoints;
- the host's public subnet and any production/private routes;
- IPv6, unless the complete tunnel and policy intentionally support it; and
- SMTP submission/delivery ports unless a reviewed workload needs them.

PIA's private DNS address is commonly inside a range otherwise rejected by this
policy. Add one narrow UDP/TCP port 53 exception for the DNS address assigned by
the generated peer configuration, before rejecting private ranges. Do not copy
an example DNS address from documentation.

The root service should use `BindsTo=`/`After=` dependencies appropriate to its
tunnel owner, clean up the readiness marker first on stop, and delete the
namespace on teardown. Configure `Restart=on-failure` and a bounded restart
delay. Never publish readiness merely because a WireGuard interface exists;
require a recent handshake and all isolation probes to pass.

When a dedicated private probe address needs a dummy interface, own the
interface and listening socket in one ordinary long-running service. Making a
systemd socket unit depend on a normal interface service creates a boot ordering
cycle: socket units are implicitly ordered before `sockets.target`, while
normal services start after `basic.target`. Always inspect the first boot
journal rather than treating a successful manual start as proof of persistence.

## Resolver and privileged seam

Create a dedicated root-owned resolver, using the DNS server assigned by PIA:

```bash
sudo install -d -o root -g root -m 0755 /etc/netns/<namespace>
sudoedit /etc/netns/<namespace>/resolv.conf
sudo chown root:root /etc/netns/<namespace>/resolv.conf
sudo chmod 0644 /etc/netns/<namespace>/resolv.conf
```

The file should contain only the namespace DNS policy, for example a
`nameserver` line and conservative timeout options. Kimi bind-mounts this file
as `/etc/resolv.conf` inside the sandbox.

Install the fixed helper and sudoers rule from this directory's templates.
Replace every token, then verify ownership, permissions, absence of symlinks,
and the complete sudo policy:

```bash
sudo visudo -cf /etc/sudoers.d/<private-rule-name>
sudo visudo -c
```

The helper must bake in exactly one namespace and readiness marker. It must not
accept either value as an argument.

## Configure Kimi

Put the following values in the private environment file used by the Kimi
systemd service. The browser and code sandbox may share the same namespace and
lease; Kimi serializes their access.

```dotenv
CODE_EXEC_ENABLED=true
CODE_EXEC_NETWORK_MODE=netns
CODE_EXEC_NETNS_HELPER_BIN=/usr/local/sbin/<private-helper-name>
CODE_EXEC_NETNS_RESOLV_CONF=/etc/netns/<namespace>/resolv.conf
CODE_EXEC_NETWORK_PROBE_BLOCKED_IP=<known-open-private-ip:port>

BROWSER_ENABLED=true
BROWSER_NETWORK_MODE=netns
BROWSER_NETNS_HELPER_BIN=/usr/local/sbin/<private-helper-name>
BROWSER_NETNS_RESOLV_CONF=/etc/netns/<namespace>/resolv.conf
BROWSER_NETWORK_PROBE_BLOCKED_IP=<known-open-private-ip:port>
```

The blocked probe must name a private service known to be listening from the
host. A closed port proves nothing. Never use a production credential-bearing
endpoint merely for this test; a small dedicated TCP listener is preferable.

Order Kimi after the namespace service. The namespace owner must become ready
before Kimi starts, and Kimi should stop or lose its tools when that boundary is
unhealthy. Do not silently switch either mode back to `host`.

## Validate before enabling users

Run every check from the generic [deployment checklist](README.md#provisioning-checklist),
then verify the PIA-specific properties:

```bash
sudo ip netns exec <namespace> wg show <interface>
sudo ip netns exec <namespace> ip route
sudo ip netns exec <namespace> nft list ruleset
sudo ip netns exec <namespace> curl --fail --show-error https://example.com/
sudo ip netns exec <namespace> curl --fail --show-error https://ifconfig.me/ip
```

Confirm the reported public IP is PIA's and differs from the host. Confirm the
known-open private target is unreachable. Then run Kimi's own probe from the
same working directory and environment as its service:

```bash
.venv/bin/python -m scripts.sandbox_probe
KIMI_REQUIRE_SANDBOX_TESTS=1 .venv/bin/python -m pytest -q \
  tests/test_sandbox_required.py tests/test_sandbox_runner.py \
  tests/test_code_exec_tool.py tests/test_skill_sandbox.py
```

Finally, remove the readiness marker or stop the namespace service and prove:

- direct namespace HTTPS no longer works;
- Kimi's startup probe fails closed;
- neither `run_code` nor `browser` falls back to host networking; and
- the host public IP never appears from a sandbox workload.

Restore the service, rerun the probes, and inspect the Kimi capability summary
before making the tools available.

## Rotation and rollback

PIA peer rotation is a boundary change. Generate a new peer into a new root-only
path, stage and test it under a separate interface or maintenance window, then
atomically update the private service configuration. Remove the old peer file
only after the new namespace passes all probes.

For emergency rollback, disable the affected Kimi tools or set code execution
back to the offline `none` mode. Do not use `host` as an automatic fallback.
Remove the readiness marker before stopping or repairing the VPN, and do not
remove the sudoers rule while a Kimi netns workload is still running.
