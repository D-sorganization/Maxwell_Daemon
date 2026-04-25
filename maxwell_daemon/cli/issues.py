"""`maxwell-daemon issue ...` subcommands — create / list / dispatch GitHub issues.

Keeps the main `maxwell-daemon` CLI file focused on core commands; everything
GitHub-specific lives here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from maxwell_daemon.config import load_config
from maxwell_daemon.gh import GitHubClient

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


@issue_app.command("dispatch-batch")
def dispatch_batch(
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", "-f", help="Text file: lines of owner/repo#N[:mode]"),
    ] = None,
    repos: Annotated[
        list[str] | None,
        typer.Option(
            "--repo",
            help="owner/repo — repeat for multiple repos",
        ),
    ] = None,
    all_fleet: Annotated[
        bool,
        typer.Option("--all", help="Expand to every enabled repo from fleet.yaml"),
    ] = False,
    fleet_manifest: Annotated[
        Path | None,
        typer.Option(
            "--fleet-manifest",
            help="Path to fleet.yaml (default: cwd/~/.maxwell-daemon)",
        ),
    ] = None,
    label: Annotated[str | None, typer.Option("--label", help="Filter by label")] = None,
    mode: Annotated[str, typer.Option("--mode")] = "plan",
    limit: Annotated[int, typer.Option("--limit")] = 100,
    max_stories: Annotated[
        int | None,
        typer.Option(
            "--max-stories",
            help="Per-repo cap on how many issues to submit this run",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print what would be dispatched without submitting"),
    ] = False,
    daemon_url: Annotated[
        str, typer.Option("--daemon-url", envvar="MAXWELL_DAEMON_URL")
    ] = "http://127.0.0.1:8080",
    auth_token: Annotated[
        str | None, typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN")
    ] = None,
) -> None:
    """Dispatch many issues across one or more repos.

    Input modes (combinable):
      * ``--from-file`` — one ``owner/repo#NUM[:mode]`` per line
      * ``--repo o/r`` (repeatable) — pull open issues matching the label filter
      * ``--all`` — expand to every enabled repo in ``fleet.yaml``

    Flags:
      * ``--max-stories N`` — cap submissions per repo
      * ``--dry-run`` — print the plan, submit nothing
    """
    from maxwell_daemon.cli.batch_dispatch import (
        BatchDispatchPlanner,
        resolve_repos_from_manifest,
    )
    from maxwell_daemon.config.fleet import FleetManifestError, load_fleet_manifest

    if not from_file and not repos and not all_fleet:
        console.print("[red]✗[/red] Pass --from-file, --repo, or --all.")
        raise typer.Exit(1)

    # File input short-circuits the planner: the file itself is the plan.
    items: list[dict[str, object]] = []
    if from_file is not None:
        items.extend(_parse_batch_file(from_file, default_mode=mode))

    # Collect target repos (explicit + fleet-expanded).
    target_repos: list[str] = list(repos or [])
    if all_fleet:
        try:
            manifest = load_fleet_manifest(path=fleet_manifest)
        except FleetManifestError as e:
            console.print(f"[red]✗[/red] {e}")
            raise typer.Exit(1) from None
        target_repos.extend(resolve_repos_from_manifest(manifest))

    # De-dupe while preserving caller order — multiple --repo + --all may overlap.
    target_repos = list(dict.fromkeys(target_repos))

    if target_repos:
        client = GitHubClient()
        planner = BatchDispatchPlanner(list_issues=client.list_issues, max_stories=max_stories)
        plan = asyncio.run(
            planner.plan(repos=target_repos, label=label, mode=mode, state="open", limit=limit)
        )

        _render_plan_summary(plan, dry_run=dry_run, label=label, mode=mode, max_stories=max_stories)
        items.extend({"repo": it.repo, "number": it.number, "mode": it.mode} for it in plan.items)

    if dry_run:
        console.print("[yellow]Dry run — nothing submitted.[/yellow]")
        return

    if not items:
        console.print("[yellow]No issues matched.[/yellow]")
        return

    import httpx

    headers = {"authorization": f"Bearer {auth_token}"} if auth_token else {}
    try:
        r = httpx.post(
            f"{daemon_url}/api/v1/issues/batch-dispatch",
            json={"items": items},
            headers=headers,
            timeout=30.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        console.print(f"[red]✗[/red] Batch dispatch failed: {e}")
        raise typer.Exit(1) from None

    body = r.json()
    console.print(
        f"[green]✓[/green] Dispatched [bold]{body['dispatched']}[/bold] issue(s); "
        f"[{'red' if body['failed'] else 'dim'}]{body['failed']} failed[/]"
    )
    for failure in body.get("failures", []):
        console.print(f"  [red]✗[/red] {failure['repo']}#{failure['number']} — {failure['error']}")


def _render_plan_summary(
    plan: Any,
    *,
    dry_run: bool,
    label: str | None,
    mode: str,
    max_stories: int | None,
) -> None:
    """Print a per-repo rollup table. Purely cosmetic — no dispatch side effects."""
    filter_bits = [f"mode={mode}"]
    if label:
        filter_bits.append(f"label={label}")
    if max_stories is not None:
        filter_bits.append(f"max-stories={max_stories}")
    header = f"{'Dry run: ' if dry_run else ''}Dispatching {len(plan.summaries)} repo(s)"
    console.print(f"\n[bold]{header}[/bold]  ([dim]{', '.join(filter_bits)}[/dim])\n")

    t = Table(header_style="bold cyan")
    t.add_column("Repo")
    t.add_column("Eligible", justify="right")
    t.add_column("Submitted", justify="right")
    t.add_column("Skipped", justify="right")
    for s in plan.summaries:
        t.add_row(s.repo, str(s.eligible), str(s.submitted), str(s.skipped))
    console.print(t)
    console.print(
        f"\n[bold]Total[/bold]: {plan.total_submitted()} submitted, "
        f"{plan.total_skipped()} skipped.\n"
    )


def _parse_batch_file(path: Path, *, default_mode: str) -> list[dict[str, object]]:
    import re

    out: list[dict[str, object]] = []
    line_re = re.compile(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)"
        r"(?::(plan|implement))?\s*$"
    )
    with path.open() as f:
        for raw in f:
            line = raw.split("#")[0].strip() if raw.strip().startswith("#") else raw.strip()
            if not line:
                continue
            m = line_re.match(line)
            if m is None:
                raise typer.BadParameter(
                    f"{path}: unparseable line {raw!r} — expected owner/repo#N[:mode]"
                )
            out.append(
                {
                    "repo": m.group(1),
                    "number": int(m.group(2)),
                    "mode": m.group(3) or default_mode,
                }
            )
    return out


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
        typer.Option("--auth-token", envvar="MAXWELL_API_TOKEN"),
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
