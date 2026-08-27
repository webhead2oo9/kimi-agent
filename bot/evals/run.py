from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from app.providers import close_provider
from config.settings import settings
from evals.capture import InstrumentedProvider
from evals.harness import image_part, run_scenario_for_model
from evals.image_caption import caption_scenario_turns
from evals.identity import EvalIdentity, new_eval_run_nonce
from evals.judge import judge_pair, load_rubric
from evals.mechanical import compute_mechanical
from evals.models import ModelsConfig, build_eval_provider, load_models
from evals.registry import EvalRegistry, build_eval_registry
from evals.report import ScenarioReport, render_report, write_raw_jsonl
from evals.scenario import load_scenarios, split_image_scenarios
from evals.stub_gateway import StubGateway

log = logging.getLogger("evals")

EVALS_DIR = Path(__file__).resolve().parent


def plan_matrix(candidate: str, models: ModelsConfig, scenario_ids: list[str]) -> list[str]:
    run_count = len(scenario_ids) * 2
    lines = [
        f"Candidate: {candidate} ({models.candidates[candidate].model})",
        f"Baseline:  {models.baseline.label} ({models.baseline.model})",
        f"Judge:     {models.judge.label} ({models.judge.model})",
        f"Scenarios: {', '.join(scenario_ids)}",
        f"Total live run_conversation calls: {run_count} (scenarios x 2 models)",
    ]
    if models.image_captioner is not None:
        lines.insert(
            3,
            f"Image captioner: {models.image_captioner.label} ({models.image_captioner.model})",
        )
    return lines


async def _run(args: argparse.Namespace) -> int:
    models = load_models(args.models)
    caption_cache_dir = Path(getattr(args, "captions", EVALS_DIR / "captions"))
    scenarios = load_scenarios(args.scenarios)
    rubric = load_rubric(args.rubric)
    if args.candidate not in models.candidates:
        log.error("Unknown candidate %r; known: %s", args.candidate, list(models.candidates))
        return 2

    # A shared captioner gives both arms identical visual evidence. Without one,
    # preserve the direct-image path and require native vision on both arms.
    scenarios, visual = split_image_scenarios(scenarios)
    candidate_spec = models.candidates[args.candidate]
    if visual and (
        models.image_captioner is not None
        or (candidate_spec.supports_images() and models.baseline.supports_images())
    ):
        scenarios = [*scenarios, *visual]
    elif visual:
        blind = [
            spec.label for spec in (candidate_spec, models.baseline) if not spec.supports_images()
        ]
        log.warning(
            "Skipping %d image scenario(s). %s cannot see images (declare capabilities "
            "in evals/models.yaml): %s",
            len(visual),
            " and ".join(blind),
            ", ".join(s.id for s in visual),
        )
    if not scenarios:
        log.error("No runnable scenarios: every selected scenario needs a vision-capable pair.")
        return 2

    if args.dry_run:
        for line in plan_matrix(args.candidate, models, [s.id for s in scenarios]):
            print(line)
        return 0

    candidate = InstrumentedProvider(
        build_eval_provider(models.candidates[args.candidate]),
        min_request_interval_seconds=models.candidates[args.candidate].min_request_interval_seconds,
        request_timeout_seconds=models.candidates[args.candidate].timeout_seconds,
    )
    baseline = InstrumentedProvider(
        build_eval_provider(models.baseline),
        min_request_interval_seconds=models.baseline.min_request_interval_seconds,
        request_timeout_seconds=models.baseline.timeout_seconds,
    )
    judge = InstrumentedProvider(
        build_eval_provider(models.judge),
        min_request_interval_seconds=models.judge.min_request_interval_seconds,
        request_timeout_seconds=models.judge.timeout_seconds,
    )
    caption_provider = (
        InstrumentedProvider(
            build_eval_provider(models.image_captioner),
            min_request_interval_seconds=models.image_captioner.min_request_interval_seconds,
            request_timeout_seconds=models.image_captioner.timeout_seconds,
        )
        if models.image_captioner is not None and visual
        else None
    )

    gateway = StubGateway()
    eval_registry: EvalRegistry | None = None
    reports: list[ScenarioReport] = []
    eval_run_nonce = new_eval_run_nonce()
    try:
        eval_registry = await build_eval_registry(settings, gateway=gateway)
        # Production parity: every chat turn runs with the mandatory compactor
        # (same wiring as app/runtime.py).
        compactor = eval_registry.provider_manager.build_compactor()
        for scenario in scenarios:
            image_captions = (
                await caption_scenario_turns(
                    scenario,
                    caption_provider,
                    cache_dir=caption_cache_dir,
                    image_loader=image_part,
                )
                if caption_provider is not None and any(turn.has_images for turn in scenario.turns)
                else None
            )
            cand_run = await run_scenario_for_model(
                scenario,
                provider=candidate,
                registry=eval_registry.registry,
                gateway=gateway,
                memory_client=eval_registry.memory_manager.active_client(),
                preference_store=eval_registry.preference_store,
                bot_name=settings.bot_name,
                thread_handoff_suggest_after_tool_calls=(
                    settings.thread_handoff_suggest_after_tool_calls
                ),
                compactor=compactor,
                identity=EvalIdentity(
                    run_nonce=eval_run_nonce,
                    arm=f"candidate:{candidate_spec.label}",
                    scenario_id=scenario.id,
                    repetition=0,
                ),
                image_captions=image_captions,
            )
            base_run = await run_scenario_for_model(
                scenario,
                provider=baseline,
                registry=eval_registry.registry,
                gateway=gateway,
                memory_client=eval_registry.memory_manager.active_client(),
                preference_store=eval_registry.preference_store,
                bot_name=settings.bot_name,
                thread_handoff_suggest_after_tool_calls=(
                    settings.thread_handoff_suggest_after_tool_calls
                ),
                compactor=compactor,
                identity=EvalIdentity(
                    run_nonce=eval_run_nonce,
                    arm=f"baseline:{models.baseline.label}",
                    scenario_id=scenario.id,
                    repetition=0,
                ),
                image_captions=image_captions,
            )
            judge_result = await judge_pair(
                judge, scenario, candidate_run=cand_run, baseline_run=base_run, rubric=rubric
            )
            reports.append(
                ScenarioReport(
                    scenario_id=scenario.id,
                    category=scenario.category,
                    candidate_run=cand_run,
                    baseline_run=base_run,
                    candidate_mechanical=compute_mechanical(scenario, cand_run),
                    baseline_mechanical=compute_mechanical(scenario, base_run),
                    judge=judge_result,
                )
            )
            log.info("Scenario %s done (winner: %s)", scenario.id, judge_result.winner_label)
    finally:
        if eval_registry is not None:
            await eval_registry.close()
        await close_provider(candidate)
        await close_provider(baseline)
        await close_provider(judge)
        if caption_provider is not None:
            await close_provider(caption_provider)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_report(candidate.model, baseline.model, reports, rubric)
    if models.image_captioner is not None and visual:
        md = (
            f"> Visual evidence was captioned once by `{models.image_captioner.model}` "
            "and shared identically between both arms.\n\n"
            f"{md}"
        )
    (out_dir / "report.md").write_text(md)
    write_raw_jsonl(out_dir / "raw.jsonl", reports)
    candidate_tokens = sum(report.candidate_run.total_tokens for report in reports)
    baseline_tokens = sum(report.baseline_run.total_tokens for report in reports)
    print(f"Report written to {out_dir / 'report.md'}")
    print(f"Candidate tokens: {candidate_tokens} | Baseline tokens: {baseline_tokens}")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Qualify a candidate model against the baseline.")
    parser.add_argument("--candidate", required=True, help="candidate name in models.yaml")
    parser.add_argument("--models", default=str(EVALS_DIR / "models.yaml"))
    parser.add_argument("--rubric", default=str(EVALS_DIR / "rubric.yaml"))
    parser.add_argument("--scenarios", default=str(EVALS_DIR / "scenarios"))
    parser.add_argument("--captions", default=str(EVALS_DIR / "captions"))
    parser.add_argument("--out", default=str(EVALS_DIR / "runs" / "latest"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the run plan and exit without calling any provider.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
