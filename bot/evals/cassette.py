"""Dispatch-level record/replay for eval tool calls.

The eval registry (`evals/capture.py:InstrumentedRegistry`) is the single
chokepoint every tool call flows through, so recording happens there rather
than per-client at the HTTP layer: one cassette file per scenario captures
`(tool, canonical args) -> result` and replays it on later runs, making
harness-eval repeats deterministic and free of live-backend calls. Replay
returns the recorded result *without* invoking the handler, so it measures the
model's tool-selection behavior against a frozen tool surface, exactly the
layer harness runs optimize.

Faults are the same layer used offensively: a scenario can declare that the
first N calls to a tool fail with a given message, which is how error-recovery
scenarios grade whether the model self-corrects after a tool error.

Tapes are **model-keyed** (`evals/cassettes/<model-key>/<scenario-id>.json`).
Cassette keys are `(tool, canonical args)` and the args are model-generated, so
one model's recordings are a poor match for another's calls; worse, the default
`replay` mode records misses and saves, so a shared file was silently rewritten
by whichever model ran last. A run now only ever writes its own tape.

The flat `evals/cassettes/*.json` tree is kept as a read-only **shared
baseline** layered underneath a per-model tape. That makes a per-model tape a
*diff* over the baseline rather than a standalone recording: deleting or
regenerating the flat tree, or renaming a scenario id, invalidates every
per-model tape and the next run dispatches those calls live (and spends). To
keep the diff from growing without bound, `save()` promotes every baseline entry
the run actually replayed into the own tape, so a tape converges on being
self-sufficient.

Promotion is *marked* (`from_base` per entry) and the mark survives into the tape
file, because otherwise promotion launders provenance: two arms that each
promoted the same baseline recordings both report tape provenance `"model"` while
replaying byte-identical tool results, and `evals.compare`'s LOW CONFIDENCE
guard, which exists to say a correlated observation is not two observations,
would stop firing exactly once the correlation had been baked into both tapes.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.tool_surfaces import surface_tools

log = logging.getLogger("evals.cassette")

CASSETTE_MODES = ("off", "record", "replay", "strict")
_INTERNET_SEARCH_TOOL = "internet_search"

# Only network/source tools are recorded; everything else always dispatches
# live. Local tools' handlers have side effects a replay would silently skip:
# browse_tools/load_skill activate tools on the context, plan rebinds the
# checklist, workspace and skill-script tools write real files and queue the
# attachment rail. Allowlisting the network edge keeps every unlisted (and
# future) tool correct by default; the only cost is that live-local dispatch
# is not replayed, and it was free anyway. Plugins add their own network-backed
# tools to this surface via app/tool_surfaces.py.
CASSETTE_RECORDED_TOOLS = frozenset(
    {
        "discord_text_search",
        # Live web search/read is network-backed and read-only. Replaying it
        # freezes volatile result ordering and content; its per-turn backend
        # budget effect is recorded alongside the result and reapplied.
        "internet_search",
        # Hindsight reads: network-backed and read-only, so replay is safe and
        # makes repeats deterministic.
        "recall_user",
        "reflect_user",
    }
)


def cassette_records(name: str) -> bool:
    return name in CASSETTE_RECORDED_TOOLS or name in surface_tools("eval_record")


_MODEL_KEY_UNSAFE = re.compile(r"[^a-z0-9._-]+")


def cassette_model_key(label: str) -> str:
    """Directory name for one eval arm's tapes, slugged from `ModelSpec.label`.

    The label rather than `spec.model` (which carries provider path segments like
    `accounts/fireworks/models/...`) or the CLI `--model` argument (which can be a
    second spelling of the same spec via `resolve_model_spec`, and would give one
    arm two tape directories).
    """
    key = _MODEL_KEY_UNSAFE.sub("-", label.strip().lower()).strip("-._")
    if not key:
        raise ValueError(f"model label {label!r} has no usable cassette key")
    return key


def assert_unique_model_keys(labels: Iterable[str]) -> None:
    """Refuse a config where two arms slug to one tape directory.

    Labels are not unique by construction and the key encodes neither provider
    nor reasoning effort, so two arms of the same model at different efforts
    would share a tape: the same cross-model contamination the model-keyed
    layout exists to stop, just quieter.
    """
    seen: dict[str, str] = {}
    for label in labels:
        key = cassette_model_key(label)
        other = seen.get(key)
        if other is not None and other != label:
            raise ValueError(
                f"model labels {other!r} and {label!r} both map to cassette key {key!r}; "
                "give them distinct labels in models.yaml"
            )
        seen[key] = label


def _safe_component(value: str) -> str:
    """One path component derived from external text (a scenario id or model key).

    Scenario ids come straight from YAML and reach the filesystem unvalidated, so
    a separator or `..` would put a tape outside the cassette tree.
    """
    cleaned = value.strip()
    if not cleaned or cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"unusable cassette path component: {value!r}")
    return cleaned


def cassette_path(base: Path, scenario_id: str, model_key: str) -> Path:
    return Path(base) / _safe_component(model_key) / f"{_safe_component(scenario_id)}.json"


def shared_cassette_path(base: Path, scenario_id: str) -> Path:
    """The committed flat-tree tape: a read-only baseline of mixed provenance."""
    return Path(base) / f"{_safe_component(scenario_id)}.json"


def call_key(tool: str, args: dict[str, Any]) -> str:
    """Canonical identity of a tool call: name + sorted-key JSON of its args."""
    try:
        canonical = json.dumps(args, sort_keys=True, default=str)
    except TypeError, ValueError:
        canonical = str(args)
    return f"{tool}\x1f{canonical}"


@dataclass(frozen=True)
class Fault:
    """Inject a failure for the next `times` calls to `tool` (scenario-declared)."""

    tool: str
    message: str
    times: int = 1


@dataclass
class _Entry:
    tool: str
    args: dict[str, Any]
    results: list[str]
    internet_search_backend_calls: list[int] | None = None
    # True when this entry was copied out of the shared baseline rather than
    # recorded by the arm that owns the tape. Persisted, so the provenance is
    # still knowable on the run after the promotion.
    from_base: bool = False


@dataclass(frozen=True)
class CassetteReplay:
    """One replayed result plus local effects the live handler would apply."""

    result: str
    internet_search_backend_calls: int | None = None


class Cassette:
    """Recorded tool results for one scenario.

    Repeated calls with the same key replay in recording order; once the list
    is exhausted the last result repeats, so a loop of identical calls stays
    deterministic instead of dying on an index error.

    `base` is a read-only underlay (the shared flat-tree tape). It is consulted
    only after the own store misses, and nothing ever writes through to it,
    because the committed file must come out of a run byte-identical.
    """

    VERSION = 2

    def __init__(self, path: Path, *, base: Cassette | None = None) -> None:
        self.path = path
        self._store: dict[str, _Entry] = {}
        self._cursors: dict[str, int] = {}
        self._dirty = False
        self._base = base
        self._from_base: set[str] = set()
        self._baseline_replays = 0

    @classmethod
    def load(cls, path: str | Path, *, base: Cassette | None = None) -> Cassette:
        cassette = cls(Path(path), base=base)
        if not cassette.path.exists():
            return cassette
        raw = json.loads(cassette.path.read_text(encoding="utf-8"))
        for item in raw.get("entries", []):
            tool = str(item.get("tool", ""))
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            results = [str(r) for r in (item.get("results") or [])]
            if not tool or not results:
                continue
            backend_calls: list[int] | None = None
            if tool == _INTERNET_SEARCH_TOOL:
                raw_calls = item.get("internet_search_backend_calls")
                if not (
                    isinstance(raw_calls, list)
                    and len(raw_calls) == len(results)
                    and all(
                        isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        for value in raw_calls
                    )
                ):
                    # Version-1 search recordings lack the local budget effect.
                    # Treat them as misses so replay mode refreshes them live
                    # instead of guessing how many providers were contacted.
                    log.warning(
                        "Ignoring legacy internet_search cassette entry without "
                        "backend-call metadata in %s",
                        cassette.path,
                    )
                    continue
                backend_calls = list(raw_calls)
            key = call_key(tool, args)
            from_base = bool(item.get("from_base"))
            entry = cassette._store.get(key)
            if entry is None:
                cassette._store[key] = _Entry(
                    tool=tool,
                    args=args,
                    results=results,
                    internet_search_backend_calls=backend_calls,
                    from_base=from_base,
                )
            else:
                entry.results.extend(results)
                if entry.internet_search_backend_calls is not None:
                    assert backend_calls is not None
                    entry.internet_search_backend_calls.extend(backend_calls)
                entry.from_base = entry.from_base or from_base
        return cassette

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        """Drop all entries (fresh recording). The underlay is not ours to clear."""
        # A legacy-only tape may have no usable entries after load discarded
        # invalid search recordings. Record mode must still replace that file,
        # even when this run records no new calls.
        if self._store or self.path.exists():
            self._dirty = True
        self._store = {}
        self._cursors = {}
        self._from_base = set()
        self._baseline_replays = 0

    @property
    def replayed_from_baseline(self) -> bool:
        """Whether any result served this run originated in the shared baseline.

        Covers both the underlay hit and the already-promoted own-store entry.
        After a promotion the second case is the only one left, and it is the one
        that reads as an independent recording if nothing tracks it.
        """
        return self._baseline_replays > 0

    def reset_cursors(self) -> None:
        """Start replay from the top (call between repetitions of a scenario)."""
        self._cursors = {}
        if self._base is not None:
            self._base.reset_cursors()

    def record(
        self,
        tool: str,
        args: dict[str, Any],
        result: str,
        *,
        internet_search_backend_calls: int | None = None,
    ) -> None:
        if tool == _INTERNET_SEARCH_TOOL:
            if (
                not isinstance(internet_search_backend_calls, int)
                or isinstance(internet_search_backend_calls, bool)
                or internet_search_backend_calls < 0
            ):
                raise ValueError(
                    "internet_search cassette results require a non-negative backend-call count"
                )
        elif internet_search_backend_calls is not None:
            raise ValueError("backend-call metadata is only valid for internet_search")
        key = call_key(tool, args)
        entry = self._store.get(key)
        if entry is None:
            self._store[key] = _Entry(
                tool=tool,
                args=dict(args),
                results=[result],
                internet_search_backend_calls=(
                    [internet_search_backend_calls]
                    if internet_search_backend_calls is not None
                    else None
                ),
            )
        else:
            entry.results.append(result)
            if entry.internet_search_backend_calls is not None:
                assert internet_search_backend_calls is not None
                entry.internet_search_backend_calls.append(internet_search_backend_calls)
        self._dirty = True

    def replay(self, tool: str, args: dict[str, Any]) -> str | None:
        """Return the next recorded result for this call, or None on a miss."""
        replay = self.replay_record(tool, args)
        return replay.result if replay is not None else None

    def replay_record(self, tool: str, args: dict[str, Any]) -> CassetteReplay | None:
        """Return the next result and its recorded local effects, or None."""
        key = call_key(tool, args)
        entry = self._store.get(key)
        if entry is None:
            if self._base is None:
                return None
            replay = self._base.replay_record(tool, args)
            if replay is not None:
                self._from_base.add(key)
                self._baseline_replays += 1
            return replay
        if entry.from_base:
            self._baseline_replays += 1
        cursor = self._cursors.get(key, 0)
        result_index = min(cursor, len(entry.results) - 1)
        result = entry.results[result_index]
        backend_calls = (
            entry.internet_search_backend_calls[result_index]
            if entry.internet_search_backend_calls is not None
            else None
        )
        self._cursors[key] = cursor + 1
        return CassetteReplay(
            result=result,
            internet_search_backend_calls=backend_calls,
        )

    def _promote_base_entries(self) -> None:
        """Copy every underlay entry this run replayed into the own store.

        Without this a per-model tape stays a permanent diff over the shared
        baseline: regenerate or rename the baseline and those calls silently go
        live again on the next run, spending real money with no signal.
        """
        if self._base is None:
            return
        for key in self._from_base:
            if key in self._store:
                continue
            entry = self._base._store.get(key)
            if entry is None:
                continue
            self._store[key] = _Entry(
                tool=entry.tool,
                args=dict(entry.args),
                results=list(entry.results),
                internet_search_backend_calls=(
                    list(entry.internet_search_backend_calls)
                    if entry.internet_search_backend_calls is not None
                    else None
                ),
                from_base=True,
            )
            self._dirty = True
        self._from_base = set()

    def save(self) -> None:
        """Write the cassette back to disk if anything changed."""
        self._promote_base_entries()
        if not self._dirty:
            return
        entries = [
            {
                "tool": entry.tool,
                "args": entry.args,
                "results": entry.results,
                **(
                    {"internet_search_backend_calls": entry.internet_search_backend_calls}
                    if entry.internet_search_backend_calls is not None
                    else {}
                ),
                # Written only when set, so an independently recorded tape stays
                # free of baseline-provenance metadata.
                **({"from_base": True} if entry.from_base else {}),
            }
            for _, entry in sorted(self._store.items())
        ]
        payload = {"version": self.VERSION, "entries": entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self._dirty = False


def load_cassette(
    base_dir: Path,
    scenario_id: str,
    model_key: str,
    *,
    shared_fallback: bool = True,
) -> tuple[Cassette, str]:
    """Open this arm's tape for a scenario, plus where its recordings come from.

    Provenance is `"model"` (this arm has its own tape), `"shared"` (only the
    flat-tree baseline, i.e. this run replays recordings some other arm made), or
    `"none"` (nothing recorded: every call dispatches live and costs money).
    `tape_provenance` narrows `"model"` to `"promoted"` after the run when the
    results actually replayed came out of the baseline.

    The baseline is layered whenever it exists and the fallback is on, *including*
    when a per-model tape is already present: the first run of an arm records only
    its misses, so dropping the underlay on the second run would turn every
    baseline-served call back into a live dispatch, silently making run two more
    expensive than run one. The underlay is read-only, so there is no conflict.
    """
    base_dir = Path(base_dir)
    own_path = cassette_path(base_dir, scenario_id, model_key)
    shared_path = shared_cassette_path(base_dir, scenario_id)
    shared: Cassette | None = None
    if shared_fallback and shared_path.exists():
        loaded_shared = Cassette.load(shared_path)
        if len(loaded_shared) > 0:
            shared = loaded_shared
    cassette = Cassette.load(own_path, base=shared)
    if len(cassette) > 0:
        return cassette, "model"
    if shared is not None:
        # Silent orphaning is the money-losing failure, so the fallback is on by
        # default, but a run leaning on another arm's recordings should say so.
        log.warning(
            "Scenario %r has no %s tape; falling back to the shared baseline %s",
            scenario_id,
            model_key,
            shared_path,
        )
        return cassette, "shared"
    return cassette, "none"


# Provenance values whose recordings came out of the shared flat-tree baseline,
# so two arms carrying one of these for a scenario replayed the same bytes.
BASELINE_PROVENANCE = ("shared", "promoted")


def tape_provenance(loaded: str, cassette: Cassette) -> str:
    """Load-time provenance, narrowed by what the run actually replayed.

    An own tape whose replayed entries were promoted out of the baseline is not
    an independent recording, and reporting it as `"model"` is what lets two arms
    look like two observations of one failure while reading identical bytes. The
    refinement keys on entries actually served this run rather than on the tape's
    contents, because a promoted entry the model never called again correlates
    nothing.
    """
    if loaded == "model" and cassette.replayed_from_baseline:
        return "promoted"
    return loaded
