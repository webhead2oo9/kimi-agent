import tempfile
from pathlib import Path

from skills.secrets import load_secrets, resolve_secrets, scrub_output


def test_load_secrets() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("API_KEY: sk-abc123\nDB_URL: postgres://secret\n")
        f.flush()
        secrets = load_secrets(Path(f.name))
        assert secrets["API_KEY"] == "sk-abc123"
        assert secrets["DB_URL"] == "postgres://secret"


def test_load_secrets_missing_file() -> None:
    secrets = load_secrets(Path("/nonexistent/secrets.yaml"))
    assert secrets == {}


def test_resolve_secrets() -> None:
    all_secrets = {"API_KEY": "sk-abc", "DB_URL": "pg://x", "OTHER": "val"}
    resolved = resolve_secrets(["API_KEY", "DB_URL"], all_secrets)
    assert resolved == {"API_KEY": "sk-abc", "DB_URL": "pg://x"}
    assert "OTHER" not in resolved


def test_resolve_secrets_missing_key() -> None:
    all_secrets = {"API_KEY": "sk-abc"}
    resolved = resolve_secrets(["API_KEY", "MISSING"], all_secrets)
    assert resolved == {"API_KEY": "sk-abc"}


def test_scrub_output() -> None:
    secrets = {"API_KEY": "sk-abc123", "DB_URL": "postgres://secret"}
    text = "Got response using sk-abc123 from postgres://secret endpoint"
    scrubbed = scrub_output(text, secrets)
    assert "sk-abc123" not in scrubbed
    assert "postgres://secret" not in scrubbed
    assert "[REDACTED]" in scrubbed
    assert "Got response using" in scrubbed


def test_scrub_output_redacts_longest_secret_first() -> None:
    secrets = {"SHORT": "sk-abc", "LONG": "sk-abc123"}
    scrubbed = scrub_output("token=sk-abc123", secrets)

    assert "sk-abc123" not in scrubbed
    assert "123" not in scrubbed
    assert scrubbed == "token=[REDACTED]"


def test_scrub_output_no_secrets() -> None:
    text = "Just some normal output"
    assert scrub_output(text, {}) == text


def test_scrub_output_empty() -> None:
    assert scrub_output("", {"KEY": "val"}) == ""
