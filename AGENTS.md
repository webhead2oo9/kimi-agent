# Repository Guidelines

## Project Structure & Module Organization

The Discord assistant lives in `bot/`: `bot.py` delegates to `app/`, `agent/`
orchestrates turns, `providers/` adapts models, `tools/` dispatches tools, and
`storage/` persists data. Templates, playbooks, deployment resources, and the React
dashboard live in `bot/config/`, `bot/skills/builtin/`, `bot/deploy/`, and
`bot/dashboard/`.

Tests live in `bot/tests/`, `bot/modules/{example,minimal}/tests/`, and
`bot/packages/kimi-agent-module-api/tests/`. See the
[architecture guide](docs/architecture.md) and [documentation index](docs/README.md).

## Build, Test, and Development Commands

Use Python 3.14+. Run commands from `bot/` unless noted. Follow
[development setup](docs/development.md) for environment creation and pip interoperability.

- `uv sync --locked --all-packages --extra dev`: install locked workspace dependencies.
- `ENV_FILE=.env.dev .venv/bin/python bot.py`: run an isolated development instance.
- `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .`: lint and check formatting.
- `.venv/bin/mypy .`: check core types.
- `.venv/bin/python -m pytest -q`: test the application and both example modules.
- `node --test tests/js/*.test.mjs`: test browser bridges.
- `uv build --package kimi-agent-module-api --no-sources`: build API distributions.

Run standalone API tests from `bot/packages/kimi-agent-module-api/` with
`uv run --isolated --locked --group test python -m pytest -q`.
In `bot/dashboard/`, use `npm run build`, `npm test` (Vitest), and
`npm run test:browser` (Playwright), with CI's pinned Node version.

After dependency changes, run `uv lock` and
`uv --preview-features audit-command audit --locked`. Follow the applicable
[CI checks](.github/workflows/ci.yml), including module types, standalone API tests,
distribution verification, dashboard checks, and live sandbox tests.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, and `PascalCase` classes.
Ruff targets Python 3.14 with 100-column formatting. Match neighboring annotations
and logging; keep blocking I/O out of async functions.

Confine runtime Discord imports to `app/`, `commands/`, and `discord_adapter/`.
Keep provider implementations outside `agent/core.py`; update
`tests/test_package_graph.py` for intentional new cross-package imports.

## Testing Guidelines

Use pytest and pytest-asyncio, `test_*.py` files, and explicit
`@pytest.mark.asyncio` markers. Add focused regression tests for behavior changes;
prefer fakes, `monkeypatch`, and `tmp_path`. CI records branch coverage without a
minimum threshold.

For documentation changes, run
`.venv/bin/python -m pytest tests/test_docs_links.py -q` and `git diff --check`.

## Commit & Pull Request Guidelines

Use concise imperative subjects, such as `Prefer the fast self-hosted runner`;
scoped prefixes also appear in history. Keep commits focused. PRs should explain
the problem and resulting behavior, link relevant issues, report verification,
and update affected documentation.

## Security & Configuration

Keep credentials, live routing, databases, workspaces, and authentication files
untracked. Use separate development tokens and state paths; follow
[instance-data guidance](docs/instance-data.md).
