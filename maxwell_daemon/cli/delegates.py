"""`maxwell-daemon delegate ...` commands."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from maxwell_daemon.core.delegate_lifecycle import DelegateSessionSnapshot

delegate_app = typer.Typer(name="delegate", help="Inspect durable delegate sessions.")
console = Console()


def _headers(token: str | None) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"} if token else {}


def _fail(message: str) -> None:
    console.print(f"[red]✗[/red] {message}")
    raise typer.Exit(1)


def _base_url(daemon_url: str) -> str:
    return daemon_url.rstrip("/")


@delegate_app.command("list")
def list_delegate_sessions(
    work_item_id: Annotated[str | None, typer.Option("--work-item-id")] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    delegate_id: Annotated[str | None, typer.Option("--delegate-id")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 25,
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Show durable delegate sessions ordered by most recent update."""

    params: dict[str, Any] = {"limit": limit}
    if work_item_id:
        params["work_item_id"] = work_item_id
    if task_id:
        params["task_id"] = task_id
    if status:
        params["status"] = status
    if delegate_id:
        params["delegate_id"] = delegate_id
    try:
        response = httpx.get(
            f"{_base_url(daemon_url)}/api/v1/delegate-sessions",
            params=params,
            headers=_headers(auth_token),
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"list failed: {exc}")

    sessions = [
        DelegateSessionSnapshot.model_validate(item) for item in response.json()
    ]
    if not sessions:
        console.print("[dim]No delegate sessions.[/dim]")
        return

    table = Table(header_style="bold cyan")
    table.add_column("ID", style="bold")
    table.add_column("Status")
    table.add_column("Delegate")
    table.add_column("Machine")
    table.add_column("Backend")
    table.add_column("Latest checkpoint")
    table.add_column("Lease expires")
    for record in sessions:
        session = record.session
        checkpoint = record.latest_checkpoint
        lease = record.active_lease
        table.add_row(
            session.id,
            session.status.value,
            session.delegate_id,
            session.machine_ref,
            session.backend_ref,
            checkpoint.id if checkpoint else "-",
            lease.expires_at.isoformat() if lease else "-",
        )
    console.print(table)


@delegate_app.command("show")
def show_delegate_session(
    session_id: Annotated[str, typer.Argument()],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Show one durable delegate session and its latest checkpoint."""

    try:
        response = httpx.get(
            f"{_base_url(daemon_url)}/api/v1/delegate-sessions/{session_id}",
            headers=_headers(auth_token),
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"show failed: {exc}")

    record = DelegateSessionSnapshot.model_validate(response.json())
    session = record.session
    lease = record.active_lease
    checkpoint = record.latest_checkpoint

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="right")
    table.add_column()
    table.add_row("id:", session.id)
    table.add_row("delegate_id:", session.delegate_id)
    table.add_row("status:", session.status.value)
    table.add_row("work_item_id:", session.work_item_id or "-")
    table.add_row("task_id:", session.task_id or "-")
    table.add_row("workspace_ref:", session.workspace_ref)
    table.add_row("backend_ref:", session.backend_ref)
    table.add_row("machine_ref:", session.machine_ref)
    table.add_row("created_at:", session.created_at.isoformat())
    table.add_row("updated_at:", session.updated_at.isoformat())
    table.add_row("active_lease:", lease.id if lease else "-")
    table.add_row("lease_owner:", lease.owner_id if lease else "-")
    table.add_row("lease_expires:", lease.expires_at.isoformat() if lease else "-")
    table.add_row("checkpoint:", checkpoint.id if checkpoint else "-")
    table.add_row("checkpoint_plan:", checkpoint.current_plan if checkpoint else "-")
    table.add_row(
        "checkpoint_prompt:",
        checkpoint.resume_prompt if checkpoint and checkpoint.resume_prompt else "-",
    )
    console.print(table)
