"""Markdown reporting and run comparison helpers for evals."""

from __future__ import annotations

from maxwell_daemon.evals.models import (
    EvalComparison,
    EvalComparisonItem,
    EvalResult,
    EvalRun,
)


def render_markdown_report(run: EvalRun, results: list[EvalResult]) -> str:
    lines = [
        f"# Eval Report: {run.id}",
        "",
        f"Status: {run.status.value}",
        f"Summary: {run.summary}",
        "",
        "| Scenario | Status | Score | Breakdown | Failure Category | Trace | Artifacts |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    result.scenario_id,
                    result.status.value,
                    f"{result.score_total:.2f}",
                    _format_breakdown(result),
                    result.failure_category.value,
                    result.trace_id or "-",
                    ", ".join(result.artifact_refs) or "-",
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _format_breakdown(result: EvalResult) -> str:
    if not result.score_breakdown:
        return "-"
    return ", ".join(
        f"{name}={score:.1f}" for name, score in sorted(result.score_breakdown.items())
    )


def compare_runs(
    base_run: EvalRun,
    base_results: list[EvalResult],
    candidate_run: EvalRun,
    candidate_results: list[EvalResult],
) -> EvalComparison:
    base_by_id = {result.scenario_id: result for result in base_results}
    candidate_by_id = {result.scenario_id: result for result in candidate_results}
    items: list[EvalComparisonItem] = []
    for scenario_id in sorted(set(base_by_id) & set(candidate_by_id)):
        base_score = base_by_id[scenario_id].score_total
        candidate_score = candidate_by_id[scenario_id].score_total
        delta = round(candidate_score - base_score, 2)
        if delta < 0:
            classification = "regression"
        elif delta > 0:
            classification = "improvement"
        else:
            classification = "unchanged"
        items.append(
            EvalComparisonItem(
                scenario_id=scenario_id,
                base_score=base_score,
                candidate_score=candidate_score,
                delta=delta,
                classification=classification,
            )
        )
    return EvalComparison(
        base_run_id=base_run.id,
        candidate_run_id=candidate_run.id,
        items=items,
    )
