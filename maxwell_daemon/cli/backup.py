"""CLI commands for backup, restore, and JSON export of Maxwell-Daemon state."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from maxwell_daemon.core.backup import BackupManager, RestoreError

backup_app = typer.Typer(
    help="Backup, restore, and export Maxwell-Daemon state.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


def _make_manager(
    config: Path | None,
    data_dir: Path | None,
) -> BackupManager:
    return BackupManager(config_path=config, data_dir=data_dir)


@backup_app.command("export")
def export_cmd(
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Destination archive path (.tar.gz)"),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Config file path"),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Data directory override"),
    ] = None,
) -> None:
    """Export all Maxwell-Daemon state to a timestamped .tar.gz archive."""
    mgr = _make_manager(config, data_dir)
    try:
        archive = mgr.export(out)
    except Exception as exc:
        err_console.print(f"[red]Export failed:[/red] {exc}")
        raise typer.Exit(1) from None

    console.print(
        Panel.fit(
            f"[green]✓[/green] Archive written to [bold]{archive}[/bold]",
            title="Backup export",
            border_style="green",
        )
    )


@backup_app.command("restore")
def restore_cmd(
    archive: Annotated[Path, typer.Argument(help="Path to the .tar.gz backup archive")],
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing data without prompting"),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Config file path"),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Data directory override"),
    ] = None,
) -> None:
    """Restore Maxwell-Daemon state from a backup archive.

    Validates every file hash before writing anything to disk.
    Use --force to overwrite existing state.
    """
    mgr = _make_manager(config, data_dir)
    try:
        mgr.restore(archive, force=force)
    except RestoreError as exc:
        err_console.print(f"[red]Restore failed:[/red] {exc}")
        raise typer.Exit(1) from None
    except Exception as exc:
        err_console.print(f"[red]Unexpected error during restore:[/red] {exc}")
        raise typer.Exit(1) from None

    console.print(
        Panel.fit(
            "[green]✓[/green] State restored successfully.",
            title="Backup restore",
            border_style="green",
        )
    )


@backup_app.command("export-json")
def export_json_cmd(
    component: Annotated[
        str,
        typer.Argument(
            help=(
                "Component to export: config, audit, ledger, tasks, work_items, "
                "task_graphs, actions, artifacts_db, delegate_sessions, "
                "auth_sessions, memory_db"
            )
        ),
    ],
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write JSON to this file instead of stdout"),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Config file path"),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Data directory override"),
    ] = None,
) -> None:
    """Export a single component to JSON for inspection.

    Example: maxwell-daemon backup export-json ledger --out ledger.json
    """
    mgr = _make_manager(config, data_dir)
    try:
        data = mgr.export_json(component)
    except ValueError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None
    except Exception as exc:
        err_console.print(f"[red]Unexpected error:[/red] {exc}")
        raise typer.Exit(1) from None

    serialised = json.dumps(data, indent=2, default=str)

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(serialised, encoding="utf-8")
        console.print(f"[green]✓[/green] Wrote {component} JSON to [bold]{out}[/bold]")
    else:
        sys.stdout.write(serialised + "\n")


@backup_app.command("info")
def info_cmd(
    archive: Annotated[Path, typer.Argument(help="Path to the .tar.gz backup archive")],
) -> None:
    """Show the manifest and component summary of a backup archive."""
    import tarfile

    archive = archive.expanduser().resolve()
    if not archive.exists():
        err_console.print(f"[red]Archive not found:[/red] {archive}")
        raise typer.Exit(1)

    try:
        with tarfile.open(archive, "r:gz") as tar:
            manifest_member = tar.getmember("maxwell-backup/manifest.json")
            fh = tar.extractfile(manifest_member)
            if fh is None:
                raise RuntimeError("Could not read manifest.json from archive")
            manifest_data = json.loads(fh.read().decode("utf-8"))
    except Exception as exc:
        err_console.print(f"[red]Failed to read archive:[/red] {exc}")
        raise typer.Exit(1) from None

    table = Table(title="Backup manifest", header_style="bold cyan")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Created at", manifest_data.get("created_at", "?"))
    table.add_row("Schema version", manifest_data.get("schema_version", "?"))
    table.add_row("Config path", manifest_data.get("config_path", "?"))
    table.add_row("Data dir", manifest_data.get("data_dir", "?"))

    hashes = manifest_data.get("hashes", {})
    components = [k for k in hashes if k not in ("artifacts", "memory")]
    components += ["artifacts", "memory"] if "artifacts" in hashes else []
    components += ["memory"] if "memory" in hashes else []
    table.add_row("Components", ", ".join(components))
    console.print(table)
