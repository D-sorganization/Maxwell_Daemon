"""`maxwell-daemon action ...` commands for the action ledger."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

action_app = typer.Typer(name="action", help="Inspect and decide proposed agent actions.")
console = Console()


def _headers(token: str | None) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"} if token else {}


def _fail(message: str) -> None:
    console.print(f"[red]x[/red] {message}")
    raise typer.Exit(1)


def _request_json(
    method: str,
    url: str,
    *,
    auth_token: str | None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    try:
        if method == "GET":
            response = httpx.get(url, headers=_headers(auth_token), timeout=10.0)
        else:
            response = httpx.post(
                url,
                json=json_body,
                headers=_headers(auth_token),
                timeout=10.0,
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"request failed: {exc}")
    return response.json()


def render_actions(actions: list[dict[str, Any]]) -> None:
    if not actions:
        console.print("[dim]No actions.[/dim]")
        return
    table = Table(header_style="bold cyan")
    table.add_column("ID", style="bold")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Approval")
    table.add_column("Summary")
    for action in actions:
        approval = "required (proposal only)" if action.get("requires_approval") else "auto"
        table.add_row(
            action["id"],
            action["kind"],
            action["status"],
            approval,
            action["summary"],
        )
    console.print(table)


@action_app.command("show")
def show_action(
    action_id: Annotated[str, typer.Argument()],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Print every field of one action."""
    action = _request_json(
        "GET",
        f"{daemon_url}/api/v1/actions/{action_id}",
        auth_token=auth_token,
    )
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="right")
    table.add_column()
    for key, value in action.items():
        if value is None:
            continue
        table.add_row(f"{key}:", str(value))
    console.print(table)


@action_app.command("approve")
def approve_action(
    action_id: Annotated[str, typer.Argument()],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Approve a proposed action without executing the side effect."""
    action = _request_json(
        "POST",
        f"{daemon_url}/api/v1/actions/{action_id}/approve",
        auth_token=auth_token,
    )
    contract = action.get("approval_contract", "proposal_only")
    console.print(f"[green]approved proposal[/green] {action['id']} ({contract})")


@action_app.command("reject")
def reject_action(
    action_id: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason", "-r")] = None,
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Reject a proposed action."""
    action = _request_json(
        "POST",
        f"{daemon_url}/api/v1/actions/{action_id}/reject",
        auth_token=auth_token,
        json_body={"reason": reason},
    )
    console.print(f"[yellow]rejected[/yellow] {action['id']}")
