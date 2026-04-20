"""``maxwell-daemon session ...`` — list and replay event-sourced session logs."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from maxwell_daemon.session import list_sessions, load_events, replay_transcript

DEFAULT_SESSION_DIR = Path.home() / ".local/share/maxwell-daemon/sessions"

session_app = typer.Typer(
    name="session",
    help="Event-sourced session logs: list, replay, inspect.",
)
console = Console()


@session_app.command("list")
def list_sessions_cmd(
    directory: Annotated[
        Path,
        typer.Argument(help="Directory of JSONL session logs"),
    ] = DEFAULT_SESSION_DIR,
) -> None:
    """List every session log under ``directory``."""
    ids = list_sessions(directory)
    if not ids:
        console.print(f"[dim]No sessions under {directory}[/dim]")
        return
    t = Table(title=f"Sessions — {directory}", header_style="bold cyan")
    t.add_column("Session")
    t.add_column("Events", justify="right")
    t.add_column("Path")
    for sid in ids:
        path = directory / f"{sid}.jsonl"
        count = sum(1 for _ in load_events(path))
        t.add_row(sid, str(count), str(path))
    console.print(t)


@session_app.command("replay")
def replay_session(
    session_id: Annotated[str, typer.Argument(help="Session ID (filename stem)")],
    directory: Annotated[
        Path,
        typer.Option("--directory", "-d", help="Where session logs live"),
    ] = DEFAULT_SESSION_DIR,
) -> None:
    """Render a session's transcript to stdout."""
    path = directory / f"{session_id}.jsonl"
    if not path.is_file():
        console.print(f"[red]✗[/red] Session {session_id!r} not found at {path}")
        raise typer.Exit(1)
    transcript = replay_transcript(path)
    console.print(transcript)
