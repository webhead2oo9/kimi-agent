# Module control plane

The control plane lets the bot owner change Kimi's managed configuration
through reviewed proposals instead of hand-editing files on the host. Trusted
application modules can inspect redacted configuration and propose changes;
only the owner can approve one, and every approval becomes an immutable
revision that can be rolled back. It is off by default.

Two things it is not: a plugin sandbox, and a defense against a malicious
installed Python package. Modules run in-process with the bot's full trust;
the control plane governs *configuration*, not code.

## Enable it

Install the desired module distributions, include `config_admin` in
`KIMI_MODULES`, and set the bootstrap values in the selected environment file:

```dotenv
KIMI_MODULES=community_moderation,image_fingerprints,config_admin
CONTROL_PLANE_ENABLED=true
CONTROL_PLANE_DIR=data/control-plane
CONTROL_PLANE_KEY=<base64-encoded 32-byte random key>
CONTROL_PLANE_AUTO_RESTART=true
```

Generate the key once and back it up separately from the encrypted credential
store:

```console
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Launch through the supervisor instead of invoking `bot.py` directly:

```console
uv run python scripts/control_plane_launcher.py -- python bot.py
```

The launcher starts a pending immutable revision with a handshake. Kimi marks
it healthy only after database initialization, module startup, and Discord
READY initialization complete. A candidate that exits first is rolled back to
the previous active revision. Exit code 75 requests a supervised restart.

## Proposal flow

The `config_admin` tools let staff-tier conversations inspect redacted state,
create proposals, and check proposal status. Tool calls never approve their own
changes. The bot owner reviews and acts through:

- `/proposals list`
- `/proposals show`
- `/proposals approve`
- `/proposals reject`
- `/proposals stage-secret`

Proposals are durable SQLite records with append-only transition events. Each
proposal records the target revision seen during preview; approval makes a
fresh preview and marks the proposal stale if the target changed. Application
is serialized, and a proposal is single-use.

Per-guild module settings use the target `guild:<guild_id>:<module_name>`
and map to `guild-modules/<guild_id>/<module_name>.md`; they are
frontmatter-only, activate live, and each document carries its own
revision so proposals for different modules never collide.

Settings and model changes require restart. Guild, channel, prompt, and tool
documents activate live. Module and plugin settings documents require restart.
All documents are copied into immutable managed revisions and strictly checked
for valid YAML frontmatter before staging. Module and plugin selection is
preflighted against installed code, API versions, dependencies, and advertised
core capabilities.

Secrets are staged only by the owner and stored with AES-256-GCM. Proposals and
configuration documents contain `secret://...` references, never plaintext.
There is intentionally no secret-read API.

## Deliberate exclusions in this cut

- no arbitrary shell commands, package installation, or host provisioning;
- no activation of code that is not already installed;
- no arbitrary filesystem writes outside recognized managed config targets;
- no changes to the control-plane enable flag, root, encryption key, automatic
  restart bootstrap, or base `CONFIG_DIR`;
- no automatic database path or SQLCipher-key migration yet;
- no multi-owner quorum, multi-node coordination, or untrusted-module sandbox.

Provider endpoints, credential references, module/plugin activation, sandbox
settings, and network-boundary settings are allowed only as typed Kimi settings
or validated model configuration and still require explicit owner approval.
They are not general command or environment-variable mutation surfaces.
