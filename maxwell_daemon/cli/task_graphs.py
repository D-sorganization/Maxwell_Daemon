"""`maxwell-daemon task-graph ...` subcommand group."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, cast

import typer
from rich.console import Console
from rich.table import Table

from maxwell_daemon.core.work_items import AcceptanceCriterion, ScopeBoundary, WorkItem
from maxwell_daemon.director.task_graphs import (
    TaskGraph,
    TaskGraphTemplate,
    build_task_graph,
)

task_graph_app = typer.Typer(
    help="Create and inspect typed sub-agent task graph definitions.",
    no_args_is_help=True,
)
console = Console()

RiskLevel = Literal["low", "medium", "high", "critical"]
_RISK_LEVELS: frozenset[str] = frozenset(("low", "medium", "high", "critical"))


@task_graph_app.command("create")
def create_graph(
    work_item_id: Annotated[str, typer.Argument(help="Work item id for the graph")],
    title: Annotated[
        str, typer.Option("--title", help="Work item title")
    ] = "Untitled work item",
    criterion: Annotated[
        list[str] | None,
        typer.Option("--criterion", "-a", help="Acceptance criterion. Repeatable."),
    ] = None,
    risk: Annotated[
        str,
        typer.Option("--risk", help="Risk level: low, medium, high, or critical."),
    ] = "medium",
    label: Annotated[
        list[str] | None,
        typer.Option("--label", "-l", help="Work item label. Repeatable."),
    ] = None,
    template: Annotated[
        TaskGraphTemplate | None,
        typer.Option("--template", help="Override automatic template selection."),
    ] = None,
    graph_id: Annotated[
        str | None, typer.Option("--graph-id", help="Stable graph id")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write JSON to file")
    ] = None,
) -> None:
    """Create a validated task graph definition as JSON."""

    if risk not in _RISK_LEVELS:
        console.print("[red]risk must be one of: low, medium, high, critical[/red]")
        raise typer.Exit(2)

    work_item = WorkItem(
        id=work_item_id,
        title=title,
        acceptance_criteria=tuple(
            AcceptanceCriterion(id=f"AC{index}", text=text)
            for index, text in enumerate(criterion or (), start=1)
        ),
        scope=ScopeBoundary(risk_level=cast(RiskLevel, risk)),
    )
    graph = build_task_graph(
        work_item,
        template=template,
        graph_id=graph_id,
        labels=tuple(label or ()),
    )
    body = graph.model_dump_json(indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body, encoding="utf-8")
        console.print(f"[green]Wrote task graph:[/green] {output}")
        return
    typer.echo(body, nl=False)


@task_graph_app.command("inspect")
def inspect_graph(
    path: Annotated[Path, typer.Argument(help="Task graph JSON file")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Print normalized JSON")
    ] = False,
) -> None:
    """Validate and inspect a saved task graph definition."""

    graph = TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))
    if json_output:
        typer.echo(graph.model_dump_json(indent=2))
        return

    table = Table(title=f"Task graph {graph.id}", header_style="bold cyan")
    table.add_column("Node")
    table.add_column("Role")
    table.add_column("Depends on")
    table.add_column("Requires")
    table.add_column("Output")
    table.add_column("Retries", justify="right")
    for node in graph.nodes_in_dependency_order():
        table.add_row(
            node.id,
            node.role.value,
            ", ".join(node.depends_on) or "-",
            ", ".join(kind.value for kind in node.required_artifacts) or "-",
            node.output_artifact_kind.value,
            str(node.max_retries),
        )
    console.print(table)
