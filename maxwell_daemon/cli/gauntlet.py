"""`maxwell-daemon gauntlet ...` and `maxwell-daemon gate ...` commands."""

from __future__ import annotations

from typing import Annotated, Any, cast

import httpx
import typer
from rich.console import Console
from rich.table import Table

gauntlet_app = typer.Typer(
    name="gauntlet",
    help="Inspect and act on task-scoped control-plane gauntlets.",
)
console = Console()


def _headers(token: str | None) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"} if token else {}


def _fail(message: str) -> None:
    console.print(f"[red]✗[/red] {message}")
    raise typer.Exit(1)


def _base_url(daemon_url: str) -> str:
    return daemon_url.rstrip("/")


def _load_control_plane_rows(
    *,
    daemon_url: str,
    auth_token: str | None,
    task_id: str | None = None,
    status: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    if task_id:
        params["task_id"] = task_id
    if status:
        params["status"] = status
    try:
        response = httpx.get(
            f"{_base_url(daemon_url)}/api/v1/control-plane/gauntlet",
            params=params,
            headers=_headers(auth_token),
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"gauntlet request failed: {exc}")
    payload = response.json()
    if not isinstance(payload, list):
        _fail("unexpected gauntlet response shape")
    rows: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            rows.append(cast(dict[str, Any], item))
    return rows


@gauntlet_app.command("list")
def list_gauntlets(
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 25,
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """List task-scoped gauntlet status from the control plane."""

    rows = _load_control_plane_rows(
        daemon_url=daemon_url,
        auth_token=auth_token,
        task_id=task_id,
        status=status,
        limit=limit,
    )
    if not rows:
        console.print("[dim]No gauntlets.[/dim]")
        return

    table = Table(header_style="bold cyan")
    table.add_column("Task", style="bold")
    table.add_column("Status")
    table.add_column("Decision")
    table.add_column("Current gate")
    table.add_column("Actions")
    table.add_column("Title")
    for row in rows:
        action_kinds = ", ".join(action["kind"] for action in row.get("actions", ())) or "-"
        table.add_row(
            row["task_id"],
            row["status"],
            row["final_decision"],
            row.get("current_gate") or "-",
            action_kinds,
            row["title"],
        )
    console.print(table)


@gauntlet_app.command("status")
def gauntlet_status(
    task_id: Annotated[
        str, typer.Argument(help="Task id shown in the control-plane gauntlet view")
    ],
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Show one task's gate/critic status in detail."""

    rows = _load_control_plane_rows(
        daemon_url=daemon_url,
        auth_token=auth_token,
        task_id=task_id,
        limit=1,
    )
    if not rows:
        _fail(f"task {task_id!r} was not found in the gauntlet view")
    row = rows[0]

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan", justify="right")
    summary.add_column()
    summary.add_row("task_id:", row["task_id"])
    summary.add_row("title:", row["title"])
    summary.add_row("status:", row["status"])
    summary.add_row("final_decision:", row["final_decision"])
    summary.add_row("current_gate:", row.get("current_gate") or "-")
    summary.add_row("next_action:", row["next_action"])
    console.print(summary)

    gates = row.get("gates", ())
    if gates:
        gate_table = Table(title="Gates", header_style="bold cyan")
        gate_table.add_column("Gate", style="bold")
        gate_table.add_column("Status")
        gate_table.add_column("Next action")
        for gate in gates:
            gate_table.add_row(gate["name"], gate["status"], gate["next_action"])
        console.print(gate_table)

    findings = row.get("critic_findings", ())
    if findings:
        finding_table = Table(title="Critic Findings", header_style="bold cyan")
        finding_table.add_column("Severity")
        finding_table.add_column("Title", style="bold")
        finding_table.add_column("Detail")
        for finding in findings:
            finding_table.add_row(
                finding["severity"],
                finding["title"],
                finding["detail"],
            )
        console.print(finding_table)

    actions = row.get("actions", ())
    if actions:
        action_table = Table(title="Available Actions", header_style="bold cyan")
        action_table.add_column("Kind", style="bold")
        action_table.add_column("Path")
        action_table.add_column("Expected status")
        for action in actions:
            action_table.add_row(action["kind"], action["path"], action["expected_status"])
        console.print(action_table)


@gauntlet_app.command("retry")
def retry_gauntlet(
    task_id: Annotated[str, typer.Argument(help="Task id to requeue from a failed gate")],
    expected_status: Annotated[str, typer.Option("--expected-status")] = "failed",
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Retry a failed task from the control-plane gauntlet."""

    payload = {"target_id": task_id, "expected_status": expected_status}
    try:
        response = httpx.post(
            f"{_base_url(daemon_url)}/api/v1/control-plane/gauntlet/{task_id}/retry",
            json=payload,
            headers=_headers(auth_token),
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"retry failed: {exc}")

    row = response.json()
    console.print(f"[green]✓[/green] Requeued {row['task_id']} -> {row['status']}")
    console.print(row["next_action"])


@gauntlet_app.command("waive")
def waive_gauntlet(
    task_id: Annotated[
        str, typer.Argument(help="Task id to waive without rewriting failure state")
    ],
    actor: Annotated[str, typer.Option("--actor")] = cast(str, ...),
    reason: Annotated[str, typer.Option("--reason")] = cast(str, ...),
    expected_status: Annotated[str, typer.Option("--expected-status")] = "failed",
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Record a waiver for a failed task while preserving the failed state."""

    payload = {
        "target_id": task_id,
        "expected_status": expected_status,
        "actor": actor,
        "reason": reason,
    }
    try:
        response = httpx.post(
            f"{_base_url(daemon_url)}/api/v1/control-plane/gauntlet/{task_id}/waive",
            json=payload,
            headers=_headers(auth_token),
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _fail(f"waive failed: {exc}")

    row = response.json()
    console.print(f"[green]✓[/green] Waived {row['task_id']} -> {row['final_decision']}")
    console.print(row["next_action"])
