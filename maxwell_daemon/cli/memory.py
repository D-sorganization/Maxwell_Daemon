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

memory_app = typer.Typer(help="Inspect and anneal local markdown memory.")
console = Console()


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
    table.add_row("Markdown memory", "present" if memory_status.memory_exists else "missing")
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
