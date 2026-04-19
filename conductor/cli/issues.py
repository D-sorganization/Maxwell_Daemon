"""`conductor issue ...` subcommands — create / list / dispatch GitHub issues.

Keeps the main `conductor` CLI file focused on core commands; everything
GitHub-specific lives here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from conductor.config import load_config
from conductor.gh import GitHubClient

issue_app = typer.Typer(name="issue", help="Create, list, and dispatch GitHub issues.")
console = Console()


@issue_app.command("new")
def new(
    repo: Annotated[str, typer.Argument(help="owner/repo")],
    title: Annotated[str, typer.Argument(help="Issue title")],
    body: Annotated[str, typer.Option("--body", "-b", help="Issue body (markdown)")] = "",
    label: Annotated[
        list[str] | None,
        typer.Option("--label", "-l", help="Add a label (repeatable)"),
    ] = None,
    dispatch: Annotated[
        bool,
        typer.Option(
            "--dispatch",
            help="After creation, dispatch the daemon to draft a PR",
        ),
    ] = False,
    mode: Annotated[
        str,
        typer.Option("--mode", help="plan | implement"),
    ] = "plan",
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Create a new GitHub issue. Optionally dispatch the daemon immediately."""
    client = GitHubClient()

    async def _run() -> str:
        url = await client.create_issue(repo, title=title, body=body, labels=label or [])
        console.print(f"[green]✓[/green] Created: {url}")
        return url

    url = asyncio.run(_run())

    if dispatch:
        _dispatch_url(url, mode=mode, config=config)


@issue_app.command("list")
def list_issues(
    repo: Annotated[str, typer.Argument(help="owner/repo")],
    state: Annotated[str, typer.Option("--state", help="open | closed | all")] = "open",
    limit: Annotated[int, typer.Option("--limit")] = 25,
) -> None:
    """List issues in a repository."""
    client = GitHubClient()
    issues = asyncio.run(client.list_issues(repo, state=state, limit=limit))

    if not issues:
        console.print("[dim]No issues found.[/dim]")
        return

    t = Table(title=f"{repo} — {state}", header_style="bold cyan")
    t.add_column("#", justify="right")
    t.add_column("Title")
    t.add_column("Labels")
    for issue in issues:
        t.add_row(
            str(issue.number),
            issue.title,
            ", ".join(issue.labels),
        )
    console.print(t)


@issue_app.command("dispatch")
def dispatch(
    repo: Annotated[str, typer.Argument(help="owner/repo")],
    number: Annotated[int, typer.Argument(help="Issue number")],
    mode: Annotated[str, typer.Option("--mode", help="plan | implement")] = "plan",
    daemon_url: Annotated[
        str,
        typer.Option("--daemon-url", help="REST endpoint of a running daemon"),
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None,
        typer.Option("--auth-token", envvar="CONDUCTOR_API_TOKEN"),
    ] = None,
) -> None:
    """Queue an existing issue for the daemon to draft a PR against."""
    _post_dispatch(
        daemon_url=daemon_url,
        repo=repo,
        number=number,
        mode=mode,
        auth_token=auth_token,
    )


def _dispatch_url(url: str, *, mode: str, config: Path | None) -> None:
    import re

    match = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", url)
    if not match:
        console.print(f"[yellow]Could not parse issue URL {url!r} — skipping dispatch.[/yellow]")
        return
    repo, number = match.group(1), int(match.group(2))
    load_config(config)  # validates that config is loadable
    _post_dispatch(
        daemon_url="http://127.0.0.1:8080",
        repo=repo,
        number=number,
        mode=mode,
        auth_token=None,
    )


def _post_dispatch(
    *,
    daemon_url: str,
    repo: str,
    number: int,
    mode: str,
    auth_token: str | None,
) -> None:
    import httpx

    headers = {}
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    try:
        r = httpx.post(
            f"{daemon_url}/api/v1/issues/dispatch",
            json={"repo": repo, "number": number, "mode": mode},
            headers=headers,
            timeout=10.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        console.print(f"[red]✗[/red] Dispatch failed: {e}")
        raise typer.Exit(1) from None

    body = r.json()
    console.print(
        f"[green]✓[/green] Dispatched — task [bold]{body['id']}[/bold] "
        f"(mode={mode}) against [bold]{repo}#{number}[/bold]"
    )
