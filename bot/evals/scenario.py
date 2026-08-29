from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]

from evals.cassette import Fault
from trust.tiers import TrustTier, trust_tier_from_value

if TYPE_CHECKING:
    from evals.identity import EvalIdentity
    from tools.registry import ToolRegistry


@dataclass(frozen=True)
class Expect:
    should_use_tools: list[str] = field(default_factory=list)
    should_not_use_tools: list[str] = field(default_factory=list)
    notes: str = ""
    # 0 = uncapped; >0 flags a run whose total tool calls exceed the budget.
    max_tool_calls: int = 0
    # Regexes (case-insensitive) that must match somewhere in the bot's replies.
    reply_must_match: list[str] = field(default_factory=list)
    # True = at least one file must be queued on the outgoing-attachment rail.
    attaches_file: bool = False


@dataclass(frozen=True)
class TurnSpec:
    """One user message, optionally carrying images.

    ``images`` are attachments on the message itself (production's
    ``input_parts``); ``reply_images`` are attachments on the message this turn
    replies to, which travel a separate rail
    (``MessageContext.reply_image_parts``). Both name files under
    ``evals/fixtures/images/``. Keeping them distinct is the point: a tool that
    reads only one rail passes a test built on the other.
    """

    text: str
    images: tuple[str, ...] = ()
    reply_images: tuple[str, ...] = ()
    reply_author: str = "Ana"
    reply_text: str = ""

    @property
    def has_images(self) -> bool:
        return bool(self.images or self.reply_images)


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str
    trust_tier: TrustTier
    turns: list[TurnSpec]
    channel_name: str = ""
    channel_id: str = ""
    guild_name: str = ""
    guild_id: str = ""
    # (role, name, text), where name is "" for assistant rows.
    seeded_history: list[tuple[str, str, str]] = field(default_factory=list)
    activated_tools: list[str] = field(default_factory=list)
    expect: Expect = field(default_factory=Expect)
    # Injected failures (see evals/cassette.py:Fault) for error-recovery scenarios.
    faults: list[Fault] = field(default_factory=list)
    # Tools whose absence means "this host cannot run this scenario", not a model failure.
    # `browser` and `run_code` need a Linux sandbox the dev box does not have, so those
    # scenarios sit out a dev run and execute on the sandbox/prod box. Distinct from
    # expect.should_use_tools, whose absence is a hard error (see
    # harness_run.missing_expected_tools): declaring a tool here says the gap is
    # expected on some hosts, so an undeclared gap keeps failing loudly.
    requires_tools: list[str] = field(default_factory=list)
    # Trusted text files seeded through the real workspace tool before turn 1.
    # The eval identity keeps every repetition in its own temporary workspace.
    workspace_files: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        # A bare string is accepted wherever a turn is expected, and normalized
        # here. Almost every scenario and test builds turns as plain text, so
        # TurnSpec stays an internal detail rather than something each of them
        # has to import and wrap with.
        if any(isinstance(turn, str) for turn in self.turns):
            object.__setattr__(
                self,
                "turns",
                [TurnSpec(text=turn) if isinstance(turn, str) else turn for turn in self.turns],
            )


def _seeded(raw: list[dict[str, Any]] | None) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for item in raw or []:
        role = str(item.get("role", "user"))
        rows.append((role, str(item.get("name", "")), str(item.get("text", ""))))
    return rows


def _reply_checks(path: str | Path, raw: list[Any] | None) -> list[str]:
    checks: list[str] = []
    for pattern in raw or []:
        text = str(pattern)
        try:
            re.compile(text, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"Scenario {path}: bad reply_must_match regex {text!r}: {exc}"
            ) from exc
        checks.append(text)
    return checks


def _turns(path: str | Path, raw: list[Any] | None) -> list[TurnSpec]:
    """Parse `turns:`, where an entry is plain text or a mapping with images.

    The string form stays the norm: most scenarios have no attachments and
    should not pay a nesting level for the ones that do.
    """
    turns: list[TurnSpec] = []
    for item in raw or []:
        if isinstance(item, str):
            turns.append(TurnSpec(text=item))
            continue
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            raise ValueError(f"Scenario {path}: each turn is text, or a mapping with 'text'")
        images = tuple(str(name) for name in (item.get("images") or []))
        reply_images = tuple(str(name) for name in (item.get("reply_images") or []))
        reply_text = str(item.get("reply_text", ""))
        if reply_text and not reply_images:
            raise ValueError(
                f"Scenario {path}: 'reply_text' without 'reply_images' does not exercise the "
                "reply rail; use seeded_history for plain prior messages"
            )
        turns.append(
            TurnSpec(
                text=str(item["text"]),
                images=images,
                reply_images=reply_images,
                reply_author=str(item.get("reply_author", "Ana")),
                reply_text=reply_text,
            )
        )
    return turns


def _faults(path: str | Path, raw: list[Any] | None) -> list[Fault]:
    faults: list[Fault] = []
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("tool") or not item.get("message"):
            raise ValueError(f"Scenario {path}: each fault needs 'tool' and 'message'")
        faults.append(
            Fault(
                tool=str(item["tool"]),
                message=str(item["message"]),
                times=int(item.get("times", 1)),
            )
        )
    return faults


def _workspace_files(path: str | Path, raw: Any) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"Scenario {path}: workspace_files must be a path-to-content mapping")
    files: list[tuple[str, str]] = []
    for file_path, content in raw.items():
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValueError(f"Scenario {path}: workspace file paths must be non-empty strings")
        if not isinstance(content, str):
            raise ValueError(f"Scenario {path}: workspace file {file_path!r} content must be text")
        files.append((file_path, content))
    return tuple(files)


def load_scenario(path: str | Path) -> Scenario:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    for required in ("id", "trust_tier"):
        if not raw.get(required):
            raise ValueError(f"Scenario {path} must declare {required!r}")
    turns = _turns(path, raw.get("turns"))
    if not turns:
        raise ValueError(f"Scenario {path} must declare at least one turn")
    channel = raw.get("channel") or {}
    guild = raw.get("guild") or {}
    expect = raw.get("expect") or {}
    return Scenario(
        id=str(raw["id"]),
        category=str(raw.get("category", "uncategorized")),
        trust_tier=trust_tier_from_value(str(raw["trust_tier"]), label="scenario trust tier"),
        turns=turns,
        channel_name=str(channel.get("name", "")),
        channel_id=str(channel.get("id", "")),
        guild_name=str(guild.get("name", "")),
        guild_id=str(guild.get("id", "")),
        seeded_history=_seeded(raw.get("seeded_history")),
        activated_tools=[str(t) for t in (raw.get("activated_tools") or [])],
        expect=Expect(
            should_use_tools=[str(t) for t in (expect.get("should_use_tools") or [])],
            should_not_use_tools=[str(t) for t in (expect.get("should_not_use_tools") or [])],
            notes=str(expect.get("notes", "")),
            max_tool_calls=int(expect.get("max_tool_calls", 0)),
            reply_must_match=_reply_checks(path, expect.get("reply_must_match")),
            attaches_file=bool(expect.get("attaches_file", False)),
        ),
        faults=_faults(path, raw.get("faults")),
        requires_tools=[str(t) for t in (raw.get("requires_tools") or [])],
        workspace_files=_workspace_files(path, raw.get("workspace_files")),
    )


def scenario_blocked_tools(scenario: Scenario) -> frozenset[str]:
    """Return the platform-scope denylist used by an eval scenario turn."""
    return frozenset() if scenario.guild_id else frozenset({"move_to_thread"})


def unavailable_scenario_tools(
    scenario: Scenario,
    tool_names: Sequence[str],
    *,
    registry: ToolRegistry,
    identities: Sequence[EvalIdentity],
) -> list[str]:
    """Return tools unavailable to at least one planned caller of ``scenario``.

    Registration alone is insufficient: production visibility also applies the
    scenario's trust tier, synthetic caller identity, guild scope, operator
    denylist, and each tool's runtime availability predicate. ``has_tool`` is
    the registry's shared visibility boundary for those gates. Searchable-tool
    activation is deliberately excluded because a scenario can load an
    otherwise-visible tool with ``browse_tools`` during the turn.
    """
    if tool_names and not identities:
        raise ValueError(f"Scenario {scenario.id!r} has tools to check but no eval identities")
    guild_id = scenario.guild_id or None
    blocked = scenario_blocked_tools(scenario)
    return [
        name
        for name in tool_names
        if any(
            not registry.has_tool(
                name,
                user_id=identity.user_id,
                guild_id=guild_id,
                blocked=blocked,
                tier=scenario.trust_tier,
            )
            for identity in identities
        )
    ]


def split_gated_scenarios(
    scenarios: list[Scenario],
    *,
    registry: ToolRegistry,
    identities_by_scenario: Mapping[str, Sequence[EvalIdentity]],
) -> tuple[list[Scenario], list[tuple[Scenario, list[str]]]]:
    """Partition into (runnable here, held back with unavailable required tools).

    Mirrors split_image_scenarios: the gated scenarios live in the default tree so
    every runner loads them, and the runner decides. Returning the missing names
    lets a report say WHY one sat out rather than under-reporting coverage in
    silence.
    """
    runnable: list[Scenario] = []
    gated: list[tuple[Scenario, list[str]]] = []
    for scenario in scenarios:
        missing = unavailable_scenario_tools(
            scenario,
            scenario.requires_tools,
            registry=registry,
            identities=identities_by_scenario[scenario.id],
        )
        if missing:
            gated.append((scenario, missing))
        else:
            runnable.append(scenario)
    return runnable, gated


def split_image_scenarios(
    scenarios: list[Scenario],
) -> tuple[list[Scenario], list[Scenario]]:
    """Partition into (runnable anywhere, needs a vision-capable model).

    Image scenarios live in the default scenario tree, so every runner loads them
    by default and has to decide what to do on a text-only arm. Returning both
    halves lets a runner drop the visual ones and say so, rather than refusing a
    whole run or sending images to a model that will 400.
    """
    plain = [s for s in scenarios if not any(turn.has_images for turn in s.turns)]
    visual = [s for s in scenarios if any(turn.has_images for turn in s.turns)]
    return plain, visual


def load_scenarios(directory: str | Path) -> list[Scenario]:
    paths = sorted(Path(directory).rglob("*.yaml"))
    return [load_scenario(p) for p in paths]
