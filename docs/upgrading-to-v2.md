# Upgrading from v1 to v2

Version 2 is an intentional compatibility reset. It removes short-lived core,
plugin, and module shims instead of carrying them indefinitely. A fresh install
needs none of this page. An existing installation should complete the sequence
below once, in order.

The important boundary is the database: v2 creates fresh databases directly at
schema v7 and upgrades existing schema-v6 databases to v7. It does not contain
the older v1-through-v6 migration chain. Do not point v2 at a database older
than v6 and do not edit the schema ledger by hand.

Three deliberately temporary retirement guards remain in v2.0: rejection of
the two removed environment names, the single v6-to-v7 database migration, and
reconciliation of a v1 hidden result marker when Discord accepted a terminal
coding report just before the bot crashed. The last guard prevents the first v2
delivery retry from posting that report twice. These guards do not otherwise
emulate v1 behavior. Remove them in v2.1 after every known deployment has
completed this runbook once and reports schema v7; that is their explicit sunset
rather than an open-ended compatibility promise.

The command blocks assume a GNU/Linux host and Bash, matching the supported
systemd user-service deployment.

## 1. Quiesce, back up, and finish the v1 side

Before changing code, quiesce durable coding work while v1 is still running.
Ask each user with active work to run `/stop scope:all`, or let their queued and
running tasks reach a final state. This is not a database-migration
requirement, but it avoids carrying a provider checkpoint or an interrupted
Discord-delivery edge across the reset. If a v1 final result already reached
Discord but its database acknowledgement did not, v2 recognizes the persisted
hidden marker and records that delivery instead of posting a duplicate. V2 does
not assign a default trust tier
to a v1 coding checkpoint that lacks one, so such an active task cannot be
recovered or delivered after the upgrade.

Stop the service and take the rollback backup before running any core or module
bridge migration:

```sh
systemctl --user stop kimi-agent.service
umask 077
backup_dir="$HOME/kimi-agent-backups/v1-before-v2-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$backup_dir"
backup_items=()
for item in \
  .config/kimi-agent \
  .config/systemd/user/kimi-agent.service \
  .config/systemd/user/kimi-agent.service.d \
  .local/share/kimi-agent \
  .cache/kimi-agent \
  .local/state/kimi-agent
do
  if [[ -e "$HOME/$item" ]]; then
    backup_items+=("$item")
  fi
done
if (( ${#backup_items[@]} == 0 )); then
  echo "No standard deployment state found; identify custom paths before continuing" >&2
  exit 1
fi
tar -C "$HOME" -czf "$backup_dir/private-state.tar.gz" "${backup_items[@]}"
chmod 600 "$backup_dir/private-state.tar.gz"
```

The archive covers the standard configuration, main service unit, optional
unit drop-ins, and complete data directory, including SQLite WAL sidecars. It
also works for SQLCipher deployments because no generic SQLite client opens the
files. Check the active `CONFIG_DIR`, `DATABASE_PATH`, `WORKSPACE_DIR`,
`ATTACHMENT_STORE_DIR`, `SKILLS_DIR`, `PERSONAL_SKILLS_DIR`,
`BROWSER_PROFILES_DIR`, and credential/log paths. Copy any value outside those
standard trees—plus deployment-owned module packages and generated service
sources—into the protected backup separately, recording its original path and
permissions. The generic command cannot safely guess custom locations.

Keep this backup until v2 has run long enough to cover the normal workload. A
code rollback after schema v7 is not valid by itself: stop v2, restore this
backup, and reinstall the matching v1 code and extension packages.

With v1 still installed, perform one short maintenance boot. Scope the schema
proof to this boot; an old journal line from another database is not evidence.
The loop stops the service as soon as initialization reports its schema:

```sh
v1_boot_since="$(date --iso-8601=seconds)"
v1_schema_line=""
systemctl --user start kimi-agent.service
for _ in {1..120}; do
  v1_schema_line="$(
    journalctl --user -u kimi-agent.service --since "$v1_boot_since" --no-pager \
      | grep 'Database ready.*schema v' | tail -n 1
  )"
  [[ -n "$v1_schema_line" ]] && break
  sleep 1
done
systemctl --user stop kimi-agent.service
printf '%s\n' "${v1_schema_line:-no database-ready line from this boot}"
```

Continue only when that prints `schema v6`. Otherwise install and run the
audited bridge revision `dfd01ce006d0553c8960de0760fcb5136300c718` against the
backup-protected deployment, then repeat the timestamp-scoped check. That
revision is the repository state immediately before the v2 reset. A v1-v5
database is deliberately rejected by v2 with the bridge revision in the error;
v2 does not retain the old migration chain.

For a source checkout, this is the concrete bridge sequence. Start with a
clean checkout on its normal deployment branch and the stopped-state archive
above. Fetch the audited `main` history explicitly; an older or shallow
deployment clone may not contain the bridge object yet:

```sh
cd "$HOME/kimi-agent"
deployment_branch="$(git branch --show-current)"
test -n "$deployment_branch"
printf 'Return branch: %s\n' "$deployment_branch"
test -z "$(git status --porcelain)"
bridge_revision=dfd01ce006d0553c8960de0760fcb5136300c718
git fetch origin main
if [[ "$(git rev-parse --is-shallow-repository)" == true ]]; then
  git fetch --unshallow origin main
fi
git cat-file -e "${bridge_revision}^{commit}"
git merge-base --is-ancestor "$bridge_revision" FETCH_HEAD
git switch --detach "$bridge_revision"

cd bot
uv sync --locked
.venv/bin/python -m ensurepip
.venv/bin/python -m pip install --no-deps \
  --editable ./packages/kimi-agent-module-api --editable .
# Reinstall any enabled modules' v1 bridge releases into this environment.
```

Now repeat the timestamp-scoped maintenance-boot block above and require a new
`schema v6` line. With the bridge stopped again, return to the deployment
branch (replace `main` below if the recorded branch was different):

```sh
cd "$HOME/kimi-agent"
git switch main
```

Confirm the schema-v6 ready line before stopping the bridge. The final
`git switch` is required: leaving the checkout detached makes the later
`git pull --ff-only` fail. If `uv` is unavailable, use the `venv`/`pip` fallback
shown in step 4 instead.

After confirming v6, stop the service:

```sh
systemctl --user stop kimi-agent.service
systemctl --user is-active kimi-agent.service
```

The second command should print `inactive`.

Active video-analysis sessions do not survive the v7 migration. Their local
rows are removed and their known Gemini Interaction and File identifiers are
queued for provider deletion. Tell users to start a new video session after the
upgrade.

The migration also permanently drops the retired `control_proposals` and
`control_proposal_events` tables. No v2 feature reads them. The rollback archive
retains their historical contents; export them separately before upgrading if
you need that history in another system.

## 2. Prepare deployment-owned extensions

Inventory the active service environment paths and inspect only the extension
selectors, without printing the rest of a secret environment file. The two
files below are installer defaults; also inspect every different `ENV_FILE` or
`EnvironmentFile` path reported by systemd and the configuration system that
generates it.

```sh
systemctl --user show kimi-agent.service -p EnvironmentFiles --no-pager
systemctl --user show kimi-agent.service -p Environment --value \
  | python3 -c 'import shlex, sys; wanted={"ENV_FILE", "RUNTIME_ENV", "KIMI_CONFIG_HOME", "PLUGIN_MODULES", "KIMI_MODULES"}; print("\n".join(item for item in shlex.split(sys.stdin.read()) if item.partition("=")[0].upper() in wanted))'
systemctl --user show-environment \
  | grep -iE '^(ENV_FILE|RUNTIME_ENV|KIMI_CONFIG_HOME|PLUGIN_MODULES|KIMI_MODULES)=' \
  || true
grep -iE '^(PLUGIN_MODULES|KIMI_MODULES)=' \
  "$HOME/.config/kimi-agent/kimi.env" \
  "$HOME/.config/kimi-agent/runtime.env" 2>/dev/null || true
```

Skip the extension subsections only when both values are empty in every active
source.

### Application modules

Every enabled module needs a v2-compatible release. That release must:

- depend on `kimi-agent-module-api>=2,<3` and declare the required keyword
  `api_version=2` as a literal, not `MODULE_API_VERSION`;
- define `scoped_migrations`, using an empty tuple when it has none;
- stop reading `GuildSettingsSnapshot.legacy`;
- import low-level port protocols from `kimi_agent_module_api.contracts` if it
  annotates them; and
- use only the physical names returned by `ctx.storage.table()`. Module table
  aliases no longer exist.

If a module currently declares `table_aliases`, migrate its physical tables
before changing the core. Publish or install a bridge release that still runs
on v1, removes the aliases, and appends a module migration which renames each
old physical table to the normal `<module>_<logical>` name. Start v1 once with
that bridge release, verify `/modules status`, and stop the service again before
changing code or the virtual environment. Once v2 is installed, the old alias
declaration is no longer available and is too late to perform that transition
implicitly.

Move each module's old guild keys out of
`<CONFIG_DIR>/servers/<guild_id>.md` and into the module-owned document:

```text
<CONFIG_DIR>/guild-modules/<guild_id>/<module_name>.md
```

Copy only fields declared by that module's guild-settings schema. The document
is frontmatter-only. Leaving the old keys in the server document is harmless to
v2 core, but removing them avoids misleading future operators. A missing new
document now uses defaults only for fields that declare them; required fields
make that module's guild settings invalid. There is no legacy lookup.

Do not start v2 with an enabled module until its v2 package is installed. A
missing or incompatible enabled module intentionally aborts startup.

### Operator plugins

Every selected plugin module must declare:

```python
PLUGIN_API_VERSION = 2
```

A missing declaration is no longer treated as an old plugin. The loader skips
it with a warning. If a plugin has its own `BaseSettings` model, it must expose
a `PLUGIN_SETTINGS` declaration and obtain the loader-prepared instance through
`ctx.settings_for(...)`; direct-context settings construction is not a fallback
in v2.

Plugins are soft-fail extensions, so one incompatible plugin is skipped rather
than aborting the process. Still verify the `Plugin registered:` lines after
startup so a missing private capability is not mistaken for success.

## 3. Review removed environment settings

`CODEX_MODEL` has been removed. Delete it from the environment and configure
every Codex model in `config/models.yaml`; the selected role's catalog entry is
now the only model identity passed to the Codex transport. The remaining
`CODEX_*` values are transport and authentication controls and are unchanged.

`DISCORD_SEARCH_CHANNELS` has been removed. First identify the environment
sources actually used by the service (including unit drop-ins), then remove
either deleted setting from every dotenv file, service declaration, and
deployment-managed secret source:

```sh
systemctl --user show kimi-agent.service -p EnvironmentFiles --no-pager
systemctl --user show kimi-agent.service -p Environment --value \
  | python3 -c 'import shlex, sys; retired={"CODEX_MODEL", "DISCORD_SEARCH_CHANNELS"}; wanted=retired | {"ENV_FILE", "RUNTIME_ENV", "KIMI_CONFIG_HOME"}; values=(item.partition("=") for item in shlex.split(sys.stdin.read())); print("\n".join(f"{name}=<set>" if name.upper() in retired else f"{name}={value}" for name, sep, value in values if sep and name.upper() in wanted))'
systemctl --user show-environment \
  | grep -iE '^(CODEX_MODEL|DISCORD_SEARCH_CHANNELS|ENV_FILE|RUNTIME_ENV|KIMI_CONFIG_HOME)=' \
  || true
grep -inE '^(CODEX_MODEL|DISCORD_SEARCH_CHANNELS)=' \
  "$HOME/.config/kimi-agent/kimi.env" \
  "$HOME/.config/kimi-agent/runtime.env" 2>/dev/null || true
grep -inE '^[[:space:]]*(codex_model|discord_search_channels):' \
  "$HOME/.config/kimi-agent/config/settings.md" 2>/dev/null || true
```

The file paths above are only the installer defaults. Follow any different
`ENV_FILE` or `EnvironmentFile` paths printed by the first two commands, check
whatever secret/configuration system generates them, and inspect `settings.md`
under every non-default `CONFIG_DIR`. Delete the lowercase frontmatter keys as
well as uppercase environment assignments; v2 rejects them as unknown operator
settings.

Discord text search now builds its scope from channels both the caller and bot
can view and whose history they can read. Use Discord permissions as the
positive boundary and `DISCORD_SEARCH_EXCLUDED_CHANNELS` for explicit denials.
Review those permissions and exclusions before re-enabling search; an old
allowlist cannot be translated mechanically into a deny list.

If either retired variable remains in an active environment source, startup
fails and names it. This is a safety gate, not a compatibility path: v2 never
interprets either value and does not preserve the old search allowlist
behavior.

If video understanding was configured before model-catalog routing, remove the
old `model:` key from `<CONFIG_DIR>/tools/video.md`. Configure the specialist
only through a `roles.video` entry in `config/models.yaml`; an unknown tool
field fails the strict fragment load.

## 4. Install v2

Update the clean checkout and rebuild the environment from the repository lock:

```sh
cd "$HOME/kimi-agent"
git status --short --branch
git pull --ff-only

cd bot
if command -v uv >/dev/null 2>&1; then
  uv sync --locked
  .venv/bin/python -m ensurepip
  .venv/bin/python -m pip install --no-deps \
    --editable ./packages/kimi-agent-module-api --editable .
else
  python3 -m venv .venv
  .venv/bin/python -m pip install \
    --editable ./packages/kimi-agent-module-api --editable .
fi
```

Reinstall each deployment-owned module's v2 release into this exact
environment. For example:

```sh
.venv/bin/python -m pip install --no-deps \
  --editable "$HOME/kimi-agent-discord-logging"
```

Reinstall every deployment-owned plugin package into this exact environment as
well. `uv sync` is allowed to remove packages that are not in the repository
lock, so a plugin merely remaining in its source checkout is not sufficient.
Use the same `.venv/bin/python -m pip install ...` form, substituting the plugin
package path or v2 release artifact.

The BetterWright runtime remains fixed at `/opt/kimi/betterwright`. If your
automation previously passed an installation path, remove the argument. The
supported invocation is:

```sh
sudo sh ./deploy/betterwright/install.sh
```

Reinstalling BetterWright is needed only when that runtime's lock or deployment
files changed, not merely because the core major version changed.

## 5. Migrate, verify, and reopen service

Run preflight against the same profile paths reported by the service, start
once, and read the first boot closely. The plain command below uses installer
defaults; when the unit uses custom files, invoke it as
`KIMI_CONFIG_HOME=/absolute/config-home ENV_FILE=/absolute/kimi.env
RUNTIME_ENV=/absolute/runtime.env ./scripts/preflight` instead, omitting only
the variables the service itself does not set.

```sh
./scripts/preflight
systemctl --user start kimi-agent.service
systemctl --user status kimi-agent.service --no-pager
journalctl --user -u kimi-agent.service -n 250 --no-pager
```

Confirm all of the following:

1. The database reports schema v7. On an existing database, the log shows the
   single v6-to-v7 migration; it never walks older migrations.
2. Discord connects and slash commands synchronize.
3. Every configured module starts and `/modules status` reports the expected
   health.
4. Every expected private plugin has a `Plugin registered:` line.
5. A normal message works, and any feature whose configuration moved is smoke
   tested in a non-production channel first.
6. There is exactly one `bot.py` process and the service is not restart-looping.

The v7 migration is transactional. Invalid historical message JSON or a
non-object payload aborts startup and names the bad row; it does not partially
rewrite the database. Repair from a reviewed copy or restore the backup rather
than deleting arbitrary history to make startup pass.

Offline evaluation cassettes are maintainer data, not production state. V1
cassettes are intentionally rejected by the v2 harness; delete and re-record
them instead of attempting to upgrade their incomplete search-budget metadata.
