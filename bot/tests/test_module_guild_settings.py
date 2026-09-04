"""Per-guild module settings: coercion, namespaced documents, and policies."""

from __future__ import annotations

import pytest

from pathlib import Path
from threading import Event, Lock, Thread

from kimi_agent_module_api.contracts import (
    GuildSettingField,
    GuildSettingsSchema,
    GuildSettingsSnapshot,
)
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
    assert coerce_value(GuildSettingField("x", "id", default=str(CHANNEL)), None) == (
        CHANNEL,
        None,
    )
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
    _write(
        tmp_path,
        f"{GUILD_MODULES_DIR}/{GUILD}/mod.md",
        f"---\nmod_log_channel_id: {CHANNEL}\ncount: 3\n---\n",
    )
    seen: list[int] = []
    unsubscribe = service.subscribe("mod", seen.append)
    service.refresh([GUILD])
    snapshot = service.get(GUILD, "mod")
    assert snapshot.valid
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


def test_server_document_does_not_supply_module_settings(tmp_path: Path) -> None:
    service, health = _service(tmp_path, mod=SCHEMA)
    _write(
        tmp_path,
        f"servers/{GUILD}.md",
        f"---\nbot_active: true\nmod_log_channel_id: {CHANNEL}\ncount: 2\nunrelated: x\n---\nbody\n",
    )
    service.refresh([GUILD])
    snapshot = service.get(GUILD, "mod")
    assert snapshot.valid is False
    assert snapshot.errors == ("count is required",)
    assert health == [("mod", "degraded", f"invalid guild settings in {GUILD}")]


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
    assert missing.valid and missing.values == {"channels": None}


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


def test_invalid_utf8_document_blocks_optional_enforcement_settings(tmp_path: Path) -> None:
    path = tmp_path / GUILD_MODULES_DIR / str(GUILD) / "enforcer.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")
    service, _ = _service(tmp_path, enforcer=OPTIONAL_ENFORCEMENT)

    service.refresh([GUILD])

    snapshot = service.get(GUILD, "enforcer")
    assert not snapshot.valid
    assert snapshot.errors and snapshot.errors[0].startswith("unreadable document:")
    assert service.blocked_guilds() == frozenset({GUILD})


def test_refresh_does_not_hold_cache_lock_while_reading(tmp_path: Path) -> None:
    read_started = Event()
    allow_read = Event()

    class BlockingReadService(GuildSettingsService):
        def _read(
            self, guild_id: int, module_name: str, schema: GuildSettingsSchema
        ) -> GuildSettingsSnapshot:
            read_started.set()
            allow_read.wait()
            return super()._read(guild_id, module_name, schema)

    service = BlockingReadService(config_dir=lambda: tmp_path, schemas={"mod": OPTIONAL})
    worker = Thread(target=service.refresh, args=([GUILD],))
    worker.start()
    assert read_started.wait(timeout=1)

    lock_was_free = service._lock.acquire(blocking=False)
    if lock_was_free:
        service._lock.release()
    allow_read.set()
    worker.join(timeout=1)

    assert lock_was_free is True
    assert worker.is_alive() is False


def test_stale_refresh_cannot_override_newer_enforcement_snapshot(tmp_path: Path) -> None:
    first_read_started = Event()
    allow_first_read = Event()
    call_lock = Lock()
    guild_calls = 0
    other_guild = GUILD + 1

    class OutOfOrderReadService(GuildSettingsService):
        def _read(
            self, guild_id: int, module_name: str, schema: GuildSettingsSchema
        ) -> GuildSettingsSnapshot:
            nonlocal guild_calls
            assert module_name == "mod"
            if guild_id == other_guild:
                return GuildSettingsSnapshot({"channels": ()}, True, (), "other-current")
            with call_lock:
                guild_calls += 1
                call_number = guild_calls
            if call_number == 1:
                first_read_started.set()
                allow_first_read.wait()
                return GuildSettingsSnapshot({"channels": ()}, True, (), "older-valid")
            return GuildSettingsSnapshot({}, False, ("newer invalid settings",), "newer-invalid")

    health: list[tuple[str, str, str]] = []
    service = OutOfOrderReadService(
        config_dir=lambda: tmp_path,
        schemas={"mod": OPTIONAL_ENFORCEMENT},
        on_health=lambda module, state, detail: health.append((module, state, detail)),
    )
    changed: list[int] = []
    service.subscribe("mod", changed.append)

    older = Thread(target=service.refresh, args=([GUILD, other_guild],))
    older.start()
    assert first_read_started.wait(timeout=1)

    # This later request finishes first and must remain authoritative for the
    # overlapping guild even after the older batch eventually applies.
    service.refresh([GUILD])
    assert service.blocked_guilds() == frozenset({GUILD})

    allow_first_read.set()
    older.join(timeout=1)

    assert older.is_alive() is False
    assert service.get(GUILD, "mod").revision == "newer-invalid"
    assert service.get(other_guild, "mod").revision == "other-current"
    assert service.blocked_guilds() == frozenset({GUILD})
    assert changed == [GUILD, other_guild]
    assert health == [("mod", "degraded", f"invalid guild settings in {GUILD}")]


def test_render_guild_settings_round_trips_through_the_host_parser() -> None:
    from kimi_agent_module_api import render_guild_settings
    from kimi_agent_module_api.contracts import GuildSettingField, GuildSettingsSchema
    from modules.guild_settings import coerce_document
    from utils.frontmatter import split_frontmatter_strict

    schema = GuildSettingsSchema(
        fields=(
            GuildSettingField("channel", "id"),
            GuildSettingField("ids", "id_list"),
            GuildSettingField("count", "int"),
            GuildSettingField("flag", "bool"),
            GuildSettingField("mode", "enum", choices=("a", "b")),
            GuildSettingField("note", "str"),
            GuildSettingField("notes", "str_list"),
            GuildSettingField("unset", "id"),
        )
    )
    values = {
        "channel": 123,
        "ids": (1, 2),
        "count": -4,
        "flag": True,
        "mode": "b",
        "note": "Great job: keep it up # true\nsecond line \x7f\x85 \u2028 é",
        "notes": ("yes", "no: maybe", "true"),
        "unset": None,
    }

    metadata, body = split_frontmatter_strict(render_guild_settings(values))
    coerced, errors = coerce_document(schema, metadata)

    assert body == ""
    assert errors == ()
    assert coerced == {**values, "unset": None}


def test_render_guild_settings_rejects_values_no_schema_kind_holds() -> None:
    from kimi_agent_module_api import render_guild_settings

    with pytest.raises(TypeError):
        render_guild_settings({"x": 1.5})
    with pytest.raises(TypeError):
        render_guild_settings({"x": {"nested": 1}})


@pytest.mark.parametrize("operation", ["refresh", "get"])
def test_retirement_during_read_cannot_restore_guild_blocks(tmp_path: Path, operation: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    read_started = Event()
    allow_read = Event()

    class BlockingReadService(GuildSettingsService):
        def _read(
            self, guild_id: int, module_name: str, schema: GuildSettingsSchema
        ) -> GuildSettingsSnapshot:
            if module_name == "retiring":
                read_started.set()
                assert allow_read.wait(timeout=5)
            return super()._read(guild_id, module_name, schema)

    service = BlockingReadService(
        config_dir=lambda: tmp_path,
        schemas={"retiring": SCHEMA, "healthy": OPTIONAL},
    )
    changes: list[int] = []
    service.subscribe("retiring", changes.append)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = (
            executor.submit(service.refresh, [GUILD])
            if operation == "refresh"
            else executor.submit(service.get, GUILD, "retiring")
        )
        try:
            assert read_started.wait(timeout=5)
            service.remove_module("retiring")
        finally:
            allow_read.set()
        future.result(timeout=5)

    assert service.blocked_guilds() == frozenset()
    assert changes == []
    assert "retiring" not in service.schemas
    assert (GUILD, "retiring") not in service._entries
    service.refresh([GUILD])
    assert service.get(GUILD, "healthy").valid
