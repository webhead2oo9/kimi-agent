from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from app.control_plane import (
    ControlPlaneStore,
    DocumentProposalHandler,
    RestartCoordinator,
    SettingsProposalHandler,
)
from app.proposals import DurableProposalService, ProposalNotPending
from kimi_agent_module_api import (
    ProposalActor,
    ProposalApplyResult,
    ProposalDraft,
    ProposalPreview,
)
from config.settings import Settings
from storage.db import Database


class _Handler:
    def __init__(self) -> None:
        self.revision = "one"
        self.applied = False

    async def preview(self, draft: ProposalDraft) -> ProposalPreview:
        return ProposalPreview(
            revision=self.revision,
            redacted_changes=draft.changes,
        )

    async def apply(self, proposal) -> ProposalApplyResult:
        self.applied = True
        return ProposalApplyResult(activation="live", revision="two", message="done")


@pytest.mark.asyncio
async def test_owner_approval_is_durable_and_single_use(tmp_path: Path) -> None:
    database = Database(tmp_path / "bot.db")
    await database.connect()
    try:
        service = DurableProposalService(database, owner_user_id="owner")
        handler = _Handler()
        service.register_handler("core", "config.test.update", handler)
        record = await service.create(
            "module",
            ProposalDraft(
                action="config.test.update",
                target="test",
                summary="Change test config",
                changes={"enabled": True},
                actor=ProposalActor(user_id="staff", source="test"),
            ),
        )
        with pytest.raises(PermissionError):
            await service.approve(record.proposal_id, owner_user_id="staff")
        applied = await service.approve(record.proposal_id, owner_user_id="owner")
        assert applied.state == "applied"
        assert handler.applied is True
        with pytest.raises(ProposalNotPending):
            await service.approve(record.proposal_id, owner_user_id="owner")
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_changed_revision_makes_proposal_stale(tmp_path: Path) -> None:
    database = Database(tmp_path / "bot.db")
    await database.connect()
    try:
        service = DurableProposalService(database, owner_user_id="owner")
        handler = _Handler()
        service.register_handler("core", "config.test.update", handler)
        record = await service.create(
            "module",
            ProposalDraft(
                action="config.test.update",
                target="test",
                summary="Change test config",
                changes={"enabled": True},
                actor=ProposalActor(user_id="staff", source="test"),
            ),
        )
        handler.revision = "changed"
        stale = await service.approve(record.proposal_id, owner_user_id="owner")
        assert stale.state == "stale"
        assert handler.applied is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_secret_store_encrypts_and_never_writes_plaintext(tmp_path: Path) -> None:
    key = base64.b64encode(b"k" * 32).decode()
    store = ControlPlaneStore(tmp_path, master_key=key)
    reference = await store.stage_secret("MODEL_API_KEY", "very-secret-value")
    assert reference.startswith("secret://")
    assert store.resolve_secret(reference) == "very-secret-value"
    assert b"very-secret-value" not in (tmp_path / "secrets.enc.json").read_bytes()


@pytest.mark.asyncio
async def test_settings_proposal_stages_restart_revision(tmp_path: Path) -> None:
    settings = Settings.model_validate({"owner_user_id": "owner"})
    store = ControlPlaneStore(tmp_path)
    restart = RestartCoordinator(enabled=False)
    handler = SettingsProposalHandler(
        store,
        settings.model_dump(mode="python"),
        restart,
    )
    draft = ProposalDraft(
        action="config.settings.update",
        target="settings",
        summary="Increase concurrency",
        changes={"llm_max_concurrency": 9},
        actor=ProposalActor(user_id="staff", source="test"),
    )
    preview = await handler.preview(draft)
    record = type(
        "Record",
        (),
        {
            "proposal_id": "abc",
            "summary": draft.summary,
            "changes": draft.changes,
        },
    )()
    result = await handler.apply(record)
    assert preview.activation == "restart"
    assert result.activation == "restart"
    assert restart.requested is True
    assert store.state()["pending"] == result.revision
    assert store.read_settings(result.revision)["llm_max_concurrency"] == 9


@pytest.mark.asyncio
async def test_live_document_proposal_validates_and_activates(tmp_path: Path) -> None:
    base = tmp_path / "config"
    base.mkdir()
    store = ControlPlaneStore(tmp_path / "control", base_config_dir=base)
    restart = RestartCoordinator(enabled=False)
    activated: list[Path] = []

    async def activate(path: Path) -> None:
        activated.append(path)

    handler = DocumentProposalHandler(store, activate, restart)
    content = "---\nbot_active: true\nstaff_user_ids: ['123']\n---\nGuild instructions.\n"
    draft = ProposalDraft(
        action="config.document.update",
        target="guild:123",
        summary="Enable a guild",
        changes={"content": content},
        actor=ProposalActor(user_id="staff", source="test"),
    )
    preview = await handler.preview(draft)
    record = type(
        "Record",
        (),
        {
            "proposal_id": "guild-change",
            "target": draft.target,
            "summary": draft.summary,
            "changes": draft.changes,
        },
    )()
    result = await handler.apply(record)

    assert preview.activation == "live"
    assert result.activation == "live"
    assert restart.requested is False
    assert activated == [store.revision_dir(result.revision) / "config"]
    assert store.read_document(Path("servers/123.md")) == content


@pytest.mark.asyncio
async def test_document_proposal_rejects_invalid_frontmatter(tmp_path: Path) -> None:
    store = ControlPlaneStore(tmp_path, base_config_dir=tmp_path / "config")

    async def activate(path: Path) -> None:
        del path

    handler = DocumentProposalHandler(store, activate, RestartCoordinator(enabled=False))
    draft = ProposalDraft(
        action="config.document.update",
        target="module:community_moderation",
        summary="Break module settings",
        changes={"content": "---\nenabled: [\n---\n"},
        actor=ProposalActor(user_id="staff", source="test"),
    )
    with pytest.raises(ValueError, match="invalid module configuration document"):
        await handler.preview(draft)


@pytest.mark.asyncio
async def test_failed_live_activation_rolls_back_revision(tmp_path: Path) -> None:
    base = tmp_path / "config"
    base.mkdir()
    original = base / "servers" / "123.md"
    original.parent.mkdir()
    original.write_text("original", encoding="utf-8")
    store = ControlPlaneStore(tmp_path / "control", base_config_dir=base)
    activations: list[Path] = []

    async def activate(path: Path) -> None:
        activations.append(path)
        if path != base.resolve():
            raise RuntimeError("refresh failed")

    handler = DocumentProposalHandler(store, activate, RestartCoordinator(enabled=False))
    record = type(
        "Record",
        (),
        {
            "proposal_id": "failed-live",
            "target": "guild:123",
            "summary": "Bad live refresh",
            "changes": {"content": "replacement"},
        },
    )()

    with pytest.raises(RuntimeError, match="refresh failed"):
        await handler.apply(record)

    assert store.state()["active"] is None
    assert store.state()["pending"] is None
    assert store.read_document(Path("servers/123.md")) == "original"
    assert activations[-1] == base.resolve()


@pytest.mark.asyncio
async def test_existing_schema_v1_adopts_control_plane_tables(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    database = Database(path)
    await database.connect()
    await database.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE control_proposal_events")
        connection.execute("DROP TABLE control_proposals")
        connection.commit()
    finally:
        connection.close()

    reopened = Database(path)
    await reopened.connect()
    try:
        async with reopened.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('control_proposals', 'control_proposal_events')"
        ) as cursor:
            assert {str(row[0]) for row in await cursor.fetchall()} == {
                "control_proposals",
                "control_proposal_events",
            }
    finally:
        await reopened.close()
