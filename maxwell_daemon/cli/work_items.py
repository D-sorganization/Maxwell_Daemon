"""`maxwell-daemon work-item ...` commands."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

work_item_app = typer.Typer(name="work-item", help="Manage governed work items.")
console = Console()


def _headers(token: str | None) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"} if token else {}


def _fail(message: str) -> None:
    console.print(f"[red]x[/red] {message}")
    raise typer.Exit(1)


def _base_url(daemon_url: str) -> str:
    return daemon_url.rstrip("/")


@work_item_app.command("create")
def create_work_item(
    title: Annotated[str, typer.Argument()],
    body: Annotated[str, typer.Option("--body")] = "",
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    criterion: Annotated[
        list[str] | None,
        typer.Option("--criterion", help="Acceptance criterion text. Repeatable."),
    ] = None,
    priority: Annotated[int, typer.Option("--priority")] = 100,
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Create a draft work item."""
    criteria = [
        {"id": f"AC{i + 1}", "text": text}
        for i, text in enumerate(criterion or [])
        if text.strip()
    ]
    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "repo": repo,
        "acceptance_criteria": criteria,
        "priority": priority,
    }
    try:
        response = httpx.post(
            f"{_base_url(daemon_url)}/api/v1/work-items",
            json=payload,
            headers=_headers(auth_token),
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"create failed: {exc}")
    item = response.json()
    console.print(f"[green]created[/green] {item['id']} {item['title']}")


@work_item_app.command("list")
def list_work_items(
    status: Annotated[str | None, typer.Option("--status")] = None,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 25,
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """List work items by priority."""
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if repo:
        params["repo"] = repo
    try:
        response = httpx.get(
            f"{_base_url(daemon_url)}/api/v1/work-items",
            params=params,
            headers=_headers(auth_token),
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"list failed: {exc}")
    items = response.json()
    if not items:
        console.print("[dim]No work items.[/dim]")
        return
    table = Table(header_style="bold cyan")
    table.add_column("ID", style="bold")
    table.add_column("Status")
    table.add_column("Priority", justify="right")
    table.add_column("Repo")
    table.add_column("Title")
    for item in items:
        table.add_row(
            item["id"],
            item["status"],
            str(item["priority"]),
            item.get("repo") or "-",
            item["title"],
        )
    console.print(table)


@work_item_app.command("show")
def show_work_item(
    item_id: Annotated[str, typer.Argument()],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Show one work item."""
    try:
        response = httpx.get(
            f"{_base_url(daemon_url)}/api/v1/work-items/{item_id}",
            headers=_headers(auth_token),
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"show failed: {exc}")
    item = response.json()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="right")
    table.add_column()
    for key, value in item.items():
        if value in (None, [], ""):
            continue
        table.add_row(f"{key}:", str(value))
    console.print(table)


def _transition(
    item_id: str,
    status: str,
    daemon_url: str,
    auth_token: str | None,
) -> None:
    try:
        response = httpx.post(
            f"{_base_url(daemon_url)}/api/v1/work-items/{item_id}/transition",
            json={"status": status},
            headers=_headers(auth_token),
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"transition failed: {exc}")
    console.print(f"[green]{status}[/green] {item_id}")


@work_item_app.command("refine")
def refine_work_item(
    item_id: Annotated[str, typer.Argument()],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Transition a work item to refined."""
    _transition(item_id, "refined", daemon_url, auth_token)


@work_item_app.command("block")
def block_work_item(
    item_id: Annotated[str, typer.Argument()],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Transition an in-progress work item to blocked."""
    _transition(item_id, "blocked", daemon_url, auth_token)


@work_item_app.command("cancel")
def cancel_work_item(
    item_id: Annotated[str, typer.Argument()],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Cancel a work item."""
    _transition(item_id, "cancelled", daemon_url, auth_token)
