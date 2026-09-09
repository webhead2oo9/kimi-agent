from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from storage.db import Database
from storage.conversations import ConversationStore
from storage.usage import (
    ImageUsageLimitExceeded,
    ImageUsageLimits,
    ImageUsageRequest,
    PaidUsageCall,
    UsageStore,
)
from usage.normalization import LLMUsageCall, UsageBreakdown


async def _store(tmp_path):
    db = Database(path=tmp_path / "usage.db")
    await db.connect()
    return db, UsageStore(db)


@pytest.mark.asyncio
async def test_core_database_has_prompt_free_image_usage_reservation_ledger(tmp_path) -> None:
    db = Database(tmp_path / "usage.db")
    await db.connect()
    try:
        async with db.conn.execute("PRAGMA table_info(image_usage_reservations)") as cursor:
            columns = {str(row["name"]) for row in await cursor.fetchall()}
        assert {
            "reservation_id",
            "user_id",
            "guild_id",
            "backend",
            "provider",
            "model",
            "operation",
            "estimated_cost_usd",
            "state",
            "provider_request_id",
            "created_at",
            "updated_at",
        } <= columns
        assert not {"prompt", "image", "image_bytes", "reference_images"} & columns
        assert not {"actual_cost_usd", "cost_source"} & columns
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_image_usage_reservation_atomically_enforces_user_rolling_limit(tmp_path) -> None:
    db, store = await _store(tmp_path)
    request = ImageUsageRequest(
        backend="openai_api",
        provider="openai",
        model="gpt-image-2",
        operation="generate",
        size="1024x1024",
        quality="high",
        estimated_cost_usd=0.25,
    )
    try:
        outcomes = await asyncio.gather(
            *(
                store.reserve_image_usage(
                    user_id="u1",
                    user_name="Ann",
                    channel_id="c",
                    guild_id="g",
                    request=request,
                    limits=ImageUsageLimits(
                        per_user_24h=1,
                        per_guild_24h=0,
                        deployment_monthly_usd=0,
                    ),
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
        rejection = next(
            outcome for outcome in outcomes if isinstance(outcome, ImageUsageLimitExceeded)
        )
        assert rejection.scope == "user_24h"
        assert rejection.resets_at > datetime.now(UTC)
        async with db.conn.execute("SELECT COUNT(*) FROM image_usage_reservations") as cursor:
            row = await cursor.fetchone()
        assert row is not None and row[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_image_reservation_is_atomic_across_database_connections(tmp_path) -> None:
    path = tmp_path / "usage.db"
    first_db = Database(path)
    second_db = Database(path)
    await first_db.connect()
    await second_db.connect()
    request = ImageUsageRequest(
        backend="openai_api",
        provider="openai",
        model="gpt-image-2",
        operation="generate",
        size="1024x1024",
        quality="high",
        estimated_cost_usd=0.30,
    )
    limits = ImageUsageLimits(1, 0, 0)
    try:
        outcomes = await asyncio.gather(
            *(
                UsageStore(db).reserve_image_usage(
                    user_id="u1",
                    user_name="Ann",
                    channel_id="c",
                    guild_id="g",
                    request=request,
                    limits=limits,
                )
                for db in (first_db, second_db)
            ),
            return_exceptions=True,
        )

        assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
        assert sum(isinstance(outcome, ImageUsageLimitExceeded) for outcome in outcomes) == 1
    finally:
        await first_db.close()
        await second_db.close()


@pytest.mark.asyncio
async def test_rolling_image_limit_expires_exactly_after_24_hours(tmp_path) -> None:
    db, store = await _store(tmp_path)
    request = ImageUsageRequest(
        backend="openai_api",
        provider="openai",
        model="gpt-image-2",
        operation="generate",
        size="1024x1024",
        quality="high",
        estimated_cost_usd=0.30,
    )
    limits = ImageUsageLimits(1, 0, 0)
    start = datetime(2026, 9, 1, 12, tzinfo=UTC)
    try:
        await store.reserve_image_usage(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            request=request,
            limits=limits,
            now=start,
        )

        reservation = await store.reserve_image_usage(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            request=request,
            limits=limits,
            now=start + timedelta(hours=24),
        )

        assert reservation.estimated_cost_usd == 0.30
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_privacy_anonymization_does_not_reset_image_call_limit(tmp_path) -> None:
    db, store = await _store(tmp_path)
    request = ImageUsageRequest(
        backend="openai_api",
        provider="openai",
        model="gpt-image-2",
        operation="generate",
        size=None,
        quality=None,
        estimated_cost_usd=0.30,
    )
    limits = ImageUsageLimits(1, 0, 0)
    try:
        await store.reserve_image_usage(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            request=request,
            limits=limits,
        )
        deletion = await ConversationStore(db).delete_user_data("u1")

        with pytest.raises(ImageUsageLimitExceeded) as exc_info:
            await store.reserve_image_usage(
                user_id="u1",
                user_name="Ann",
                channel_id="c",
                guild_id="g",
                request=request,
                limits=limits,
            )

        assert deletion.image_usage_records_anonymized == 1
        assert exc_info.value.scope == "user_24h"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_zero_disables_all_persistent_image_limits(tmp_path) -> None:
    db, store = await _store(tmp_path)
    request = ImageUsageRequest(
        backend="openai_api",
        provider="openai",
        model="gpt-image-2",
        operation="generate",
        size=None,
        quality=None,
        estimated_cost_usd=1_000_000.0,
    )
    try:
        for _ in range(3):
            await store.reserve_image_usage(
                user_id="same-user",
                user_name=None,
                channel_id=None,
                guild_id="same-guild",
                request=request,
                limits=ImageUsageLimits(0, 0, 0),
            )

        async with db.conn.execute("SELECT COUNT(*) FROM image_usage_reservations") as cursor:
            row = await cursor.fetchone()
        assert row is not None and row[0] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_staff_call_exemption_never_bypasses_deployment_ceiling(tmp_path) -> None:
    db, store = await _store(tmp_path)
    request = ImageUsageRequest(
        backend="openai_api",
        provider="openai",
        model="gpt-image-2",
        operation="edit",
        size="1024x1024",
        quality="high",
        estimated_cost_usd=0.20,
    )
    limits = ImageUsageLimits(
        per_user_24h=1,
        per_guild_24h=1,
        deployment_monthly_usd=0.25,
        staff_exempt_from_call_limits=True,
    )
    try:
        await store.reserve_image_usage(
            user_id="staff",
            user_name="Staff",
            channel_id="c",
            guild_id="g",
            request=request,
            limits=limits,
            is_staff=True,
        )
        with pytest.raises(ImageUsageLimitExceeded) as exc_info:
            await store.reserve_image_usage(
                user_id="staff",
                user_name="Staff",
                channel_id="c",
                guild_id="g",
                request=request,
                limits=limits,
                is_staff=True,
            )
        assert exc_info.value.scope == "deployment_monthly_usd"
        assert exc_info.value.resets_at.day == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_usage_totals_report_conservative_image_reservations_separately(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        await store.reserve_image_usage(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            request=ImageUsageRequest(
                backend="openai_api",
                provider="openai",
                model="gpt-image-2",
                operation="generate",
                size="1024x1024",
                quality="high",
                estimated_cost_usd=0.30,
            ),
            limits=ImageUsageLimits(0, 0, 0),
        )

        total = await store.user_total("u1", datetime.now(UTC) - timedelta(hours=1))

        assert total.image_est_cost_usd == 0.30
        assert total.image_calls == 1
        assert total.est_cost_usd == 0.30
        assert total.paid_tool_cost_usd == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_uncertain_image_outcome_keeps_the_configured_reservation_counted(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        reservation = await store.reserve_image_usage(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            request=ImageUsageRequest(
                backend="openai_api",
                provider="openai",
                model="gpt-image-2.5-flare",
                operation="generate",
                size="1024x1024",
                quality="high",
                estimated_cost_usd=0.30,
            ),
            limits=ImageUsageLimits(0, 0, 0),
        )
        await store.finalize_image_usage(
            reservation.reservation_id,
            state="failed_uncertain",
        )

        total = await store.user_total("u1", datetime.now(UTC) - timedelta(hours=1))
        async with db.conn.execute(
            "SELECT state, estimated_cost_usd FROM image_usage_reservations"
        ) as cursor:
            row = await cursor.fetchone()

        assert row is not None
        assert row["state"] == "failed_uncertain"
        assert row["estimated_cost_usd"] == 0.30
        assert total.image_est_cost_usd == 0.30
        assert total.image_calls == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_image_only_user_appears_in_top_spenders_with_reserved_estimate(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        await store.reserve_image_usage(
            user_id="image-user",
            user_name="Image User",
            channel_id="c",
            guild_id="g",
            request=ImageUsageRequest(
                backend="openai_api",
                provider="openai",
                model="gpt-image-2.5-sunburst",
                operation="edit",
                size="1024x1024",
                quality="high",
                estimated_cost_usd=0.45,
            ),
            limits=ImageUsageLimits(0, 0, 0),
        )

        top = await store.top_spenders("g", datetime.now(UTC) - timedelta(hours=1), limit=10)

        assert len(top) == 1
        assert top[0].user_id == "image-user"
        assert top[0].est_cost_usd == 0.45
        assert top[0].image_est_cost_usd == 0.45
        assert top[0].image_calls == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_usage_markers_are_queryable_without_affecting_spend(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        await store.record_usage_marker(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            surface="run_code",
            operation="run",
        )
        markers = await store.usage_markers(
            "u1",
            surfaces=("run_code",),
            since=datetime.now(UTC) - timedelta(hours=1),
        )
        aggregate = await store.user_total("u1", datetime.now(UTC) - timedelta(hours=1))
    finally:
        await db.close()

    assert len(markers) == 1
    assert markers[0].unit_count == 1
    assert aggregate.paid_tool_calls == 0
    assert aggregate.est_cost_usd == 0


@pytest.mark.asyncio
async def test_record_and_user_total(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        await store.record_turn(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            calls=[
                LLMUsageCall(
                    model="minimax-m3",
                    role="chat",
                    usage=UsageBreakdown(
                        input_tokens=100,
                        cached_read_tokens=200,
                        output_tokens=40,
                    ),
                    est_cost_usd=0.05,
                )
            ],
        )
        await store.record_turn(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            calls=[
                LLMUsageCall(
                    model="minimax-m3",
                    role="chat",
                    usage=UsageBreakdown(output_tokens=10),
                    est_cost_usd=None,
                )
            ],
        )

        since = datetime.now(UTC) - timedelta(hours=1)
        agg = await store.user_total("u1", since)
    finally:
        await db.close()

    assert agg.turns == 2
    assert agg.input_tokens == 100
    assert agg.output_tokens == 50
    assert agg.est_cost_usd == pytest.approx(0.05)
    assert agg.unpriced_llm_calls == 1


@pytest.mark.asyncio
async def test_missing_usage_and_reported_zero_remain_distinct_in_storage(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        await store.record_turn(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            calls=[
                LLMUsageCall(
                    model="m",
                    role="chat",
                    usage=UsageBreakdown(),
                    usage_present=False,
                    est_cost_usd=None,
                ),
                LLMUsageCall(
                    model="m",
                    role="chat",
                    usage=UsageBreakdown(),
                    est_cost_usd=0.0,
                ),
            ],
        )
        aggregate = await store.user_total("u1", datetime.now(UTC) - timedelta(hours=1))
        async with db.conn.execute("SELECT est_cost_usd FROM usage_ledger ORDER BY id") as cursor:
            costs = [row["est_cost_usd"] for row in await cursor.fetchall()]
    finally:
        await db.close()

    assert costs == [None, 0.0]
    assert aggregate.llm_calls == 2
    assert aggregate.est_cost_usd == 0.0
    assert aggregate.unpriced_llm_calls == 1


@pytest.mark.asyncio
async def test_paid_tool_usage_is_separate_and_included_in_known_cost(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        await store.record_turn(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            calls=[
                LLMUsageCall(
                    model="m",
                    role="chat",
                    usage=UsageBreakdown(output_tokens=10),
                    est_cost_usd=0.20,
                )
            ],
            turn_id="turn-1",
        )
        await store.record_paid_usage(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            calls=[
                PaidUsageCall("internet_search", "exa", 0.01),
                PaidUsageCall("internet_search", "brave", 0.02),
            ],
            turn_id="turn-1",
        )

        total = await store.user_total("u1", datetime.now(UTC) - timedelta(hours=1))
        async with db.conn.execute(
            "SELECT provider, cost_usd FROM paid_usage_ledger ORDER BY provider"
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()

    assert [(row["provider"], row["cost_usd"]) for row in rows] == [
        ("brave", 0.02),
        ("exa", 0.01),
    ]
    assert total.llm_est_cost_usd == pytest.approx(0.20)
    assert total.paid_tool_cost_usd == pytest.approx(0.03)
    assert total.est_cost_usd == pytest.approx(0.23)
    assert total.paid_tool_calls == 2
    assert total.turns == 1


@pytest.mark.asyncio
async def test_free_paid_tool_report_creates_no_ledger_row(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        await store.record_paid_usage(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            calls=[PaidUsageCall("internet_search", "exa", 0.0)],
        )
        async with db.conn.execute("SELECT COUNT(*) FROM paid_usage_ledger") as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    assert row[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [-0.01, float("inf"), float("nan")])
async def test_invalid_paid_tool_cost_is_rejected(tmp_path, cost: float) -> None:
    db, store = await _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="finite and non-negative"):
            await store.record_paid_usage(
                user_id="u1",
                user_name="Ann",
                channel_id="c",
                guild_id="g",
                calls=[PaidUsageCall("internet_search", "exa", cost)],
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_window_excludes_old_rows_and_guild_filter(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        for uid, gid, cost in [
            ("u1", "g1", 1.0),
            ("u2", "g1", 2.0),
            ("u3", "g2", 5.0),
        ]:
            await store.record_turn(
                user_id=uid,
                user_name=uid,
                channel_id="c",
                guild_id=gid,
                calls=[
                    LLMUsageCall(
                        model="m",
                        role="chat",
                        usage=UsageBreakdown(output_tokens=1),
                        est_cost_usd=cost,
                    )
                ],
            )

        since = datetime.now(UTC) - timedelta(hours=1)
        g1 = await store.server_total("g1", since)
        all_rows = await store.server_total(None, since)
        top = await store.top_spenders("g1", since, limit=1)
        future = datetime.now(UTC) + timedelta(hours=1)
        future_total = await store.server_total(None, future)
    finally:
        await db.close()

    assert g1.turns == 2 and g1.est_cost_usd == pytest.approx(3.0)
    assert all_rows.turns == 3 and all_rows.est_cost_usd == pytest.approx(8.0)
    assert len(top) == 1 and top[0].user_id == "u2"
    assert future_total.turns == 0


@pytest.mark.asyncio
async def test_paid_only_user_is_in_server_totals_and_top_spenders(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        await store.record_paid_usage(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            calls=[PaidUsageCall("internet_search", "exa", 0.25)],
            turn_id="paid-only-turn",
        )
        since = datetime.now(UTC) - timedelta(hours=1)
        total = await store.server_total("g", since)
        top = await store.top_spenders("g", since, limit=10)
    finally:
        await db.close()

    assert total.est_cost_usd == pytest.approx(0.25)
    assert total.paid_tool_cost_usd == pytest.approx(0.25)
    assert total.turns == 1
    assert len(top) == 1
    assert top[0].user_id == "u1"
    assert top[0].paid_tool_cost_usd == pytest.approx(0.25)
    assert top[0].paid_tool_calls == 1
    assert top[0].turns == 1


@pytest.mark.asyncio
async def test_user_total_scopes_to_guild(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        for gid, cost in [("g1", 1.0), ("g2", 4.0)]:
            await store.record_turn(
                user_id="u1",
                user_name="Ann",
                channel_id="c",
                guild_id=gid,
                calls=[
                    LLMUsageCall(
                        model="m",
                        role="chat",
                        usage=UsageBreakdown(output_tokens=1),
                        est_cost_usd=cost,
                    )
                ],
            )

        since = datetime.now(UTC) - timedelta(hours=1)
        g1 = await store.user_total("u1", since, guild_id="g1")
        combined = await store.user_total("u1", since)
    finally:
        await db.close()

    assert g1.turns == 1
    assert g1.est_cost_usd == pytest.approx(1.0)
    assert combined.turns == 2
    assert combined.est_cost_usd == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_created_at_is_utc_iso_string(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        await store.record_turn(
            user_id="u1",
            user_name="Ann",
            channel_id="c",
            guild_id="g",
            calls=[
                LLMUsageCall(
                    model="m", role="chat", usage=UsageBreakdown(output_tokens=1), est_cost_usd=0.01
                )
            ],
        )
        async with db.conn.execute("SELECT created_at FROM usage_ledger") as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    assert datetime.fromisoformat(row["created_at"]).tzinfo is UTC


@pytest.mark.asyncio
async def test_unattributed_aggregate_rows_keep_honest_call_limits(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        async with db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO usage_ledger ("
                "user_id, model, role, input_tokens, output_tokens, iterations, "
                "est_cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "aggregate-user",
                    "configured-model",
                    "chat",
                    100,
                    20,
                    3,
                    None,
                    datetime.now(UTC).isoformat(),
                ),
            )
        since = datetime.now(UTC) - timedelta(hours=1)
        total = await store.user_total("aggregate-user", since)
    finally:
        await db.close()

    assert total.turns == 1
    assert total.llm_calls == 3
    assert total.unpriced_llm_calls == 3


@pytest.mark.asyncio
async def test_shared_turn_id_does_not_inflate_nested_llm_turns(tmp_path) -> None:
    db, store = await _store(tmp_path)
    try:
        for model, role, cost in (
            ("chat-model", "chat", 0.2),
            ("distill-model", "distill", 0.3),
        ):
            await store.record_turn(
                user_id="u1",
                user_name="Ann",
                channel_id="c",
                guild_id="g",
                calls=[
                    LLMUsageCall(
                        model=model,
                        role=role,
                        usage=UsageBreakdown(input_tokens=10, output_tokens=5),
                        est_cost_usd=cost,
                    )
                ],
                turn_id="parent-turn",
            )
        since = datetime.now(UTC) - timedelta(hours=1)
        total = await store.user_total("u1", since)
    finally:
        await db.close()

    assert total.turns == 1
    assert total.llm_calls == 2
    assert total.llm_est_cost_usd == pytest.approx(0.5)
    assert total.input_tokens == 20
