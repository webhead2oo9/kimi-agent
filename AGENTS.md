# Repository Guidelines

## Project Structure & Module Organization

The Discord assistant lives in `bot/`. Entry point `bot.py` delegates to `app/`; `agent/` owns conversation orchestration, `providers/` model adapters, `tools/` tool dispatch, and `storage/` persistence. Configuration templates and prompts live in `bot/config/`, built-in playbooks in `bot/skills/builtin/`, and deployment resources in `bot/deploy/`.

Tests live in `bot/tests/`, `bot/modules/example/tests/`, and `bot/modules/minimal/tests/`; the standalone module API has its own tests under `bot/packages/bram-agent-module-api/tests/`. Browser bridge tests live in `bot/tests/js/`. Subsystem documentation lives in [docs/](docs/README.md).

## Build, Test, and Development Commands

Use Python 3.14+ and run commands from `bot/`. Follow [development setup](docs/development.md) to create `.venv` and install all three local projects with development dependencies.

- `uv sync --locked --all-packages --extra dev` — install locked workspace dependencies; see setup documentation for pip interoperability.
- `ENV_FILE=.env.dev .venv/bin/python bot.py` — run an isolated development instance.
- `.venv/bin/ruff check .` — lint Python.
- `.venv/bin/ruff format --check .` — check formatting; omit `--check` to format.
- `.venv/bin/mypy .` — check core types.
- `.venv/bin/mypy --config-file modules/example/pyproject.toml modules/example/src modules/example/tests` — check reference-module types.
- `.venv/bin/mypy --config-file modules/minimal/pyproject.toml modules/minimal/hello_module.py modules/minimal/tests` — check minimal-module types.
- `.venv/bin/python -m pytest -q` — run application, reference-module, and minimal-module tests; standalone API tests run separately.
- `uv run --isolated --locked --group test python -m pytest -q` — run standalone API tests from `bot/packages/bram-agent-module-api/`.
- `node --test tests/js/*.test.mjs` — run browser bridge tests; CI uses Node 22.18.0.
- `uv build --package bram-agent-module-api --no-sources` — build API distributions.
- `uv build --package community-agent-reference-module --no-sources` — build reference-module distributions.

After dependency changes, run `uv lock` and `uv --preview-features audit-command audit --locked`. Match applicable checks in [.github/workflows/ci.yml](.github/workflows/ci.yml) before submitting, including browser runtime installation/auditing and syntax checks, pip installation compatibility, and built-distribution verification through `scripts/verify_module_api_dist.py`. See [development setup](docs/development.md) for the commands and live sandbox prerequisites; CI runs a separate sandbox job with `BRAM_REQUIRE_SANDBOX_TESTS=1`.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, and `PascalCase` classes. Ruff targets Python 3.14 and formats to 100 columns. Match neighboring modules' type annotations and module-level logging. Keep async paths free of blocking I/O; lint rules explicitly check this.

Architecture tests restrict runtime Discord imports to `app/`, `commands/`, and `discord_adapter/`. Keep provider implementations outside `agent/core.py`. New cross-package dependencies require a deliberate update to `tests/test_package_graph.py`.

## Testing Guidelines

Use pytest and pytest-asyncio, `test_*.py` files, and explicit `@pytest.mark.asyncio` markers. Add focused regression tests for behavior changes; prefer `monkeypatch`, reusable fakes, and `tmp_path`. CI records branch coverage without a configured minimum percentage. For documentation changes, run `.venv/bin/python -m pytest tests/test_docs_links.py -q` and `git diff --check`.

## Commit & Pull Request Guidelines

History includes imperative subjects (`Report test coverage in CI`) and scoped prefixes (`refactor(tools): centralize per-turn budgets`). Keep subjects concise and commits focused. PRs should explain the problem and resulting behavior, link relevant issues, report verification, and update affected documentation.

## Security & Configuration

Keep credentials, live model routing, databases, workspaces, and authentication files untracked. Development instances require separate tokens and state paths; follow [instance-data guidance](docs/instance-data.md).
