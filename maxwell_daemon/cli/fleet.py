"""Fleet capability registry commands."""

from __future__ import annotations

import json
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

fleet_app = typer.Typer(help="Fleet capability registry commands.")
console = Console()


def _fetch_status(
    repo: Annotated[str, typer.Option("--repo", help="Repo to evaluate")],
    tool: Annotated[str, typer.Option("--tool", help="Tool to evaluate")],
    base_url: Annotated[
        str,
        typer.Option("--base-url", help="Daemon API base URL"),
    ] = "http://127.0.0.1:8080",
    required_capability: Annotated[
        list[str] | None,
        typer.Option(
            "--required-capability",
            "-r",
            help="Repeat to add one required capability at a time.",
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option("--token", help="Bearer token for the API, if enabled"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON instead of a table"),
    ] = False,
) -> None:
    """Fetch a redacted capability snapshot from the daemon."""

    params: list[tuple[str, str | int | float | bool | None]] = [("repo", repo), ("tool", tool)]
    params.extend(("required_capability", capability) for capability in required_capability or ())
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{base_url.rstrip('/')}/api/v1/fleet/capabilities"

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"[red]x[/red] fleet status request failed: {exc}")
        raise typer.Exit(1) from None

    payload: dict[str, Any] = response.json()
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    _render_status(payload)


@fleet_app.command()
def status(
    repo: Annotated[str, typer.Option("--repo", help="Repo to evaluate")],
    tool: Annotated[str, typer.Option("--tool", help="Tool to evaluate")],
    base_url: Annotated[
        str,
        typer.Option("--base-url", help="Daemon API base URL"),
    ] = "http://127.0.0.1:8080",
    required_capability: Annotated[
        list[str] | None,
        typer.Option(
            "--required-capability",
            "-r",
            help="Repeat to add one required capability at a time.",
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option("--token", help="Bearer token for the API, if enabled"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON instead of a table"),
    ] = False,
) -> None:
    """Fetch a redacted capability snapshot from the daemon."""

    _fetch_status(
        repo=repo,
        tool=tool,
        base_url=base_url,
        required_capability=required_capability,
        token=token,
        json_output=json_output,
    )


@fleet_app.command(name="nodes")
def nodes(
    repo: Annotated[str, typer.Option("--repo", help="Repo to evaluate")],
    tool: Annotated[str, typer.Option("--tool", help="Tool to evaluate")],
    base_url: Annotated[
        str,
        typer.Option("--base-url", help="Daemon API base URL"),
    ] = "http://127.0.0.1:8080",
    required_capability: Annotated[
        list[str] | None,
        typer.Option(
            "--required-capability",
            "-r",
            help="Repeat to add one required capability at a time.",
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option("--token", help="Bearer token for the API, if enabled"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON instead of a table"),
    ] = False,
) -> None:
    """Alias for `status` that matches the node-centric fleet vocabulary."""

    _fetch_status(
        repo=repo,
        tool=tool,
        base_url=base_url,
        required_capability=required_capability,
        token=token,
        json_output=json_output,
    )


def _render_status(payload: dict[str, Any]) -> None:
    console.print(
        f"[bold]Repo:[/bold] {payload.get('repo', '-')}\n"
        f"[bold]Tool:[/bold] {payload.get('tool', '-')}\n"
        f"[bold]Required:[/bold] {', '.join(payload.get('required_capabilities', [])) or '-'}\n"
        f"[bold]Selected:[/bold] "
        f"{payload.get('selected_node', {}).get('hostname', '-') if payload.get('selected_node') else '-'}\n"
        f"[bold]Decision:[/bold] {payload.get('explanation', '-')}"
    )

    table = Table(title="Fleet Nodes", header_style="bold cyan")
    table.add_column("Node")
    table.add_column("Eligible")
    table.add_column("Score", justify="right")
    table.add_column("Sessions", justify="right")
    table.add_column("Tailscale")
    table.add_column("Reasons")

    selected_id = None
    if payload.get("selected_node"):
        selected_id = payload["selected_node"].get("node_id")

    for node in payload.get("nodes", []):
        marker = "* " if node.get("node_id") == selected_id else ""
        tailscale_status = node.get("tailscale_status") or {}
        tailscale = "online" if tailscale_status.get("online") else "offline"
        table.add_row(
            f"{marker}{node.get('hostname', node.get('node_id', '-'))}",
            "yes" if node.get("eligible") else "no",
            "-" if node.get("score") is None else str(node["score"]),
            str(node.get("active_sessions", 0)),
            tailscale,
            "; ".join(node.get("reasons", [])) or "-",
        )

    console.print(table)
