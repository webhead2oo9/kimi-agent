# BetterWright runtime installer

`install.sh` installs the reviewed BetterWright `1.10.0` npm release and its
managed BetterChromium binary into `/opt/kimi/betterwright`. That directory sits
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

Re-run the installer to repair or reproduce the pinned runtime. Review and bump
`VERSION` explicitly when adopting a later BetterWright release; do not use an
unbounded npm range in production.

The bot does not use BetterWright's credential vault, downloads, live-view
server, cloud providers, or daemon.

Enable lingering for the bot account so its user manager remains available
after logout, along with the transient browser and code-exec units. The unit
file itself is [`../kimi.service.example`](../kimi.service.example).

Run the production boundary without Discord before enabling the service:

```sh
uv run python -m deploy.betterwright.smoke_test
```

The smoke check performs public navigation, verifies profile persistence and
cross-user isolation through real worker switches, removes both synthetic test
profiles, and exits nonzero on any failed runtime or sandbox check.
