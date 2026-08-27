from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from storage.db import Database
from storage.usage import PaidUsageCall, UsageStore
from usage.normalization import LLMUsageCall, UsageBreakdown


async def _store(tmp_path):
    db = Database(path=tmp_path / "usage.db")
    await db.connect()
    return db, UsageStore(db)


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
async def test_usage_by_model_groups_by_model_and_role(tmp_path) -> None:
    """The same model is routinely wired to several roles.

    A per-model total alone hides "compaction costs more than chat", which is
    the question this breakdown exists to answer.
    """
    db, store = await _store(tmp_path)
    try:

        async def turn(model: str, role: str, cost: float | None, out: int) -> None:
            await store.record_turn(
                user_id="u1",
                user_name="Ann",
                channel_id="c",
                guild_id="g",
                calls=[
                    LLMUsageCall(
                        model=model,
                        role=role,
                        usage=UsageBreakdown(input_tokens=10, output_tokens=out),
                        est_cost_usd=cost,
                    )
                ],
            )

        await turn("big-model", "chat", 0.50, 100)
        await turn("big-model", "chat", 0.25, 50)
        await turn("big-model", "compaction", 0.10, 20)
        await turn("small-model", "distill", None, 5)

        since = datetime.now(UTC) - timedelta(hours=1)
        rows = await store.usage_by_model(since)
    finally:
        await db.close()

    assert [(r.model, r.role) for r in rows] == [
        ("big-model", "chat"),
        ("big-model", "compaction"),
        ("small-model", "distill"),
    ]
    chat = rows[0]
    assert chat.turns == 2
    assert chat.est_cost_usd == pytest.approx(0.75)
    assert chat.output_tokens == 150
    # A model with no configured price reports zero cost, so the unpriced count
    # is the only thing separating "free" from "unknown".
    assert rows[2].est_cost_usd == pytest.approx(0.0)
    assert rows[2].unpriced_llm_calls == 1


@pytest.mark.asyncio
async def test_usage_by_model_honors_the_scope_filters(tmp_path) -> None:
    """The breakdown must sum to the aggregate shown beside it."""
    db, store = await _store(tmp_path)
    try:
        for user_id, guild_id in (("u1", "g1"), ("u2", "g1"), ("u1", "g2")):
            await store.record_turn(
                user_id=user_id,
                user_name=user_id,
                channel_id="c",
                guild_id=guild_id,
                calls=[
                    LLMUsageCall(
                        model="m",
                        role="chat",
                        usage=UsageBreakdown(output_tokens=10),
                        est_cost_usd=0.10,
                    )
                ],
            )

        since = datetime.now(UTC) - timedelta(hours=1)
        everything = await store.usage_by_model(since)
        by_guild = await store.usage_by_model(since, guild_id="g1")
        by_user = await store.usage_by_model(since, user_id="u1")
        guild_total = await store.server_total("g1", since)
        user_total = await store.user_total("u1", since)
    finally:
        await db.close()

    assert everything[0].turns == 3
    assert by_guild[0].turns == 2
    assert by_guild[0].est_cost_usd == pytest.approx(guild_total.est_cost_usd)
    assert by_user[0].turns == 2
    assert by_user[0].est_cost_usd == pytest.approx(user_total.est_cost_usd)


@pytest.mark.asyncio
async def test_usage_by_model_excludes_rows_outside_the_window(tmp_path) -> None:
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
                    est_cost_usd=0.10,
                )
            ],
        )
        rows = await store.usage_by_model(datetime.now(UTC) + timedelta(hours=1))
    finally:
        await db.close()

    assert rows == []


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
        rows = await store.usage_by_model(since, user_id="aggregate-user")
    finally:
        await db.close()

    assert total.turns == 1
    assert total.llm_calls == 3
    assert total.unpriced_llm_calls == 3
    assert rows[0].attribution == "unattributed"
    assert rows[0].llm_calls == 3


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
        rows = await store.usage_by_model(since, user_id="u1")
    finally:
        await db.close()

    assert total.turns == 1
    assert total.llm_calls == 2
    assert sum(row.est_cost_usd for row in rows) == pytest.approx(total.llm_est_cost_usd)
    assert sum(row.input_tokens for row in rows) == total.input_tokens
