"""`maxwell-daemon checks ...` commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from maxwell_daemon.checks import CheckLoadError, LocalCheckRunner

checks_app = typer.Typer(name="checks", help="Run source-controlled Maxwell checks.")
console = Console()


@checks_app.command("list")
def list_checks(
    repo: Annotated[
        Path | None, typer.Option("--repo", help="Repository root.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List `.maxwell/checks/*.md` definitions."""
    repo_path = repo or Path.cwd()
    try:
        definitions = LocalCheckRunner(repo_path).list()
    except CheckLoadError as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(1) from exc

    if json_output:
        typer.echo(
            json.dumps([item.model_dump(mode="json") for item in definitions], indent=2)
        )
        return
    if not definitions:
        console.print("[dim]No Maxwell checks found.[/dim]")
        return
    table = Table(title="Maxwell Checks", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Severity")
    table.add_column("Globs")
    table.add_column("Name")
    for definition in definitions:
        table.add_row(
            definition.id,
            definition.severity.value,
            ", ".join(definition.applies_to.globs) or "*",
            definition.name,
        )
    console.print(table)


@checks_app.command("run")
def run_checks(
    repo: Annotated[
        Path | None, typer.Option("--repo", help="Repository root.")
    ] = None,
    event: Annotated[
        str,
        typer.Option("--event", help="Event name used to select triggered checks."),
    ] = "pull_request",
    changed_file: Annotated[
        list[str] | None,
        typer.Option("--changed-file", help="Changed file path. Repeatable."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Run local source-controlled checks against changed files."""
    repo_path = repo or Path.cwd()
    changed_files = tuple(changed_file or ())
    try:
        results = LocalCheckRunner(repo_path).run(
            changed_files=changed_files, event=event
        )
    except CheckLoadError as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(1) from exc

    if json_output:
        typer.echo(
            json.dumps([item.model_dump(mode="json") for item in results], indent=2)
        )
        return
    if not results:
        console.print("[dim]No Maxwell checks found.[/dim]")
        return
    table = Table(title="Maxwell Check Results", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Conclusion")
    table.add_column("Severity")
    table.add_column("Summary")
    for result in results:
        table.add_row(
            result.check_id,
            result.conclusion.value,
            result.severity.value,
            result.summary,
        )
    console.print(table)
