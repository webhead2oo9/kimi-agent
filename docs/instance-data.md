# Public source and private instance data

The repository is the application, not a deployment backup. A public checkout
contains code, generic templates, and synthetic fixtures. Anything that
identifies a live Discord deployment, contains community knowledge, records user
activity, or reveals private provider routing is instance data, and it stays
outside the public repository.

## Boundary

| Public and tracked | Private and ignored or external |
|---|---|
| Python source and tests | `.env` files and credential stores |
| `.env.example` and `*.example.*` templates | `<CONFIG_DIR>/models.yaml`, `settings.md`, and module/plugin/tool overlays |
| Generic prompt/persona examples | The active prompt, persona, and deployment-specific prompt overrides |
| Synthetic Discord identifiers in fixtures | `<SKILLS_DIR>` including staff-created and learned skills |
| Eval code, synthetic scenarios, fixtures, and `models.example.yaml` | Deployment-specific scenarios; `evals/models.yaml`, cassettes, results, transcripts, and run artifacts |
| Deployment manifests containing variable references only | SQLite, Hindsight data, attachments, workspaces, logs, and generated files |
| Current architecture and public documentation | Private checkout paths and unpublished project notes |

Secrets belong in the deployment secret store or in ignored files, never in a
private Git repository. Private configuration and skills, on the other hand,
should be versioned in a separate access-controlled repository and backed up
independently of Git.

## Private configuration repository

Keep the deployment repository separate from the public application checkout.
A practical layout looks like this:

```text
kimibot-private/
├── README.md                         # deployment notes, with no secrets
├── config/
│   ├── prompt.md                     # required base system-prompt template
│   ├── persona.md                    # default persona, when <persona> is used
│   ├── models.yaml                   # required provider/model routing
│   ├── settings.md                   # optional safe startup overrides
│   ├── tools.md                      # optional deployment-wide tool denylist
│   ├── servers/<guild_id>.md         # guild activation, policy, and instructions
│   ├── channels/<channel_id>.md      # channel instructions and tool policy
│   ├── channel_threads/<channel_id>.md
│   ├── threads/<thread_id>.md
│   ├── prompts/
│   │   ├── commands/<name>.md        # copy every tracked shared template
│   │   ├── commands/<name>/<guild_id>.md
│   │   ├── servers/<guild_id>.md     # optional full-prompt replacements
│   │   └── channels/<channel_id>.md  # or <thread_id>.md for one thread
│   ├── modules/<module_name>.md      # optional safe startup module settings
│   ├── guild-modules/<guild_id>/<module_name>.md
│   ├── plugins/<plugin_name>.md      # optional safe plugin settings
│   └── tools/<tool_name>.md          # optional live per-tool configuration
└── skills/
    └── <skill_name>/
        ├── SKILL.md
        ├── reference/                # optional supporting documents
        └── scripts/                  # optional reviewed executable tools
```

`config/prompt.md` and `config/models.yaml` are required for normal model turns.
`persona.md` may be absent only if the prompt doesn't use `<persona>` (the
public `prompt.md` does); a missing file silently renders an empty block rather
than failing, so you won't get an error to warn you. Copy every tracked shared
command template as well. In particular, the **Teach Kimi** context menu expects
`config/prompts/commands/learn.md`; without it, the turn falls back to the base
prompt and loses its narrower learning workflow and quoted-message handling.

Numeric fragments, full overrides, module/plugin/tool files, and skills are only
included when the deployment actually uses them. Copy the public
`bot/config/prompt.md`, `persona.md`, tracked shared command templates, and
`models.example.yaml` as starting points; from then on the private copies are
the deployment's source of truth. One subtlety worth knowing: `models.yaml` is
validated at startup, but `prompt.md` is first opened when a model turn builds
its prompt, so provisioning must include a real message smoke test rather than
treating a successful process start as sufficient.

The active prompt is one complete template, selected most-specific-first from a
command, channel/thread, server, or base `prompt.md` file. Full overrides don't
inherit prose from the base template, so every private full override must
carry all of the behavioral, safety, guardrail, error-hygiene, and tool-routing
rules that should apply in that scope. `<safety>` and `<guardrails>` are not
tokens, and nothing appends those rules in code. Scalar and generated
placeholders are substituted once; see the
[full-template guide](../bot/config/prompts/README.md) for the resolution
order and supported layout behavior.

Point the application at the private checkout with absolute paths:

```dotenv
CONFIG_DIR=/srv/kimi/private/config
SKILLS_DIR=/srv/kimi/private/skills
```

Clone or update the public and private repositories independently, review and
commit private configuration changes, then deploy an approved revision of each.
Prompt templates, instruction fragments, tool fragments, and instruction-skill
indexes are refreshed from disk during normal turns. Per-guild module settings
refresh with guild activation and immediately after an approved proposal. Model
routing, global settings, module and plugin registration, startup module/plugin
settings, secrets metadata, and executable skill registrations are startup-only,
so restart the bot after changing those files.

Private templates are independent copies of the public templates. Before each
application upgrade, diff the release's public `prompt.md`, `persona.md`, and
shared command templates against the private base files and every applicable
full override. Merge required placeholders, safety rules, error handling, and
tool-routing guidance before deploying the two revisions together.

Staff learning tools write instruction skills into the live `SKILLS_DIR`, and
Git doesn't commit or push those writes on its own. If the private checkout is
also the live skill directory, monitor and review its working tree, then commit
accepted changes through the normal private-repository workflow. Executable
scripts can only be added on disk by an operator, and they must be reviewed
before deployment.

Don't put `.env`, credential files, Discord or provider tokens, skill secret
values, Codex auth, SQLite/Hindsight data, attachments, workspaces, logs, eval
cassettes, or generated output in the private repository. Back up those runtime
stores separately, with access and retention controls appropriate to user data.

Deployment-owned plugin packages are private application code, not
configuration fragments. Keep each package, its tests, and its documentation in
an access-controlled source repository, install it into the bot environment (or
place its checkout on `PYTHONPATH`), and list its importable module explicitly
in `PLUGIN_MODULES`. See
[Operator Plugins](plugins.md#publicprivate-source-split).

## Recommended production layout

Point every writable or deployment-owned path outside the application checkout.
Absolute paths avoid any dependence on the supervisor's working directory, and
separating durable state from temporary staging keeps the backup and retention
policy clear:

```dotenv
CONFIG_DIR=/srv/kimi/private/config
SKILLS_DIR=/srv/kimi/private/skills
DATABASE_PATH=/srv/kimi/instance/data/bot.db
PERSONAL_SKILLS_DIR=/srv/kimi/instance/data/personal_skills
WORKSPACE_DIR=/srv/kimi/instance/workspaces
TOOL_EVENT_LOG_PATH=/srv/kimi/instance/logs/events.jsonl
SECRETS_FILE=/srv/kimi/instance/secrets/skills.yaml
CODEX_TOKEN_FILE=/srv/kimi/instance/secrets/codex-auth.json
BROWSER_PROFILES_DIR=/srv/kimi/instance/data/browser_profiles
BROWSER_RUNTIME_DIR=/opt/kimi/betterwright

# Temporary staging; normal turns delete these files immediately.
ATTACHMENT_STORE_DIR=/var/tmp/kimi/attachments
```

These paths are examples, not prescribed host locations. Mount the durable
paths into replacement containers or services, but don't back up attachment
staging. Workspaces and logs are retained according to their own TTL, quota,
and rotation rules, so any backups must enforce compatible deletion periods.

Grant the bot only the access each path requires:

| Path | Bot access | Operational treatment |
|---|---|---|
| `CONFIG_DIR`, `.env`, `SECRETS_FILE` | read-only | Private configuration or secrets; updates come from the deployment system. |
| `SKILLS_DIR` | read/write | Staff tools can create, edit, and delete shared skills. |
| `DATABASE_PATH`, `PERSONAL_SKILLS_DIR`, `WORKSPACE_DIR`, `BROWSER_PROFILES_DIR` | read/write | Durable user or instance state; back up with retention and privacy controls. Browser profiles can contain authenticated site state. |
| `BROWSER_RUNTIME_DIR` | read-only | Root-owned pinned BetterWright/Node/Chromium program files; reproduce with the installer rather than backing up. |
| `TOOL_EVENT_LOG_PATH` | write | Retain or ship according to the diagnostic-log policy. |
| `CODEX_TOKEN_FILE` | read/write | OAuth refresh rewrites this credential atomically. |
| `ATTACHMENT_STORE_DIR` | read/write | Ephemeral staging; exclude from backups. |

## Provisioning a new instance

1. Copy `.env.example` to an ignored `.env` or configure the supervisor's secret
   environment, then set the private and runtime paths.
2. Clone the access-controlled deployment repository. For its first revision,
   seed `config/prompt.md`, `persona.md`, and all tracked shared command
   templates from the public generic files, then copy
   `config/models.example.yaml` to the private `config/models.yaml`. Replace its
   placeholder endpoints/model IDs, context windows, capabilities, roles, and
   fallbacks. Add image routing only after you've verified model support.
3. Restore the private `skills/` tree if the deployment uses shared skills.
   An absent or empty `SKILLS_DIR` is valid and simply contributes no private
   skills; the read-only built-ins shipped with the application remain
   available.
4. Add numeric server/channel/thread fragments only under the private
   `CONFIG_DIR`. Activate at least one guild there or through
   `ALLOWED_GUILD_IDS`.
5. When replacing an existing instance, restore a WAL-consistent SQLite backup
   and its matching SQLCipher key when encryption is enabled. Restore shared and
   personal skills, retained workspaces/logs as policy requires, and the
   Hindsight service through its own backup procedure. Never restore attachment
   staging.
6. Start exactly one bot process, confirm the expected database schema and guild
   activation in the logs, then complete a real mention/reply smoke test so the
   active prompt and provider route are actually exercised.

The default in-checkout paths remain convenient for local development.
Deployment-owned files under `config/` and the contents of `skills/store/` are
ignored as defense in depth, while the generic source files under `config/` and
the read-only skills under `skills/builtin/` remain tracked.

## Skills

`SKILLS_DIR` is both read by the skill loader and written by the staff learning
tools (see above). Back it up alongside the database and configuration, and
restore it before starting a replacement instance. At runtime it's merged with
the tracked, read-only `skills/builtin/` catalog. The operational contract and
restore guidance are summarized in
[`bot/skills/README.md`](../bot/skills/README.md).

Instruction-only skills created through Discord can't add scripts. A skill
containing `tools:` declarations or files under `scripts/` is executable
operator code: review it separately, and don't accept executable content from
an untrusted backup. Executable tools run inside the mandatory Linux Bubblewrap
boundary, with the skill mounted read-only and the exact per-call job directory
as the only writable host-backed mount (`/workspace`). A private writable `/tmp`
exists inside the sandbox but exposes no host files. Network access is absent
unless that individual declaration sets `network: true`; such an opt-in shares
the service host's network reachability, subject to its firewall, proxy, and
routing controls.

## Evals

Copy `evals/models.example.yaml` to the ignored `evals/models.yaml` before
running an eval. Live model names, gateway URLs, pricing, environment-variable
names, and benchmark results are all deployment metadata. Cassettes contain
recorded tool arguments and results, and transcripts contain prompt and tool
data, so `evals/cassettes/`, `evals/runs/`, and `evals/RESULTS.md` remain
private.

The tracked `evals/scenarios/` tree is for generic synthetic cases only. Put
deployment-specific scenarios under the ignored `evals/private/`, especially
when they contain community knowledge, copied messages, private prompts, or
real Discord identifiers. Select that directory with `--scenarios evals/private`
rather than copying its content into the public scenario tree.

## Before publishing

From the repository root:

```bash
git status --short --ignored
git ls-files
git diff --check
```

Review every tracked non-source artifact, run a content secret scanner in CI,
and verify that the examples contain synthetic IDs and placeholder hosts.
Ignore rules do not untrack files; remove those paths from the index while
preserving the local instance copy before creating or publishing a public
repository. Scan the repository object database
as well as the current tree: a committed secret remains recoverable after its
file is deleted.
