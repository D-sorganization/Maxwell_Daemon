"""`maxwell-daemon tasks ...` subcommands — list, show, cancel.

LoD: the CLI never reaches into the daemon's in-memory state. It speaks only
HTTP, so it works against a local daemon (``maxwell-daemon serve``) or a remote one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

from maxwell_daemon.core.presets import FilterPreset, PresetStore

tasks_app = typer.Typer(name="tasks", help="List, show, cancel tasks on a running daemon.")
preset_app = typer.Typer(name="preset", help="Named filter presets for `tasks list`.")
tasks_app.add_typer(preset_app, name="preset")
console = Console()


def _presets_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "maxwell-daemon" / "presets.json"


def _preset_store() -> PresetStore:
    return PresetStore(_presets_path())


def _headers(token: str | None) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"} if token else {}


def _fail(message: str) -> None:
    console.print(f"[red]✗[/red] {message}")
    raise typer.Exit(1)


@tasks_app.command("list")
def list_tasks(
    status: Annotated[
        str | None,
        typer.Option("--status", help="queued | running | completed | failed | cancelled"),
    ] = None,
    kind: Annotated[str | None, typer.Option("--kind", help="prompt | issue")] = None,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 25,
    preset: Annotated[
        str | None,
        typer.Option("--preset", help="Name of a saved preset; CLI flags override fields it sets"),
    ] = None,
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Show tasks, newest first. Combine filters with a saved preset."""
    # Apply the preset first; any explicit CLI flag takes precedence.
    if preset:
        p = _preset_store().get(preset)
        if p is None:
            _fail(f"preset {preset!r} not found — try `maxwell-daemon tasks preset list`")
        assert p is not None
        status = status or p.status
        kind = kind or p.kind
        repo = repo or p.repo
        if p.limit is not None and limit == 25:
            limit = p.limit

    params: list[str] = []
    if status:
        params.append(f"status={status}")
    if kind:
        params.append(f"kind={kind}")
    if repo:
        params.append(f"repo={repo}")
    params.append(f"limit={limit}")
    url = f"{daemon_url}/api/v1/tasks?{'&'.join(params)}"

    try:
        r = httpx.get(url, headers=_headers(auth_token), timeout=10.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        _fail(f"request failed: {e}")

    tasks = r.json()
    if not tasks:
        console.print("[dim]No tasks.[/dim]")
        return

    t = Table(header_style="bold cyan")
    t.add_column("ID", style="bold")
    t.add_column("Kind")
    t.add_column("Status")
    t.add_column("Target")
    t.add_column("Cost (USD)", justify="right")
    t.add_column("Created")
    for task in tasks:
        target = (
            f"{task['issue_repo']}#{task['issue_number']}"
            if task.get("issue_repo")
            else task.get("prompt", "")[:40]
        )
        t.add_row(
            task["id"],
            task["kind"],
            _colourise(task["status"]),
            target,
            f"${task.get('cost_usd', 0):.4f}",
            task["created_at"][:19],
        )
    console.print(t)


@tasks_app.command("show")
def show_task(
    task_id: Annotated[str, typer.Argument()],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Print every field of a task."""
    try:
        r = httpx.get(
            f"{daemon_url}/api/v1/tasks/{task_id}",
            headers=_headers(auth_token),
            timeout=10.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        _fail(f"request failed: {e}")

    task = r.json()
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold cyan", justify="right")
    t.add_column()
    for key, value in task.items():
        if value is None:
            continue
        if isinstance(value, str) and len(value) > 200:
            value = value[:200] + "…"
        t.add_row(f"{key}:", str(value))
    console.print(t)


@tasks_app.command("cancel")
def cancel_task(
    task_id: Annotated[str, typer.Argument()],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Cancel a queued task. Running / completed / failed tasks are unchanged."""
    try:
        r = httpx.post(
            f"{daemon_url}/api/v1/tasks/{task_id}/cancel",
            headers=_headers(auth_token),
            timeout=10.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        _fail(f"cancel failed: {e}")

    console.print(f"[green]✓[/green] Task {task_id} cancelled")


def _colourise(status: str) -> str:
    return {
        "queued": "[yellow]queued[/yellow]",
        "running": "[cyan]running[/cyan]",
        "completed": "[green]completed[/green]",
        "failed": "[red]failed[/red]",
        "cancelled": "[dim]cancelled[/dim]",
    }.get(status, status)


@preset_app.command("save")
def preset_save(
    name: Annotated[str, typer.Argument()],
    status: Annotated[str | None, typer.Option("--status")] = None,
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
) -> None:
    """Save a named filter preset for reuse."""
    try:
        _preset_store().save(
            FilterPreset(name=name, status=status, kind=kind, repo=repo, limit=limit)
        )
    except ValueError as e:
        _fail(str(e))
    console.print(f"[green]✓[/green] Saved preset [bold]{name}[/bold]")


@preset_app.command("list")
def preset_list() -> None:
    """Show every saved preset."""
    presets = _preset_store().list()
    if not presets:
        console.print("[dim]No presets saved. Try `maxwell-daemon tasks preset save ...`[/dim]")
        return
    t = Table(header_style="bold cyan")
    t.add_column("Name", style="bold")
    t.add_column("Status")
    t.add_column("Kind")
    t.add_column("Repo")
    t.add_column("Limit")
    for p in presets:
        t.add_row(
            p.name,
            p.status or "-",
            p.kind or "-",
            p.repo or "-",
            str(p.limit) if p.limit is not None else "-",
        )
    console.print(t)


@preset_app.command("delete")
def preset_delete(name: Annotated[str, typer.Argument()]) -> None:
    """Remove a saved preset."""
    if _preset_store().delete(name):
        console.print(f"[green]✓[/green] Deleted preset {name}")
    else:
        _fail(f"preset {name!r} not found")
