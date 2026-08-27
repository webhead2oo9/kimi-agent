# BetterWright runtime installer

`install.sh` installs the reviewed BetterWright `1.10.0` and Mermaid `11.17.2`
npm releases plus the managed BetterChromium binary into
`/opt/kimi/betterwright`. The committed `package-lock.json` is the complete npm
input. That directory sits
deliberately outside the bot checkout: root-owned, and not writable by the bot
account. It remains readable and traversable by the unprivileged bot account so
that account can execute the immutable Node and BetterChromium files.

On the Linux host, install Node `>=22.18.0`, npm, Bubblewrap, util-linux, and a
working per-user systemd manager. Then run:

```sh
sudo apt-get install bubblewrap util-linux libatk1.0-0t64 \
  libatk-bridge2.0-0t64 libcups2t64 libasound2t64 libxdamage1 \
  libatspi2.0-0t64
```

The `t64` suffix is used by current Ubuntu/Debian releases; older distributions
may provide the same libraries without that suffix. The installer checks the
BetterChromium binary with `ldd` and fails with the exact missing library names.
Then run:

```sh
sudo sh ./deploy/betterwright/install.sh
```

If a suitable Node/npm installation is outside `/usr/bin`, pass the reviewed
absolute binaries explicitly instead of replacing the system Node installation:

```sh
sudo env NODE_BIN=/absolute/path/to/node NPM_BIN=/absolute/path/to/npm \
  PATH=/absolute/path/to/node-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  sh ./deploy/betterwright/install.sh /opt/kimi/betterwright
```

Re-run the installer to repair or reproduce the pinned runtime. It runs `npm
ci` in a sibling staging tree, verifies exact package versions, the Mermaid
browser bundle, BetterWright import, BetterChromium binary/shared libraries, and
permissions, then atomically renames the completed tree into place. A failed run
leaves the previous runtime available.

Review and bump `VERSION` or `MERMAID_VERSION` explicitly when adopting a later
release, update `package.json` and `package-lock.json` together, and do not use
an unbounded npm range in production. npm and network access are install-time
requirements only; browser and visual-rendering calls do not install packages
or load Mermaid from a CDN.

The bot does not use BetterWright's credential vault, downloads, live-view
server, cloud providers, or daemon. `render_visual` reuses the immutable files
but launches a separate one-shot, offline worker with no persistent profile or
VPN lease; see [Visual rendering](../../../docs/visual-rendering.md).

Enable lingering for the bot account so its user manager remains available
after logout, along with the transient browser and code-exec units. The unit
file itself is [`../kimi.service.example`](../kimi.service.example).

Run the production boundary without Discord before enabling the service:

```sh
uv run python -m deploy.betterwright.smoke_test
```

The smoke check performs public navigation, verifies profile persistence and
cross-user isolation through real worker switches, renders and host-validates a
chart and Mermaid diagram, removes its synthetic profiles and outputs, and
exits nonzero on any failed runtime or sandbox check.
