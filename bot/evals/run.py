from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from app.providers import close_provider
from config.settings import settings
from evals.capture import InstrumentedProvider
from evals.harness import run_scenario_for_model
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
    return [
        f"Candidate: {candidate} ({models.candidates[candidate].model})",
        f"Baseline:  {models.baseline.label} ({models.baseline.model})",
        f"Judge:     {models.judge.label} ({models.judge.model})",
        f"Scenarios: {', '.join(scenario_ids)}",
        f"Total live run_conversation calls: {run_count} (scenarios x 2 models)",
    ]


async def _run(args: argparse.Namespace) -> int:
    models = load_models(args.models)
    scenarios = load_scenarios(args.scenarios)
    rubric = load_rubric(args.rubric)
    if args.candidate not in models.candidates:
        log.error("Unknown candidate %r; known: %s", args.candidate, list(models.candidates))
        return 2

    # Both arms answer the same scenarios and are judged head to head, so an
    # image scenario is only fair when *both* can see the image. One arm reading
    # the picture while the other reads the caption is not a model comparison.
    scenarios, visual = split_image_scenarios(scenarios)
    candidate_spec = models.candidates[args.candidate]
    if visual and candidate_spec.supports_images() and models.baseline.supports_images():
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

    candidate = InstrumentedProvider(build_eval_provider(models.candidates[args.candidate]))
    baseline = InstrumentedProvider(build_eval_provider(models.baseline))
    judge = build_eval_provider(models.judge)

    gateway = StubGateway()
    eval_registry: EvalRegistry | None = None
    reports: list[ScenarioReport] = []
    try:
        eval_registry = await build_eval_registry(settings, gateway=gateway)
        # Production parity: every chat turn runs with the mandatory compactor
        # (same wiring as app/runtime.py).
        compactor = eval_registry.provider_manager.build_compactor()
        for scenario in scenarios:
            cand_run = await run_scenario_for_model(
                scenario,
                provider=candidate,
                registry=eval_registry.registry,
                gateway=gateway,
                memory_client=eval_registry.memory_manager.active_client(),
                preference_store=eval_registry.preference_store,
                bot_name=settings.bot_name,
                compactor=compactor,
            )
            base_run = await run_scenario_for_model(
                scenario,
                provider=baseline,
                registry=eval_registry.registry,
                gateway=gateway,
                memory_client=eval_registry.memory_manager.active_client(),
                preference_store=eval_registry.preference_store,
                bot_name=settings.bot_name,
                compactor=compactor,
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

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_report(candidate.model, baseline.model, reports, rubric)
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
