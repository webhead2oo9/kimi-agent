import asyncio
import json

import pytest

from evals.capture import InstrumentedRegistry
from evals.cassette import (
    Cassette,
    Fault,
    assert_unique_model_keys,
    call_key,
    cassette_model_key,
    cassette_path,
    load_cassette,
    tape_provenance,
)
from tools.registry import MessageContext
from trust.tiers import TrustTier


def _ctx():
    return MessageContext(
        user_id="u",
        user_name="n",
        guild_id=None,
        channel_id="c",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )


def _registry_with_probe():
    calls = {"live": 0}

    async def handler(args, ctx):
        calls["live"] += 1
        return json.dumps({"n": calls["live"]})

    registry = InstrumentedRegistry()
    registry.register(
        name="discord_text_search",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    return registry, calls


def _registry_with_search_probe(*, limit: int = 3, backend_calls: int = 2):
    calls = {"live": 0}

    async def handler(args, ctx):
        calls["live"] += 1
        ctx.internet_search_backend_calls_this_turn += backend_calls
        return json.dumps({"query": args.get("query"), "live": calls["live"]})

    registry = InstrumentedRegistry(
        internet_search_max_backend_calls_per_turn=limit,
    )
    registry.register(
        name="internet_search",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        min_tier=TrustTier.MEMBER,
    )
    return registry, calls


def test_call_key_is_order_insensitive():
    assert call_key("t", {"a": 1, "b": 2}) == call_key("t", {"b": 2, "a": 1})
    assert call_key("t", {"a": 1}) != call_key("u", {"a": 1})


def test_cassette_records_and_replays_in_order(tmp_path):
    cassette = Cassette(tmp_path / "s.json")
    cassette.record("discord_text_search", {"x": 1}, "r1")
    cassette.record("discord_text_search", {"x": 1}, "r2")
    assert cassette.replay("discord_text_search", {"x": 1}) == "r1"
    assert cassette.replay("discord_text_search", {"x": 1}) == "r2"
    # Exhausted keys repeat the last result so identical-call loops stay deterministic.
    assert cassette.replay("discord_text_search", {"x": 1}) == "r2"
    cassette.reset_cursors()
    assert cassette.replay("discord_text_search", {"x": 1}) == "r1"
    assert cassette.replay("discord_text_search", {"y": 9}) is None


def test_cassette_round_trips_through_disk(tmp_path):
    path = tmp_path / "s.json"
    cassette = Cassette(path)
    cassette.record("discord_text_search", {"x": 1}, "r1")
    cassette.save()
    assert path.exists()

    loaded = Cassette.load(path)
    assert len(loaded) == 1
    assert loaded.replay("discord_text_search", {"x": 1}) == "r1"

    loaded.clear()
    assert loaded.replay("discord_text_search", {"x": 1}) is None


def test_cassette_save_is_noop_when_clean(tmp_path):
    cassette = Cassette(tmp_path / "s.json")
    cassette.save()
    assert not (tmp_path / "s.json").exists()


def test_registry_record_mode_stays_live_and_records(tmp_path):
    registry, calls = _registry_with_probe()
    cassette = Cassette(tmp_path / "s.json")
    registry.configure_cassette(cassette, "record")

    result = asyncio.run(registry.dispatch("discord_text_search", {"x": 1}, _ctx()))
    assert json.loads(result) == {"n": 1}
    assert calls["live"] == 1
    assert registry.sink[-1].source == "live"
    assert cassette.replay("discord_text_search", {"x": 1}) == result


def test_registry_replay_mode_skips_live_handler(tmp_path):
    registry, calls = _registry_with_probe()
    cassette = Cassette(tmp_path / "s.json")
    cassette.record("discord_text_search", {"x": 1}, json.dumps({"recorded": True}))
    registry.configure_cassette(cassette, "replay")

    result = asyncio.run(registry.dispatch("discord_text_search", {"x": 1}, _ctx()))
    assert json.loads(result) == {"recorded": True}
    assert calls["live"] == 0
    assert registry.sink[-1].source == "replay"


def test_internet_search_replay_preserves_and_enforces_backend_budget(tmp_path):
    path = tmp_path / "s.json"
    cassette = Cassette(path)
    registry, calls = _registry_with_search_probe(limit=3, backend_calls=2)
    registry.configure_cassette(cassette, "record")
    args = {"query": "bounded"}

    live_ctx = _ctx()
    live_result = asyncio.run(registry.dispatch("internet_search", args, live_ctx))
    assert live_ctx.internet_search_backend_calls_this_turn == 2
    assert calls["live"] == 1
    cassette.save()

    payload = json.loads(path.read_text())
    assert payload["version"] == 2
    assert payload["entries"][0]["internet_search_backend_calls"] == [2]

    replay = Cassette.load(path)
    registry.configure_cassette(replay, "replay")
    replay_ctx = _ctx()
    assert asyncio.run(registry.dispatch("internet_search", args, replay_ctx)) == live_result
    assert replay_ctx.internet_search_backend_calls_this_turn == 2
    assert calls["live"] == 1

    exhausted = json.loads(asyncio.run(registry.dispatch("internet_search", args, replay_ctx)))
    assert exhausted["error"] == "Internet search call limit reached for this turn."
    assert replay_ctx.internet_search_backend_calls_this_turn == 2
    assert calls["live"] == 1
    assert registry.sink[-1].source == "replay"


def test_legacy_internet_search_entry_refreshes_live_with_budget_metadata(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "tool": "internet_search",
                        "args": {"query": "old"},
                        "results": [json.dumps({"stale": True})],
                    }
                ],
            }
        )
    )
    cassette = Cassette.load(path)
    registry, calls = _registry_with_search_probe(backend_calls=2)
    registry.configure_cassette(cassette, "replay")

    ctx = _ctx()
    result = asyncio.run(registry.dispatch("internet_search", {"query": "old"}, ctx))
    assert json.loads(result) == {"query": "old", "live": 1}
    assert calls["live"] == 1
    assert registry.sink[-1].source == "live"
    cassette.save()

    refreshed = json.loads(path.read_text())
    assert refreshed["version"] == 2
    assert refreshed["entries"][0]["internet_search_backend_calls"] == [2]


def test_legacy_only_search_tape_reports_none_and_record_mode_wipes_it(tmp_path):
    path = cassette_path(tmp_path, "s", "m")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "tool": "internet_search",
                        "args": {"query": "old"},
                        "results": [json.dumps({"stale": True})],
                    }
                ],
            }
        )
    )

    cassette, provenance = load_cassette(tmp_path, "s", "m")
    assert provenance == "none"
    cassette.clear()
    cassette.save()

    assert json.loads(path.read_text()) == {"version": 2, "entries": []}


def test_legacy_only_shared_search_tape_is_not_reported_as_replayable(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "tool": "internet_search",
                        "args": {"query": "old"},
                        "results": [json.dumps({"stale": True})],
                    }
                ],
            }
        )
    )

    cassette, provenance = load_cassette(tmp_path, "s", "m")
    assert provenance == "none"
    assert cassette.replay("internet_search", {"query": "old"}) is None


def test_registry_replay_mode_records_misses_live(tmp_path):
    registry, calls = _registry_with_probe()
    cassette = Cassette(tmp_path / "s.json")
    registry.configure_cassette(cassette, "replay")

    result = asyncio.run(registry.dispatch("discord_text_search", {"x": 2}, _ctx()))
    assert calls["live"] == 1
    assert registry.sink[-1].source == "live"
    # The miss was recorded, so the next identical call replays.
    cassette.reset_cursors()
    result2 = asyncio.run(registry.dispatch("discord_text_search", {"x": 2}, _ctx()))
    assert result2 == result
    assert calls["live"] == 1
    assert registry.sink[-1].source == "replay"


def test_registry_strict_mode_fails_misses_without_live_call(tmp_path):
    registry, calls = _registry_with_probe()
    cassette = Cassette(tmp_path / "s.json")
    registry.configure_cassette(cassette, "strict")

    result = asyncio.run(registry.dispatch("discord_text_search", {"x": 3}, _ctx()))
    assert "error" in json.loads(result)
    assert calls["live"] == 0
    record = registry.sink[-1]
    assert record.source == "miss"
    assert record.ok is False


def test_registry_fault_injection_consumes_budget_then_goes_live():
    registry, calls = _registry_with_probe()
    registry.set_faults([Fault(tool="discord_text_search", message="upstream 504", times=1)])

    first = asyncio.run(registry.dispatch("discord_text_search", {}, _ctx()))
    assert json.loads(first) == {"error": "upstream 504"}
    assert calls["live"] == 0
    assert registry.sink[-1].source == "fault"
    assert registry.sink[-1].ok is False

    second = asyncio.run(registry.dispatch("discord_text_search", {}, _ctx()))
    assert json.loads(second) == {"n": 1}
    assert registry.sink[-1].source == "live"


def test_unlisted_tools_bypass_replay_and_recording(tmp_path):
    # Only allowlisted network tools are cassette-recorded; anything else (here
    # browse_tools, whose handler mutates activation state) must run live even
    # when a matching recording exists.
    calls = {"live": 0}

    async def handler(args, ctx):
        calls["live"] += 1
        return json.dumps({"loaded": True})

    registry = InstrumentedRegistry()
    registry.register(
        name="browse_tools",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    cassette = Cassette(tmp_path / "s.json")
    cassette.record("browse_tools", {}, json.dumps({"stale": True}))
    registry.configure_cassette(cassette, "replay")

    result = asyncio.run(registry.dispatch("browse_tools", {}, _ctx()))
    # Live handler ran (side effects happen) despite a matching recording, and
    # the live result was not re-recorded.
    assert json.loads(result) == {"loaded": True}
    assert calls["live"] == 1
    assert registry.sink[-1].source == "live"
    cassette.reset_cursors()
    assert cassette.replay("browse_tools", {}) == json.dumps({"stale": True})


def test_cassette_records_covers_source_tools_and_plugin_declarations(monkeypatch):
    from app import tool_surfaces
    from evals.cassette import cassette_records

    monkeypatch.setattr(tool_surfaces, "_SURFACE_TOOLS", {})

    assert cassette_records("discord_text_search")
    assert cassette_records("internet_search")
    assert cassette_records("recall_user")
    # Downloads must stay live: replaying one would skip the workspace write
    # and outgoing-attachment side effects the eval is meant to exercise.
    assert not cassette_records("fetch_url")
    assert not cassette_records("write_file")
    assert not cassette_records("matplotlib_chart")
    assert not cassette_records("browse_tools")

    # A plugin's network-backed tool opts itself in; core never names it.
    assert not cassette_records("plugin_source_search")
    tool_surfaces.declare_surface_tools("eval_record", ["plugin_source_search"])
    assert cassette_records("plugin_source_search")


def test_cassette_path_is_model_scoped(tmp_path):
    path = cassette_path(tmp_path, "steam-player-count", "deepseek-v4-flash")
    assert path == tmp_path / "deepseek-v4-flash" / "steam-player-count.json"


def test_cassette_model_key_slugs_label():
    assert cassette_model_key("gpt-5.6-sol") == "gpt-5.6-sol"
    assert cassette_model_key("MiniMax M3") == "minimax-m3"
    # spec.model carries provider path segments; the key is slugged off the label
    # precisely so nothing like this ever becomes a nested directory.
    assert "/" not in cassette_model_key("accounts/fireworks/models/kimi-k3")
    with pytest.raises(ValueError):
        cassette_model_key("   ")


def test_assert_unique_model_keys_rejects_colliding_labels():
    assert_unique_model_keys(["kimi-k3", "gpt-5.6-sol", "minimax-m3"])
    # Two arms differing only in punctuation/case would share one tape directory,
    # which is the same cross-model contamination in a quieter costume.
    with pytest.raises(ValueError):
        assert_unique_model_keys(["MiniMax M3", "minimax-m3"])


def test_cassette_path_rejects_traversal_in_scenario_id(tmp_path):
    for bad in ("../escape", "sub/dir", "..", ""):
        with pytest.raises(ValueError):
            cassette_path(tmp_path, bad, "m")
    with pytest.raises(ValueError):
        cassette_path(tmp_path, "s", "../escape")


def test_layered_replay_falls_through_to_base(tmp_path):
    base = Cassette(tmp_path / "s.json")
    base.record("discord_text_search", {"x": 1}, "base-1")
    base.record("discord_text_search", {"x": 1}, "base-2")
    own = Cassette(tmp_path / "m" / "s.json", base=base)
    own.record("discord_text_search", {"y": 9}, "own-1")

    assert own.replay("discord_text_search", {"y": 9}) == "own-1"
    assert own.replay("discord_text_search", {"x": 1}) == "base-1"
    assert own.replay("discord_text_search", {"x": 1}) == "base-2"
    assert own.replay("discord_text_search", {"z": 0}) is None


def test_reset_cursors_resets_base_cursor(tmp_path):
    base = Cassette(tmp_path / "s.json")
    base.record("discord_text_search", {"x": 1}, "base-1")
    base.record("discord_text_search", {"x": 1}, "base-2")
    own = Cassette(tmp_path / "m" / "s.json", base=base)

    assert own.replay("discord_text_search", {"x": 1}) == "base-1"
    own.reset_cursors()
    assert own.replay("discord_text_search", {"x": 1}) == "base-1"


def test_promoted_internet_search_keeps_backend_budget_metadata(tmp_path):
    base = Cassette(tmp_path / "s.json")
    base.record(
        "internet_search",
        {"query": "q"},
        json.dumps({"results": []}),
        internet_search_backend_calls=2,
    )
    base.save()
    own_path = tmp_path / "m" / "s.json"
    own = Cassette(own_path, base=Cassette.load(base.path))

    replay = own.replay_record("internet_search", {"query": "q"})
    assert replay is not None
    assert replay.internet_search_backend_calls == 2
    own.save()

    promoted = Cassette.load(own_path).replay_record("internet_search", {"query": "q"})
    assert promoted is not None
    assert promoted.internet_search_backend_calls == 2


def test_save_writes_only_own_entries_and_leaves_base_file_untouched(tmp_path):
    base_path = tmp_path / "s.json"
    seed = Cassette(base_path)
    seed.record("discord_text_search", {"x": 1}, "shared-result")
    seed.save()
    before = base_path.read_bytes()

    cassette, _ = load_cassette(tmp_path, "s", "deepseek-v4-flash")
    assert cassette.replay("discord_text_search", {"x": 1}) == "shared-result"
    cassette.record("discord_text_search", {"x": 2}, "own-result")
    cassette.save()

    # Saving a per-model cassette must not rewrite the shared corpus it replayed from.
    assert base_path.read_bytes() == before
    own_path = cassette_path(tmp_path, "s", "deepseek-v4-flash")
    entries = json.loads(own_path.read_text())["entries"]
    keys = {call_key(entry["tool"], entry["args"]) for entry in entries}
    assert call_key("discord_text_search", {"x": 2}) in keys
    # The replayed baseline entry is promoted into the own tape, so the tape stops
    # being a permanent diff over a file that may be regenerated or renamed.
    assert call_key("discord_text_search", {"x": 1}) in keys


def test_second_run_replays_shared_entries_without_live_dispatch(tmp_path):
    seed = Cassette(tmp_path / "s.json")
    seed.record("discord_text_search", {"x": 1}, json.dumps({"shared": True}))
    seed.save()

    registry, calls = _registry_with_probe()
    cassette, provenance = load_cassette(tmp_path, "s", "m")
    assert provenance == "shared"
    registry.configure_cassette(cassette, "replay")
    asyncio.run(registry.dispatch("discord_text_search", {"x": 1}, _ctx()))
    asyncio.run(registry.dispatch("discord_text_search", {"x": 2}, _ctx()))
    assert calls["live"] == 1
    cassette.save()

    # Second run of the same arm: a per-model tape now exists, and nothing the
    # first run replayed may fall through live; otherwise run two of every model
    # is silently more expensive than run one.
    registry2, calls2 = _registry_with_probe()
    cassette2, provenance2 = load_cassette(tmp_path, "s", "m")
    assert provenance2 == "model"
    registry2.configure_cassette(cassette2, "replay")
    result = asyncio.run(registry2.dispatch("discord_text_search", {"x": 1}, _ctx()))
    asyncio.run(registry2.dispatch("discord_text_search", {"x": 2}, _ctx()))
    assert json.loads(result) == {"shared": True}
    assert calls2["live"] == 0


def test_load_cassette_reports_provenance(tmp_path):
    _, provenance = load_cassette(tmp_path, "s", "m")
    assert provenance == "none"

    shared = Cassette(tmp_path / "s.json")
    shared.record("discord_text_search", {"x": 1}, "shared-result")
    shared.save()
    cassette, provenance = load_cassette(tmp_path, "s", "m")
    assert provenance == "shared"
    assert cassette.replay("discord_text_search", {"x": 1}) == "shared-result"

    own = Cassette(cassette_path(tmp_path, "s", "m"))
    own.record("discord_text_search", {"x": 9}, "own-result")
    own.save()
    cassette, provenance = load_cassette(tmp_path, "s", "m")
    assert provenance == "model"
    # The baseline stays layered underneath an existing per-model tape.
    assert cassette.replay("discord_text_search", {"x": 9}) == "own-result"
    assert cassette.replay("discord_text_search", {"x": 1}) == "shared-result"


def test_promoted_baseline_entries_stay_marked_across_runs(tmp_path):
    # Promotion copies the shared baseline into the arm's own tape. If the copy
    # loses its provenance, the next run replays another arm's recordings out of
    # its own file and reports "model", and evals.compare's LOW CONFIDENCE
    # guard stops firing exactly once the correlation is permanent.
    seed = Cassette(tmp_path / "s.json")
    seed.record("discord_text_search", {"x": 1}, "shared-result")
    seed.save()

    first, provenance = load_cassette(tmp_path, "s", "m")
    assert provenance == "shared"
    assert first.replay("discord_text_search", {"x": 1}) == "shared-result"
    assert first.replayed_from_baseline
    first.record("discord_text_search", {"x": 2}, "own-result")
    first.save()

    entries = json.loads(cassette_path(tmp_path, "s", "m").read_text())["entries"]
    by_tool_args = {call_key(e["tool"], e["args"]): e for e in entries}
    assert by_tool_args[call_key("discord_text_search", {"x": 1})]["from_base"] is True
    # An entry this arm recorded itself carries no baseline-provenance marker.
    assert "from_base" not in by_tool_args[call_key("discord_text_search", {"x": 2})]

    second, provenance2 = load_cassette(tmp_path, "s", "m")
    assert provenance2 == "model"
    assert second.replay("discord_text_search", {"x": 1}) == "shared-result"
    assert tape_provenance(provenance2, second) == "promoted"


def test_tape_provenance_leaves_an_independent_tape_alone(tmp_path):
    own = Cassette(cassette_path(tmp_path, "s", "m"))
    own.record("discord_text_search", {"x": 1}, "own-result")
    own.save()

    cassette, provenance = load_cassette(tmp_path, "s", "m")
    assert cassette.replay("discord_text_search", {"x": 1}) == "own-result"
    assert not cassette.replayed_from_baseline
    assert tape_provenance(provenance, cassette) == "model"
    # The other two values are already honest about where the bytes came from.
    assert tape_provenance("shared", cassette) == "shared"
    assert tape_provenance("none", cassette) == "none"


def test_load_cassette_without_fallback_ignores_shared_tape(tmp_path):
    shared = Cassette(tmp_path / "s.json")
    shared.record("discord_text_search", {"x": 1}, "shared-result")
    shared.save()

    cassette, provenance = load_cassette(tmp_path, "s", "m", shared_fallback=False)
    assert provenance == "none"
    assert cassette.replay("discord_text_search", {"x": 1}) is None


def test_registry_configure_cassette_off_restores_plain_dispatch(tmp_path):
    registry, calls = _registry_with_probe()
    cassette = Cassette(tmp_path / "s.json")
    cassette.record("discord_text_search", {}, json.dumps({"recorded": True}))
    registry.configure_cassette(cassette, "replay")
    registry.configure_cassette(None)

    result = asyncio.run(registry.dispatch("discord_text_search", {}, _ctx()))
    assert json.loads(result) == {"n": 1}
    assert registry.sink[-1].source == "live"
