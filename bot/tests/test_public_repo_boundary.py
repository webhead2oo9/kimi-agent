from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path
from pathlib import PurePosixPath

import yaml  # type: ignore[import-untyped]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return {
        entry.decode("utf-8").replace("\\", "/") for entry in result.stdout.split(b"\0") if entry
    }


def test_private_instance_paths_are_not_tracked() -> None:
    tracked = _tracked_paths()
    forbidden_exact = {
        "bot/config/models.yaml",
        "bot/config/settings.md",
        "bot/config/tools.md",
        "bot/config/plugins.md",
        "bot/evals/models.yaml",
        "bot/evals/RESULTS.md",
    }
    forbidden_prefixes = (
        "bot/skills/store/",
        "bot/skills/instances/",
        "bot/evals/cassettes/",
        "bot/evals/runs/",
        "bot/evals/results/",
        "bot/evals/latest/",
    )
    private_config = re.compile(
        r"^bot/config/(?:servers|channels|channel_threads|threads)/"
        r"(?!example\.md$)"
    )
    private_prompt = re.compile(
        r"^bot/config/prompts/(?:servers|channels)/(?!example\.md$)"
        r"|^bot/config/prompts/commands/[^/]+/"
    )
    private_operator_tree = re.compile(
        r"^bot/config/(?:tools|plugins)/"
        r"(?!(?:example|sample|template)\.md$|"
        r"[^/]+\.(?:example|sample|template)\.md$)"
    )
    private_dev_root = re.compile(r"^bot/(?:config|skills)(?:\.[^/]+)?\.(?:dev|local|instance)/")
    private_config_file = re.compile(
        r"^bot/config/(?:models[^/]*\.ya?ml|settings[^/]*\.md|"
        r"(?:tools|plugins)[^/]*\.md)$",
        re.IGNORECASE,
    )
    private_eval_file = re.compile(
        r"^bot/evals/(?:models[^/]*\.ya?ml|RESULTS(?:\..*)?)$",
        re.IGNORECASE,
    )
    public_config_files = {
        "bot/config/models.example.yaml",
        "bot/config/models.example.yml",
        "bot/config/models.sample.yaml",
        "bot/config/models.sample.yml",
        "bot/config/models.template.yaml",
        "bot/config/models.template.yml",
        "bot/config/settings.example.md",
        "bot/config/settings.sample.md",
        "bot/config/settings.template.md",
        "bot/config/tools.example.md",
        "bot/config/tools.sample.md",
        "bot/config/tools.template.md",
        "bot/config/plugins.example.md",
        "bot/config/plugins.sample.md",
        "bot/config/plugins.template.md",
    }
    public_eval_files = {
        "bot/evals/models.example.yaml",
        "bot/evals/models.example.yml",
        "bot/evals/models.sample.yaml",
        "bot/evals/models.sample.yml",
        "bot/evals/models.template.yaml",
        "bot/evals/models.template.yml",
    }
    runtime_segments = {
        "data",
        "logs",
        "secrets",
        "workspaces",
        "sandbox",
        "sandboxes",
        "attachments",
        "transcripts",
    }
    private_suffixes = (
        ".log",
        ".db",
        ".db-shm",
        ".db-wal",
        ".db-journal",
        ".sqlite",
        ".sqlite-shm",
        ".sqlite-wal",
        ".sqlite-journal",
        ".sqlite3",
        ".sqlite3-shm",
        ".sqlite3-wal",
        ".sqlite3-journal",
        ".pid",
        ".sock",
        ".key",
        ".pem",
        ".p12",
        ".pfx",
        ".jks",
        ".ppk",
        ".kdbx",
        ".ovpn",
        ".mobileprovision",
        ".tfstate",
        ".tfvars",
        ".tfvars.json",
    )

    def is_public_variant(path: str) -> bool:
        name = PurePosixPath(path).name.lower()
        return any(marker in name for marker in (".example.", ".sample.", ".template."))

    def is_private_name(path: str) -> bool:
        name = PurePosixPath(path).name.lower()
        if name == ".env":
            return True
        if name.startswith(".env.") and not name.endswith((".example", ".sample", ".template")):
            return True
        if name.startswith(("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")):
            return True
        if name == "events.jsonl" or name.startswith(("events.jsonl.",)):
            return True
        if ".log." in name or ".tfstate." in name:
            return True
        if name.endswith(private_suffixes):
            return not (name.endswith((".tfvars", ".tfvars.json")) and is_public_variant(path))
        if name in {".netrc", ".npmrc", ".pypirc", "auth.json", "credentials.json"}:
            return True
        return (
            name.endswith(("-auth.json", "_auth.json"))
            or ("credentials" in name and name.endswith(".json"))
            or (name.startswith("client_secret") and name.endswith(".json"))
            or ("service-account" in name and name.endswith(".json"))
            or ("service_account" in name and name.endswith(".json"))
            or ("cookies" in name and name.endswith(".json"))
            or (name.startswith(("storage-state", "storage_state")) and name.endswith(".json"))
        )

    def is_private_path(path: str) -> bool:
        parts = PurePosixPath(path).parts
        public_sandbox_source = path.startswith("bot/sandbox/") and path.endswith(".py")
        public_sandbox_eval = path.startswith("bot/evals/scenarios/sandbox/") and path.endswith(
            (".yaml", ".yml")
        )
        return (
            path == "PLAN.md"
            or path in forbidden_exact
            or path.startswith(forbidden_prefixes)
            or (
                bool(runtime_segments.intersection(parts))
                and not public_sandbox_source
                and not public_sandbox_eval
            )
            or bool({".aws", ".azure", ".kube", ".ssh", ".gnupg"}.intersection(parts))
            or (".docker" in parts and PurePosixPath(path).name == "config.json")
            or ".auth" in parts
            or bool(private_config.search(path))
            or bool(private_prompt.search(path))
            or bool(private_operator_tree.search(path))
            or bool(private_dev_root.search(path))
            or (bool(private_config_file.fullmatch(path)) and path not in public_config_files)
            or (bool(private_eval_file.fullmatch(path)) and path not in public_eval_files)
            or is_private_name(path)
        )

    leaked = sorted(path for path in tracked if is_private_path(path))

    assert leaked == [], f"private instance paths are tracked: {leaked}"


def test_public_templates_remain_tracked() -> None:
    tracked = _tracked_paths()
    assert {
        "bot/.env.example",
        "bot/config/models.example.yaml",
        "bot/config/servers/example.md",
        "bot/deploy/hindsight/.env.example",
        "bot/evals/models.example.yaml",
        "bot/skills/README.md",
    } <= tracked


def test_repository_declares_lf_text_and_binary_asset_attributes() -> None:
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attributes.splitlines()
    assert "*.png binary" in attributes.splitlines()


def test_mypy_excludes_runtime_workspaces_without_excluding_workspace_code() -> None:
    config = tomllib.loads((REPO_ROOT / "bot/pyproject.toml").read_text(encoding="utf-8"))
    excluded = re.compile(config["tool"]["mypy"]["exclude"])

    for path in (
        "workspaces/user/files/generated.py",
        "data/workspaces/user/files/generated.py",
        "data/dev/workspaces/user/files/generated.py",
        "skills/store/private/scripts/runtime.py",
    ):
        assert excluded.search(path), path

    for path in (
        "tools/workspace/files.py",
        "workspace/manager.py",
        "data/dev/workspace_helpers.py",
    ):
        assert excluded.search(path) is None, path


def test_dependabot_updates_uv_and_github_actions_weekly() -> None:
    config = yaml.safe_load((REPO_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}

    assert updates["uv"]["directory"] == "/bot"
    assert updates["uv"]["schedule"]["interval"] == "weekly"
    assert updates["uv"]["groups"]["python-minor-patch"]["update-types"] == [
        "minor",
        "patch",
    ]
    # ruff and mypy gate CI, so they get their own PR. exclude-patterns rather
    # than group order, so the split does not depend on which group wins a tie.
    assert updates["uv"]["groups"]["lint-and-types"]["patterns"] == ["ruff", "mypy"]
    assert updates["uv"]["groups"]["python-minor-patch"]["exclude-patterns"] == [
        "ruff",
        "mypy",
    ]
    assert updates["github-actions"]["directory"] == "/"
    assert updates["github-actions"]["schedule"]["interval"] == "weekly"


def test_public_model_templates_are_non_routable_placeholders() -> None:
    runtime = yaml.safe_load(
        (REPO_ROOT / "bot/config/models.example.yaml").read_text(encoding="utf-8")
    )
    for provider in runtime["providers"].values():
        assert provider["base_url"].endswith(".example.invalid/v1")
    for model in runtime["models"].values():
        assert model["model"].startswith("provider/")
        assert "pricing" not in model

    evals = yaml.safe_load(
        (REPO_ROOT / "bot/evals/models.example.yaml").read_text(encoding="utf-8")
    )
    specs = [evals["baseline"], evals["judge"], *evals["candidates"].values()]
    for spec in specs:
        assert spec["base_url"].endswith(".example.invalid/v1")
        assert spec["model"].startswith("provider/")

    hindsight = (REPO_ROOT / "bot/deploy/hindsight/.env.example").read_text(encoding="utf-8")
    assert "https://memory-llm.example.invalid/v1" in hindsight
    assert "provider/memory-model" in hindsight

    compose = yaml.safe_load(
        (REPO_ROOT / "bot/deploy/hindsight/docker-compose.yml").read_text(encoding="utf-8")
    )
    environment = compose["services"]["hindsight"]["environment"]
    private_route_values = {
        entry.split("=", 1)[1] for entry in environment if "_LLM_" in entry.split("=", 1)[0]
    }
    assert private_route_values
    assert all(value.startswith("${HINDSIGHT_LLM_") for value in private_route_values)


def test_tracked_discord_ids_are_synthetic() -> None:
    allowed_prefixes = ("123456", "700000", "800000", "900000", "987654")
    result = subprocess.run(
        [
            "git",
            "grep",
            "-h",
            "-I",
            "-o",
            "-E",
            r"[0-9]{17,20}",
            "--",
            # Dependency hashes contain arbitrary digit runs.
            ":!bot/uv.lock",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode in {0, 1}, result.stderr
    identifiers = set(result.stdout.splitlines())
    leaked = sorted(value for value in identifiers if not value.startswith(allowed_prefixes))

    assert leaked == [], f"non-synthetic Discord-like identifiers are tracked: {leaked}"
