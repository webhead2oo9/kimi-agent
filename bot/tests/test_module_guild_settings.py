"""Per-guild module settings: coercion, namespaced documents, legacy fallback, policies."""

from __future__ import annotations

from pathlib import Path

from kimi_agent_module_api.contracts import GuildSettingField, GuildSettingsSchema
from modules.guild_settings import (
    GUILD_MODULES_DIR,
    GuildSettingsService,
    coerce_document,
    coerce_value,
)

GUILD = 700000000000000001
CHANNEL = 800000000000000002

SCHEMA = GuildSettingsSchema(
    fields=(
        GuildSettingField("mod_log_channel_id", "id"),
        GuildSettingField("mod_log_events", "id_list"),
        GuildSettingField("mode", "enum", choices=("soft", "hard"), default="soft"),
        GuildSettingField("label", "str"),
        GuildSettingField("count", "int", required=True),
        GuildSettingField("flag", "bool", default=False),
    ),
    invalid_policy="disable_guild",
)
OPTIONAL = GuildSettingsSchema(
    fields=(GuildSettingField("channels", "id_list"),), invalid_policy="disable_module"
)
OPTIONAL_ENFORCEMENT = GuildSettingsSchema(
    fields=(GuildSettingField("channels", "id_list"),), invalid_policy="disable_guild"
)


def test_coercion_rules() -> None:
    assert coerce_value(GuildSettingField("x", "id"), CHANNEL) == (CHANNEL, None)
    assert coerce_value(GuildSettingField("x", "id"), "abc")[1] is not None
    assert coerce_value(GuildSettingField("x", "id_list"), [CHANNEL, str(CHANNEL)]) == (
        (CHANNEL, CHANNEL),
        None,
    )
    assert coerce_value(GuildSettingField("x", "int"), True)[1] is not None
    assert coerce_value(GuildSettingField("x", "str_list"), ["a", "b"]) == (("a", "b"), None)
    assert coerce_value(GuildSettingField("x", "str_list"), ["a", 1])[1] is not None
    assert coerce_value(GuildSettingField("x", "bool"), "yes")[1] is not None
    assert coerce_value(GuildSettingField("x", "enum", choices=("a",)), "b")[1] is not None
    assert coerce_value(GuildSettingField("x", "str", default="d"), None) == ("d", None)
    assert coerce_value(GuildSettingField("x", "int", required=True), None)[1] == "x is required"


def test_document_coercion_rejects_unknown_keys_and_runs_custom_validation() -> None:
    values, errors = coerce_document(SCHEMA, {"count": 1, "typo": 2})
    assert errors == ("unknown setting 'typo'",)
    schema = GuildSettingsSchema(
        fields=(GuildSettingField("count", "int"),),
        validate=lambda v: ("count too big",) if (v.get("count") or 0) > 5 else (),
    )
    assert coerce_document(schema, {"count": 9})[1] == ("count too big",)
    assert coerce_document(schema, {"count": 2}) == ({"count": 2}, ())
    del values


def _write(config_dir: Path, relative: str, text: str) -> None:
    path = config_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _service(
    tmp_path: Path, **schemas: GuildSettingsSchema
) -> tuple[GuildSettingsService, list[tuple[str, str, str]]]:
    health: list[tuple[str, str, str]] = []
    service = GuildSettingsService(
        config_dir=lambda: tmp_path,
        schemas=schemas,
        on_health=lambda m, s, d: health.append((m, s, d)),
        clock=lambda: 1.0,
    )
    return service, health


def test_namespaced_document_wins_and_changes_notify_subscribers(tmp_path: Path) -> None:
    service, health = _service(tmp_path, mod=SCHEMA)
    _write(tmp_path, f"servers/{GUILD}.md", "---\nbot_active: true\ncount: 9\nlabel: legacy\n---\n")
    _write(
        tmp_path,
        f"{GUILD_MODULES_DIR}/{GUILD}/mod.md",
        f"---\nmod_log_channel_id: {CHANNEL}\ncount: 3\n---\n",
    )
    seen: list[int] = []
    unsubscribe = service.subscribe("mod", seen.append)
    service.refresh([GUILD])
    snapshot = service.get(GUILD, "mod")
    assert snapshot.valid and not snapshot.legacy
    assert snapshot.values == {
        "mod_log_channel_id": CHANNEL,
        "mod_log_events": None,
        "mode": "soft",
        "label": None,
        "count": 3,
        "flag": False,
    }
    assert snapshot.revision
    assert seen == [GUILD]
    service.refresh([GUILD])
    assert seen == [GUILD]  # unchanged content does not notify
    _write(tmp_path, f"{GUILD_MODULES_DIR}/{GUILD}/mod.md", "---\ncount: 4\n---\n")
    service.refresh([GUILD])
    assert seen == [GUILD, GUILD]
    unsubscribe()
    assert health == []


def test_legacy_server_keys_are_a_reported_fallback(tmp_path: Path) -> None:
    service, health = _service(tmp_path, mod=SCHEMA)
    _write(
        tmp_path,
        f"servers/{GUILD}.md",
        f"---\nbot_active: true\nmod_log_channel_id: {CHANNEL}\ncount: 2\nunrelated: x\n---\nbody\n",
    )
    service.refresh([GUILD])
    snapshot = service.get(GUILD, "mod")
    assert snapshot.valid and snapshot.legacy
    assert snapshot.values["mod_log_channel_id"] == CHANNEL
    assert health == [("mod", "degraded", f"legacy server keys still used by {GUILD}")]
    _write(tmp_path, f"{GUILD_MODULES_DIR}/{GUILD}/mod.md", "---\ncount: 2\n---\n")
    service.refresh([GUILD])
    assert service.get(GUILD, "mod").legacy is False
    assert health[-1] == ("mod", "healthy", "")


def test_invalid_documents_apply_the_declared_policy(tmp_path: Path) -> None:
    service, health = _service(tmp_path, mod=SCHEMA, opt=OPTIONAL)
    _write(tmp_path, f"{GUILD_MODULES_DIR}/{GUILD}/mod.md", "---\ncount: nope\n---\n")
    _write(tmp_path, f"{GUILD_MODULES_DIR}/{GUILD}/opt.md", "---\nchannels: [abc]\n---\n")
    service.refresh([GUILD])
    assert service.get(GUILD, "mod").errors == ("count must be an integer",)
    assert service.blocked_guilds() == frozenset({GUILD})  # disable_guild
    assert service.is_enabled(GUILD, "opt", guild_active=True) is False  # disable_module
    assert ("mod", "degraded", f"invalid guild settings in {GUILD}") in health
    view = service.view_for("opt", lambda _g: True)
    assert view.is_enabled(GUILD) is False
    _write(tmp_path, f"{GUILD_MODULES_DIR}/{GUILD}/mod.md", "---\ncount: 1\n---\n")
    _write(tmp_path, f"{GUILD_MODULES_DIR}/{GUILD}/opt.md", f"---\nchannels: [{CHANNEL}]\n---\n")
    service.refresh([GUILD])
    assert service.blocked_guilds() == frozenset()
    assert view.is_enabled(GUILD) is True
    assert view.get(GUILD).values == {"channels": (CHANNEL,)}


def test_body_content_and_missing_documents(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, mod=OPTIONAL)
    _write(tmp_path, f"{GUILD_MODULES_DIR}/{GUILD}/mod.md", "---\nchannels: []\n---\nnot allowed\n")
    assert service.get(GUILD, "mod").errors == ("module guild settings must be frontmatter only",)
    missing = service.get(GUILD + 1, "mod")
    assert missing.valid and missing.values == {"channels": None} and missing.legacy is False


def test_malformed_optional_only_documents_fail_closed(tmp_path: Path) -> None:
    """Parse failures cannot look like an empty, valid optional configuration."""
    documents = (
        "---\nchannels: [unclosed\n---\n",
        "---\nchannels: []\n",
        "---\n- not\n- a mapping\n---\n",
    )
    for offset, document in enumerate(documents):
        guild_id = GUILD + offset
        _write(tmp_path, f"{GUILD_MODULES_DIR}/{guild_id}/enforcer.md", document)

    service, _ = _service(tmp_path, enforcer=OPTIONAL_ENFORCEMENT)
    service.refresh(GUILD + offset for offset in range(len(documents)))

    assert all(not service.get(GUILD + offset, "enforcer").valid for offset in range(3))
    assert service.blocked_guilds() == frozenset(GUILD + offset for offset in range(3))
