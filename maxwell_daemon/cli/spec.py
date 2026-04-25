"""`maxwell-daemon spec ...` subcommands — load + scaffold `.feature` files.

The CLI owns the I/O edge: parse arguments, read/write files, print Rich
output. The parsing + scaffold logic lives in :mod:`maxwell_daemon.spec`
and is covered by its own unit tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from maxwell_daemon.spec import (
    GherkinParseError,
    load_feature,
    load_spec_directory,
    render_pytest_bdd_scaffold,
)

spec_app = typer.Typer(name="spec", help="Load + scaffold Gherkin `.feature` files.")
console = Console()


@spec_app.command("list")
def list_specs(
    directory: Annotated[
        Path,
        typer.Argument(help="Path to a directory of .feature files"),
    ] = Path(".maxwell/specs"),
) -> None:
    """Summarise every spec in ``directory`` as a table."""
    specs = load_spec_directory(directory)
    if not specs:
        console.print(f"[dim]No .feature files under {directory}[/dim]")
        return
    t = Table(title=f"{directory} — {len(specs)} spec(s)", header_style="bold cyan")
    t.add_column("Feature")
    t.add_column("Scenarios", justify="right")
    t.add_column("Tags")
    t.add_column("Source")
    for s in specs:
        t.add_row(s.feature, str(len(s.scenarios)), " ".join(s.tags), str(s.source))
    console.print(t)


@spec_app.command("show")
def show_spec(
    feature: Annotated[Path, typer.Argument(help=".feature file to inspect")],
) -> None:
    """Parse one spec and print its scenarios + steps."""
    try:
        spec = load_feature(feature)
    except (
        GherkinParseError,
        FileNotFoundError,
        IsADirectoryError,
        PermissionError,
    ) as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1) from None
    console.print(f"[bold]Feature:[/bold] {spec.feature}")
    if spec.tags:
        console.print(f"[dim]Tags: {' '.join(spec.tags)}[/dim]")
    if spec.description:
        console.print(f"\n{spec.description}\n")
    for scenario in spec.scenarios:
        console.print(f"[bold cyan]Scenario:[/bold cyan] {scenario.name}")
        if scenario.tags:
            console.print(f"  [dim]Tags: {' '.join(scenario.tags)}[/dim]")
        for step in scenario.steps:
            console.print(f"  [green]{step.keyword}[/green] {step.text}")
        console.print()


@spec_app.command("generate")
def generate_scaffold(
    feature: Annotated[Path, typer.Argument(help=".feature file to scaffold")],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output path (default: tests/bdd/test_<feature>.py)",
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Replace an existing scaffold file"),
    ] = False,
) -> None:
    """Render a pytest-bdd scaffold for ``feature`` and write it to disk."""
    try:
        spec = load_feature(feature)
    except (
        GherkinParseError,
        FileNotFoundError,
        IsADirectoryError,
        PermissionError,
    ) as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1) from None

    target = output or _default_scaffold_path(feature)
    if target.exists() and not overwrite:
        console.print(
            f"[red]✗[/red] {target} already exists — pass --overwrite to replace it."
        )
        raise typer.Exit(1)

    scaffold = render_pytest_bdd_scaffold(spec)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(scaffold)
    console.print(
        f"[green]✓[/green] Wrote {len(scaffold)} bytes to [bold]{target}[/bold] "
        f"(scenarios: {len(spec.scenarios)})"
    )


def _default_scaffold_path(feature: Path) -> Path:
    """Mirror of the convention pytest-bdd users expect:

    ``specs/login.feature`` → ``tests/bdd/test_login.py``.
    """
    return Path("tests/bdd") / f"test_{feature.stem}.py"
