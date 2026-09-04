# Move an operator plugin to a module

Use application modules for new extensions. Existing operator plugins remain
supported for deployment-local tools; migration is useful when you want the
public SDK, lifecycle management, or additional host services.

Start with the [one-tool example](../bot/modules/minimal/README.md). Keep your
API client and business logic where they are, and replace the wiring layer.

| Plugin | Module |
|---|---|
| `PLUGIN_MODULES=package.plugin` | Install the package and set `KIMI_MODULES=entry_name` |
| `PLUGIN_API_VERSION = 2` | `ModuleSpec(..., api_version=2)` |
| `register(ctx)` | `ModuleSpec.create(ctx)` returns an object with `scoped_migrations`, `start(ctx)`, and `close()` |
| Imports from `app.plugins`, `tools.registry`, or `trust.tiers` | Host contracts from `kimi_agent_module_api` |
| `ctx.registry.register(...)` | Register through the module load context in `create()` |
| `PLUGIN_SETTINGS` | `ModuleSpec.settings` with `ModuleSettingsDefinition` |
| `ctx.settings_for(MySettings)` | `ctx.settings_for(MySettings)` on the module load context |
| Live gateway access | Declared operations through the runtime context's `discord` port |
| Best-effort loading | Required by default; explicitly optional through `KIMI_OPTIONAL_MODULES` |

## Move configuration and tool registration

Module settings use `<CONFIG_DIR>/modules/<module_name>.md`; plugin overrides
use `<CONFIG_DIR>/plugins/<plugin_name>.md`. Copy the supported values into the
module document and validate them before switching. Migration does not move
files automatically. Keep secrets and deployment boundaries environment-only,
and continue obtaining the prepared model from `settings_for()`.

Register tools in `create()`, preserving their trust tier, owner restrictions,
and searchable setting. Module context IDs are integers, with absent IDs
represented by `None`; adapt client code that previously expected strings.
Module tools default to guild-only. Set `guild_only=False` deliberately for a
tool that supports private conversations, and handle missing guild context.
Keep `untrusted=True` for external content and read caller identity from the
trusted tool context. Activity labels and eval surfaces can still be declared
through the load context.

## Give resources a lifecycle

`create()` prepares settings and tool handlers without opening clients,
starting tasks, or accessing the database. Return a module object whose
handlers can access the resources initialized in `start()`.

Declare the HTTP hosts, Discord actions, events, and services the module uses
in its spec. Prefer host HTTP and Discord ports; a separate client that owns
resources must release them in `close()`. Use the durable scheduler for jobs
that must survive restarts. Add scoped migrations only if the feature needs
persistent data. The [module guide](modules.md) describes these contracts.

`close()` must tolerate a partially completed `start()` and repeated calls.
A recoverable optional startup failure invokes it before disabling the module.
Permission declarations govern host ports; trusted Python code still runs in
the bot process and is not sandboxed.

## Test and switch

Use SDK fakes to test handlers, settings, and cleanup independently of core.
Verify the module in an isolated development instance, including failure after
partial startup. Remove the plugin import from `PLUGIN_MODULES` when adding
the module entry point to `KIMI_MODULES`: enabling both versions can collide
on tool names. Restart to apply the switch and check `/modules status`.

Keep the old package and plugin configuration until the replacement is
verified. To revert, restore the previous lists and restart. Any new database
migrations are forward-only; switching back does not undo them.
