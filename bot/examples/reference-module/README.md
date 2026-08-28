# Reference module

This deliberately small module demonstrates the complete supported path:

- an installed `kimi_agent.modules` entry point;
- one operator-editable setting;
- one LLM tool;
- a scoped database migration and persistent counter; and
- lifecycle startup and shutdown.

It is laid out as a standalone distribution—the directory itself is what a
module author would copy into another repository:

```text
reference-module/
├── pyproject.toml
├── README.md
├── LICENSE
├── src/community_agent_reference_module/
│   ├── __init__.py
│   ├── settings.py
│   ├── migrations.py
│   ├── module.py
│   └── spec.py
└── tests/test_reference_module.py
```

`__init__.py` only exports the entry-point object. Settings, migrations,
lifecycle behavior, and load-time registration live in the files a real module
would normally own.

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

Copy this package into another repository, change its distribution/import/module
names, and depend on `kimi-agent-module-api` from PyPI when publishing it. Once
the SDK is published, the copied package can install and test itself with:

```console
uv sync --extra dev
uv run pytest
```
