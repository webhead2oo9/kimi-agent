from pathlib import Path


SCRIPTS_DIR = Path("scripts")
HELPERS = ("preflight", "diagnostics", "codex-login", "install-service")


def _script(name: str) -> str:
    return (SCRIPTS_DIR / name).read_text(encoding="utf-8")


def test_helpers_keep_deployment_home_separate_from_app_config_dir() -> None:
    for name in HELPERS:
        source = _script(name)
        assert 'KIMI_CONFIG_HOME="${KIMI_CONFIG_HOME:-' in source
        assert 'CONFIG_DIR="${CONFIG_DIR:-' not in source


def test_codex_login_uses_the_selected_codex_token_setting() -> None:
    source = _script("codex-login")

    assert 'if [[ -z "${TOKEN_FILE:-}" ]]' in source
    assert 'ENV_FILE="$ENV_FILE" "$PYTHON_BIN"' in source
    assert "from config.settings import Settings" in source
    assert "print(Settings().codex_token_file)" in source


def test_preflight_passes_effective_settings_to_browser_smoke() -> None:
    source = _script("preflight")

    assert "asyncio.run(browser_smoke(settings))" in source
    assert "asyncio.run(browser_smoke())" not in source


def test_service_installer_quotes_generated_path_values() -> None:
    source = _script("install-service")

    assert 'SYSTEMD_WORKING_DIRECTORY="$(systemd_quote "$BOT_DIR")"' in source
    assert 'SYSTEMD_ENV_FILE="$(systemd_quote "ENV_FILE=$ENV_FILE")"' in source
    assert 'SYSTEMD_RUNTIME_ENV="$(systemd_quote "-$RUNTIME_ENV")"' in source
    assert 'SYSTEMD_EXECUTABLE="$(systemd_exec_quote "$PYTHON_BIN")"' in source
    assert "WorkingDirectory=$SYSTEMD_WORKING_DIRECTORY" in source
    assert "Environment=$SYSTEMD_ENV_FILE" in source
    assert "EnvironmentFile=$SYSTEMD_RUNTIME_ENV" in source
    assert "ExecStart=$SYSTEMD_EXECUTABLE bot.py" in source
