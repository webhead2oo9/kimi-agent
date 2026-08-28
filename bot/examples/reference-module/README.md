# Reference module

This deliberately small module demonstrates the complete supported path:

- an installed `community_agent.modules` entry point;
- one operator-editable setting;
- one LLM tool;
- a scoped database migration and persistent counter; and
- lifecycle startup and shutdown.

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
names, and depend on `community-agent-module-api` from PyPI when publishing it.
