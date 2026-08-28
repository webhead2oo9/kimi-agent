# Reference module

This deliberately small module demonstrates the complete supported path:

- an installed `kimi_agent.modules` entry point;
- one operator-editable setting;
- one LLM tool;
- a scoped database migration and persistent counter; and
- lifecycle startup and shutdown.

It is laid out like a standalone distribution so it shows the shape of a real
module development effort:

```text
example/
├── pyproject.toml
├── README.md
├── LICENSE
├── src/community_agent_reference_module/
│   ├── __init__.py
│   ├── settings.py
│   ├── migrations.py
│   ├── module.py
│   ├── py.typed
│   └── spec.py
└── tests/test_reference_module.py
```

`__init__.py` only exports the entry-point object. Settings, migrations,
lifecycle behavior, and load-time registration live in the files a real module
would normally own. This is a starting point, not a drop-in production module:
copy the structure, choose a new package and module identity, and replace the
example behavior, configuration, migrations, tests, and documentation.

From the `bot` directory, install the example:

```console
uv sync --all-packages --extra dev
```

Enable its entry-point name in `bot/.env`, then start normally on any platform:

```dotenv
KIMI_MODULES=reference_greeter
```

```console
uv run python bot.py
```

After making that copy, depend on `kimi-agent-module-api` from PyPI. Once the
SDK is published, the new package can install and test itself with:

```console
uv sync --extra dev
uv run ruff check .
uv run mypy .
uv run pytest
```
