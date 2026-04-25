"""CLI commands for the local markdown memory store."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from maxwell_daemon.config import load_config
from maxwell_daemon.core import BackendRouter
from maxwell_daemon.core.memory_annealer import MemoryAnnealer
from maxwell_daemon.core.roles import Role, RoleOrchestrator
from maxwell_daemon.memory import MemoryEntry, MemoryProposal, RepoMemoryStore

memory_app = typer.Typer(help="Inspect and anneal local markdown memory.")
repo_app = typer.Typer(help="Manage repo-carried memory proposals and snapshots.")
console = Console()

memory_app.add_typer(repo_app, name="repo")


@memory_app.command()
def status(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Show local markdown memory and dream-cycle configuration."""
    cfg = load_config(config)
    annealer = MemoryAnnealer(workspace=cfg.memory_workspace_path)
    memory_status = annealer.status()

    table = Table(title="Memory", show_header=True, header_style="bold cyan")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("Workspace", str(memory_status.workspace))
    table.add_row("Raw logs", str(memory_status.raw_log_count))
    table.add_row("Raw bytes", str(memory_status.raw_bytes))
    table.add_row(
        "Markdown memory", "present" if memory_status.memory_exists else "missing"
    )
    table.add_row(
        "Dream interval",
        (
            "disabled"
            if cfg.memory_dream_interval_seconds == 0
            else f"{cfg.memory_dream_interval_seconds}s"
        ),
    )
    console.print(table)


@memory_app.command()
def anneal(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Run one local markdown memory anneal pass."""
    cfg = load_config(config)
    annealer = MemoryAnnealer(workspace=cfg.memory_workspace_path)
    if annealer.status().raw_log_count == 0:
        console.print("No raw memory to anneal.")
        return

    router = BackendRouter(cfg)
    role = Role(
        name="memory_summarizer",
        system_prompt=(
            "You consolidate raw Maxwell-Daemon execution logs into concise, durable "
            "markdown memory. Preserve technical decisions, repository conventions, "
            "and lessons learned. Drop transient chatter and secrets."
        ),
    )
    summarizer = RoleOrchestrator(router).assign_player(role)
    result = asyncio.run(
        MemoryAnnealer(
            workspace=cfg.memory_workspace_path,
            summarizer_role=summarizer,
        ).anneal()
    )
    console.print(result)


@memory_app.command("export")
def export_memory(
    repo_root: Annotated[
        Path,
        typer.Argument(help="Path to the repository root that carries .maxwell/memory"),
    ],
    scope: Annotated[
        str, typer.Option("--scope", help="Scope to export, e.g. repo:Foo")
    ],
    out_path: Annotated[
        Path, typer.Option("--out", "-o", help="Path to write the JSONL to")
    ],
) -> None:
    """Export memory entries of a given scope to a JSONL file."""
    store = RepoMemoryStore(repo_root)
    store.export_jsonl(scope, out_path)
    console.print(f"[green]✓[/green] Exported memory scope {scope!r} to {out_path}")


@memory_app.command("import")
def import_memory(
    repo_root: Annotated[
        Path,
        typer.Argument(help="Path to the repository root that carries .maxwell/memory"),
    ],
    in_path: Annotated[
        Path, typer.Option("--in", "-i", help="Path to read the JSONL from")
    ],
    target_scope: Annotated[
        str, typer.Option("--scope", help="Scope to import into, e.g. repo:Foo")
    ],
    allow_promotion: Annotated[
        bool,
        typer.Option(
            "--allow-promotion",
            help="Allow promoting 'personal' memory to a broader scope",
        ),
    ] = False,
) -> None:
    """Import memory entries from a JSONL file into a given scope."""
    store = RepoMemoryStore(repo_root)
    count = store.import_jsonl(in_path, target_scope, allow_promotion=allow_promotion)
    console.print(
        f"[green]✓[/green] Imported {count} memory entries into scope {target_scope!r}"
    )


@repo_app.command("list")
def list_entries(
    repo_root: Annotated[
        Path,
        typer.Argument(help="Path to the repository root that carries .maxwell/memory"),
    ],
    repo_id: Annotated[
        str, typer.Option("--repo-id", help="owner/repo id used by memory entries")
    ],
    include_superseded: Annotated[
        bool, typer.Option("--include-superseded", help="Show superseded entries too")
    ] = False,
) -> None:
    """List accepted memory entries for one repo root."""
    store = RepoMemoryStore(repo_root)
    entries = store.list_entries(repo_id=repo_id, include_superseded=include_superseded)
    if not entries:
        console.print("[dim]No memory entries.[/dim]")
        return

    table = Table(title=f"Repo memory — {repo_id}", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Scope")
    table.add_column("Kind")
    table.add_column("Confidence", justify="right")
    table.add_column("Source")
    table.add_column("Body")
    for entry in entries:
        table.add_row(
            entry.id,
            entry.scope,
            entry.kind,
            f"{entry.confidence:.2f}",
            entry.source,
            entry.body.replace("\n", " ")[:120],
        )
    console.print(table)


@repo_app.command("proposals")
def list_proposals(
    repo_root: Annotated[
        Path,
        typer.Argument(help="Path to the repository root that carries .maxwell/memory"),
    ],
) -> None:
    """List proposal history for one repo root."""
    store = RepoMemoryStore(repo_root)
    proposals = store.latest_proposals()
    if not proposals:
        console.print("[dim]No proposals.[/dim]")
        return

    table = Table(title="Memory proposals", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Scope")
    table.add_column("Proposed by")
    table.add_column("Reviewer")
    table.add_column("Reason")
    for proposal in proposals:
        table.add_row(
            proposal.id,
            proposal.status,
            proposal.target_scope,
            proposal.proposed_by,
            proposal.reviewed_by or "",
            proposal.reason,
        )
    console.print(table)


@repo_app.command("propose")
def propose_entry(
    repo_root: Annotated[
        Path,
        typer.Argument(help="Path to the repository root that carries .maxwell/memory"),
    ],
    entry_id: Annotated[str, typer.Argument(help="Stable memory entry id")],
    repo_id: Annotated[
        str, typer.Option("--repo-id", help="owner/repo id used by memory entries")
    ],
    body: Annotated[str, typer.Option("--body", "-b", help="Memory body text")],
    source: Annotated[str, typer.Option("--source", help="Evidence source")],
    proposed_by: Annotated[
        str, typer.Option("--proposed-by", help="Delegate or reviewer id")
    ],
    reason: Annotated[str, typer.Option("--reason", help="Why this proposal exists")],
    scope: Annotated[str, typer.Option("--scope")] = "repo",
    kind: Annotated[str, typer.Option("--kind")] = "semantic",
    work_item_id: Annotated[
        str | None, typer.Option("--work-item-id", help="Issue/work-item id if scoped")
    ] = None,
    confidence: Annotated[float, typer.Option("--confidence")] = 0.8,
    supersedes: Annotated[
        list[str] | None,
        typer.Option("--supersedes", help="Entries superseded by this one"),
    ] = None,
) -> None:
    """Create a pending proposal and write it to the repo-carried memory store."""
    store = RepoMemoryStore(repo_root)
    proposal = MemoryProposal(
        id=entry_id,
        proposed_by=proposed_by,
        reason=reason,
        evidence=(source,),
        target_scope=scope,
        entry=MemoryEntry(
            id=entry_id,
            scope=scope,
            repo_id=repo_id,
            work_item_id=work_item_id,
            kind=kind,
            body=body,
            source=source,
            confidence=confidence,
            supersedes=tuple(supersedes or ()),
        ),
    )
    store.propose(proposal)
    console.print(f"[green]✓[/green] Proposed memory entry {entry_id}")


@repo_app.command("review")
def review_proposal(
    repo_root: Annotated[
        Path,
        typer.Argument(help="Path to the repository root that carries .maxwell/memory"),
    ],
    proposal_id: Annotated[str, typer.Argument(help="Proposal id")],
    reviewer: Annotated[
        str, typer.Option("--reviewer", help="Reviewer or policy actor")
    ],
    status: Annotated[str, typer.Option("--status")] = "accepted",
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Terminal review for a pending proposal."""
    store = RepoMemoryStore(repo_root)
    if status == "accepted":
        proposal = store.accept_proposal(proposal_id, reviewer=reviewer, reason=reason)
    elif status == "rejected":
        proposal = store.reject_proposal(proposal_id, reviewer=reviewer, reason=reason)
    elif status == "superseded":
        proposal = store.supersede_proposal(
            proposal_id, reviewer=reviewer, reason=reason
        )
    else:
        console.print("[red]x[/red] --status must be accepted, rejected, or superseded")
        raise typer.Exit(2)
    console.print(f"[green]✓[/green] {proposal.id} -> {proposal.status}")


@repo_app.command("accept")
def accept_proposal(
    repo_root: Annotated[
        Path,
        typer.Argument(help="Path to the repository root that carries .maxwell/memory"),
    ],
    proposal_id: Annotated[str, typer.Argument(help="Proposal id")],
    reviewer: Annotated[
        str, typer.Option("--reviewer", help="Reviewer or policy actor")
    ],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Accept a pending proposal and persist its entry."""
    review_proposal(
        repo_root, proposal_id, reviewer=reviewer, status="accepted", reason=reason
    )


@repo_app.command("reject")
def reject_proposal(
    repo_root: Annotated[
        Path,
        typer.Argument(help="Path to the repository root that carries .maxwell/memory"),
    ],
    proposal_id: Annotated[str, typer.Argument(help="Proposal id")],
    reviewer: Annotated[
        str, typer.Option("--reviewer", help="Reviewer or policy actor")
    ],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Reject a pending proposal."""
    review_proposal(
        repo_root, proposal_id, reviewer=reviewer, status="rejected", reason=reason
    )


@repo_app.command("snapshot")
def snapshot(
    repo_root: Annotated[
        Path,
        typer.Argument(help="Path to the repository root that carries .maxwell/memory"),
    ],
    repo_id: Annotated[
        str, typer.Option("--repo-id", help="owner/repo id used by memory entries")
    ],
    issue_number: Annotated[
        int | None,
        typer.Option("--issue-number", help="Issue/work item id for scoped matches"),
    ] = None,
    max_items: Annotated[int, typer.Option("--max-items", min=0)] = 12,
    token_budget: Annotated[int, typer.Option("--token-budget", min=0)] = 800,
) -> None:
    """Render the selected memory snapshot for a repo root."""
    store = RepoMemoryStore(repo_root)
    rendered = store.render_snapshot(
        repo_id=repo_id,
        work_item_id=str(issue_number) if issue_number is not None else None,
        max_items=max_items,
        token_budget=token_budget,
    )
    if not rendered:
        console.print("[dim]No memory selected.[/dim]")
        return
    console.print(rendered)
