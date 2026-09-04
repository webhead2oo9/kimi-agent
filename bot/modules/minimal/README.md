# One-tool module

Start here when you want to add an LLM tool. `hello_module.py` is the entire
implementation: one handler, a module with empty lifecycle methods, and a
versioned `ModuleSpec`. It imports only the public SDK. The tool greets the
actual caller, using trusted context rather than a model-supplied name.

## Install and enable

From `bot/`, with the development environment already installed:

```console
.venv/bin/python -m pip install --no-deps --editable ./modules/minimal
```

Add `hello` to the existing `KIMI_MODULES` list in your instance's environment
and restart. For an instance with no other modules:

```dotenv
KIMI_MODULES=hello
```

Ask the assistant in an active server to use `hello_member`. This example
needs no credentials, settings documents, database tables, or Discord actions.
The default tool gates allow members and require a guild. Externally authored
results receive the registry's default untrusted-content envelope.

Modules are required by default. To make this greeting feature optional, also set:

```dotenv
KIMI_OPTIONAL_MODULES=hello
```

Optional failures appear in `/modules status`; changing either list requires
a restart. See [failure policy](../../../docs/modules.md#required-and-optional-modules).

## Test and copy

From `bot/`:

```console
.venv/bin/python -m pytest modules/minimal/tests -q
```

To develop independently, copy this directory outside the checkout, create a
Python 3.14+ virtual environment there, and run:

```console
python -m pip install -e '.[dev]'
python -m pytest -q
```

The unit test uses the SDK's `load_context()` to capture the registered tool
and invoke its handler. It does not import core or connect to Discord.

Rename the distribution, Python file, entry point, spec name, and tool name
for your own feature. Keep `api_version=2` explicit. Add settings and runtime
ports when the feature needs them; empty lifecycle methods need no framework
helper. `create()` is wiring only: acquire resources in `start()` and release
them in an idempotent `close()` that also works after a partial start.

For HTTP clients, settings, storage, commands, events, and scheduled work,
consult the [full kudos example](../example/README.md) and the
[module guide](../../../docs/modules.md). Existing plugins can follow the
[migration guide](../../../docs/plugin-migration.md).
