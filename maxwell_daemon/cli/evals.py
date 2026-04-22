"""`maxwell-daemon eval ...` commands for deterministic workflow evaluations."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from maxwell_daemon.contracts import ContractViolation
from maxwell_daemon.evals.registry import list_scenarios
from maxwell_daemon.evals.reports import compare_runs, render_markdown_report
from maxwell_daemon.evals.runner import EvalRunner
from maxwell_daemon.evals.storage import EvalRunStore

eval_app = typer.Typer(name="eval", help="Run deterministic Maxwell evaluation scenarios.")
console = Console()
DEFAULT_OUTPUT_ROOT = Path(".maxwell") / "evals"


@eval_app.command("list")
def list_eval_scenarios() -> None:
    """List built-in CI-safe eval scenarios."""

    table = Table(title="Eval Scenarios", header_style="bold cyan")
    table.add_column("ID", style="bold")
    table.add_column("Source")
    table.add_column("Risk")
    table.add_column("Title")
    for scenario in list_scenarios():
        table.add_row(
            scenario.id,
            scenario.source_type.value,
            scenario.risk_level.value,
            scenario.title,
        )
    console.print(table)


@eval_app.command("run")
def run_eval_scenarios(
    scenario: Annotated[
        list[str] | None,
        typer.Option("--scenario", "-s", help="Scenario id. Repeat to run a subset."),
    ] = None,
    output: Annotated[Path, typer.Option("--output", "-o")] = DEFAULT_OUTPUT_ROOT,
    approve: Annotated[
        list[str] | None,
        typer.Option("--approve", help="Scenario id with explicit approval granted."),
    ] = None,
    preserve_workspaces: Annotated[bool, typer.Option("--preserve-workspaces")] = False,
) -> None:
    """Run the smoke suite or a selected scenario subset."""

    runner = EvalRunner(output)
    try:
        run, results = runner.run(
            scenario_ids=scenario,
            approvals=set(approve or []),
            preserve_workspaces=preserve_workspaces,
        )
    except (ContractViolation, KeyError, ValueError) as exc:
        console.print(f"[red]ERROR[/red] {exc}")
        raise typer.Exit(1) from None

    run_dir = EvalRunStore(output).save(run, results)
    console.print(f"[green]OK[/green] Eval run {run.id}: {run.summary}")
    console.print(f"[dim]Saved to {run_dir}[/dim]")
    if run.status.value != "passed":
        raise typer.Exit(1)


@eval_app.command("show")
def show_eval_run(
    run_id: Annotated[str, typer.Argument()],
    output: Annotated[Path, typer.Option("--output", "-o")] = DEFAULT_OUTPUT_ROOT,
) -> None:
    """Show one stored eval run."""

    try:
        store = EvalRunStore(output)
        run = store.load_run(run_id)
        results = store.load_results(run_id)
    except ContractViolation as exc:
        console.print(f"[red]ERROR[/red] {exc}")
        raise typer.Exit(1) from None

    table = Table(title=f"Eval Run {run.id}", header_style="bold cyan")
    table.add_column("Scenario", style="bold")
    table.add_column("Status")
    table.add_column("Score", justify="right")
    table.add_column("Failure")
    for result in results:
        table.add_row(
            result.scenario_id,
            result.status.value,
            f"{result.score_total:.2f}",
            result.failure_category.value,
        )
    console.print(table)


@eval_app.command("report")
def report_eval_run(
    run_id: Annotated[str, typer.Argument()],
    output: Annotated[Path, typer.Option("--output", "-o")] = DEFAULT_OUTPUT_ROOT,
) -> None:
    """Print a markdown report for one stored eval run."""

    try:
        store = EvalRunStore(output)
        run = store.load_run(run_id)
        results = store.load_results(run_id)
    except ContractViolation as exc:
        console.print(f"[red]ERROR[/red] {exc}")
        raise typer.Exit(1) from None
    console.print(render_markdown_report(run, results))


@eval_app.command("compare")
def compare_eval_runs(
    base_run_id: Annotated[str, typer.Argument()],
    candidate_run_id: Annotated[str, typer.Argument()],
    output: Annotated[Path, typer.Option("--output", "-o")] = DEFAULT_OUTPUT_ROOT,
) -> None:
    """Compare two stored eval runs and highlight regressions."""

    try:
        store = EvalRunStore(output)
        base_run = store.load_run(base_run_id)
        candidate_run = store.load_run(candidate_run_id)
        comparison = compare_runs(
            base_run,
            store.load_results(base_run_id),
            candidate_run,
            store.load_results(candidate_run_id),
        )
    except ContractViolation as exc:
        console.print(f"[red]ERROR[/red] {exc}")
        raise typer.Exit(1) from None

    table = Table(title="Eval Comparison", header_style="bold cyan")
    table.add_column("Scenario", style="bold")
    table.add_column("Base", justify="right")
    table.add_column("Candidate", justify="right")
    table.add_column("Delta", justify="right")
    table.add_column("Class")
    for item in comparison.items:
        table.add_row(
            item.scenario_id,
            f"{item.base_score:.2f}",
            f"{item.candidate_score:.2f}",
            f"{item.delta:+.2f}",
            item.classification,
        )
    console.print(table)
    if comparison.regressions:
        raise typer.Exit(1)
