"""Maxwell-Daemon CLI — the primary user-facing entrypoint."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from maxwell_daemon import __version__
from maxwell_daemon.backends import Message, MessageRole, registry
from maxwell_daemon.cli.issues import issue_app
from maxwell_daemon.cli.session import session_app
from maxwell_daemon.cli.spec import spec_app
from maxwell_daemon.cli.tasks import tasks_app
from maxwell_daemon.config import (
    MaxwellDaemonConfig,
    load_config,
    save_config,
)
from maxwell_daemon.config.loader import default_config_path
from maxwell_daemon.core import BackendRouter

app = typer.Typer(
    name="maxwell-daemon",
    help="Multi-backend autonomous code agent orchestrator.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
app.add_typer(issue_app, name="issue")
app.add_typer(spec_app, name="spec")
app.add_typer(session_app, name="session")
app.add_typer(tasks_app, name="tasks")
console = Console()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V"),
) -> None:
    if version:
        console.print(f"maxwell-daemon {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command()
def init(
    path: Annotated[Path | None, typer.Option("--path", "-p", help="Config path")] = None,
    force: Annotated[bool, typer.Option("--force", "-f")] = False,
) -> None:
    """Create a starter maxwell-daemon.yaml."""
    target = path or default_config_path()
    if target.exists() and not force:
        console.print(f"[yellow]Config already exists at {target}[/yellow]")
        console.print("Pass --force to overwrite.")
        raise typer.Exit(1)

    cfg = MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "claude": {
                    "type": "claude",
                    "model": "claude-sonnet-4-6",
                    "api_key": "${ANTHROPIC_API_KEY}",
                },
                "ollama": {
                    "type": "ollama",
                    "model": "llama3.1",
                    "base_url": "http://localhost:11434",
                },
            },
            "agent": {"default_backend": "claude"},
            "api": {"enabled": True, "host": "127.0.0.1", "port": 8080},
        }
    )
    written = save_config(cfg, target)
    console.print(
        Panel.fit(
            f"[green]✓[/green] Wrote starter config to [bold]{written}[/bold]\n"
            f"Edit it, then run [bold]maxwell-daemon status[/bold] to verify.",
            title="Maxwell-Daemon initialized",
            border_style="green",
        )
    )


@app.command()
def status(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Show configured backends, repos, and fleet members."""
    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1) from None

    table = Table(title="Backends", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Model")
    table.add_column("Enabled")
    for name, b in cfg.backends.items():
        mark = "[green]✓[/green]" if b.enabled else "[red]✗[/red]"
        table.add_row(name, b.type, b.model, mark)
    console.print(table)

    if cfg.repos:
        rt = Table(title="Repos", show_header=True, header_style="bold cyan")
        rt.add_column("Name")
        rt.add_column("Path")
        rt.add_column("Slots")
        rt.add_column("Backend")
        for r in cfg.repos:
            rt.add_row(r.name, str(r.path), str(r.slots), r.backend or "(default)")
        console.print(rt)

    console.print(f"\nDefault backend: [bold]{cfg.agent.default_backend}[/bold]")
    console.print(f"Available adapters: {', '.join(registry.available())}")


@app.command()
def backends() -> None:
    """List all registered backend adapters."""
    table = Table(title="Registered Backend Adapters", header_style="bold cyan")
    table.add_column("Adapter")
    table.add_column("Module")
    for name in registry.available():
        table.add_row(name, f"maxwell_daemon.backends.{name}")
    console.print(table)


@app.command()
def health(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Probe each configured backend for reachability."""
    cfg = load_config(config)
    router = BackendRouter(cfg)

    async def _run() -> int:
        failures = 0
        table = Table(title="Backend Health", header_style="bold cyan")
        table.add_column("Name")
        table.add_column("Status")
        for name in router.available_backends():
            try:
                decision = router.route(backend_override=name)
                ok = await decision.backend.health_check()
                table.add_row(name, "[green]healthy[/green]" if ok else "[red]unreachable[/red]")
                if not ok:
                    failures += 1
            except Exception as e:
                table.add_row(name, f"[red]error: {e}[/red]")
                failures += 1
        console.print(table)
        return failures

    failures = asyncio.run(_run())
    if failures:
        raise typer.Exit(1)


@app.command()
def ask(
    prompt: Annotated[str, typer.Argument(help="Prompt to send to the backend")],
    backend: Annotated[str | None, typer.Option("--backend", "-b")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    stream: Annotated[bool, typer.Option("--stream/--no-stream")] = True,
) -> None:
    """Send a one-shot prompt to the configured backend (for smoke-testing)."""
    cfg = load_config(config)
    router = BackendRouter(cfg)
    decision = router.route(backend_override=backend, model_override=model)

    console.print(
        f"[dim]→ routing to [bold]{decision.backend_name}[/bold] "
        f"({decision.model}) — {decision.reason}[/dim]"
    )

    async def _run() -> None:
        msgs = [Message(role=MessageRole.USER, content=prompt)]
        if stream:
            async for chunk in decision.backend.stream(msgs, model=decision.model):
                console.print(chunk, end="", soft_wrap=True)
            console.print()
        else:
            resp = await decision.backend.complete(msgs, model=decision.model)
            console.print(resp.content)
            cost = decision.backend.estimate_cost(resp.usage, decision.model)
            console.print(f"\n[dim]tokens: {resp.usage.total_tokens}  cost: ${cost:.4f}[/dim]")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/yellow]")
        sys.exit(130)


@app.command()
def cost(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    ledger: Annotated[Path | None, typer.Option("--ledger", help="Ledger DB path")] = None,
) -> None:
    """Show current month-to-date spend and budget status."""
    from maxwell_daemon.core import BudgetEnforcer, CostLedger

    cfg = load_config(config)
    ledger_path = ledger or Path.home() / ".local/share/maxwell-daemon/ledger.db"
    ledger_obj = CostLedger(ledger_path)
    enforcer = BudgetEnforcer(cfg.budget, ledger_obj)
    check = enforcer.check()

    status_color = {"ok": "green", "alert": "yellow", "exceeded": "red"}[check.status]
    forecast_line = ""
    if check.forecast_usd is not None and check.forecast_usd > 0:
        forecast_line = f"\n[bold]Forecast (month-end):[/bold] ${check.forecast_usd:.2f}"
        if check.limit_usd is not None:
            headroom = check.limit_usd - check.forecast_usd
            headroom_colour = "green" if headroom > 0 else "red"
            forecast_line += (
                f"  [[{headroom_colour}]{'+' if headroom >= 0 else ''}"
                f"${headroom:.2f} vs limit[/{headroom_colour}]]"
            )
    console.print(
        Panel.fit(
            f"[bold]Month-to-date:[/bold] ${check.spent_usd:.2f}\n"
            f"[bold]Limit:[/bold] "
            f"{'$' + format(check.limit_usd, '.2f') if check.limit_usd else '(unset)'}\n"
            f"[bold]Utilisation:[/bold] {check.utilisation:.1%}"
            f"{forecast_line}\n"
            f"[bold]Status:[/bold] [{status_color}]{check.status}[/{status_color}]",
            title="Cost summary",
            border_style=status_color,
        )
    )

    by_backend = ledger_obj.by_backend(
        datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    )
    if by_backend:
        t = Table(title="By backend", header_style="bold cyan")
        t.add_column("Backend")
        t.add_column("Spend (USD)", justify="right")
        for name, spend in sorted(by_backend.items(), key=lambda kv: -kv[1]):
            t.add_row(name, f"${spend:.4f}")
        console.print(t)


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    ledger: Annotated[Path | None, typer.Option("--ledger")] = None,
) -> None:
    """Run every preflight check and print a green/yellow/red summary."""
    from maxwell_daemon.core.doctor import Severity, run_all_checks

    cfg_path = config or Path.home() / ".config/maxwell-daemon/maxwell-daemon.yaml"
    ledger_path = ledger or Path.home() / ".local/share/maxwell-daemon/ledger.db"

    results = asyncio.run(run_all_checks(config_path=cfg_path, ledger_path=ledger_path))

    icon = {
        Severity.OK: "[green]✓[/green]",
        Severity.WARN: "[yellow]⚠[/yellow]",
        Severity.ERROR: "[red]✗[/red]",
    }
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column("")
    t.add_column("Check", style="bold")
    t.add_column("Detail", style="dim")
    for r in results:
        t.add_row(icon[r.severity], r.name, r.message)
    console.print(t)

    errors = [r for r in results if r.severity is Severity.ERROR]
    warns = [r for r in results if r.severity is Severity.WARN]
    if errors:
        console.print(f"\n[red]{len(errors)} error(s), {len(warns)} warning(s)[/red]")
        raise typer.Exit(1)
    if warns:
        console.print(
            f"\n[yellow]{len(warns)} warning(s) — daemon will run but may be degraded[/yellow]"
        )
    else:
        console.print("\n[green]All checks healthy.[/green]")


@app.command()
def serve(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8080,
    workers: Annotated[int, typer.Option("--workers")] = 2,
) -> None:
    """Run the daemon + REST API together (foreground)."""
    import uvicorn

    from maxwell_daemon.api import create_app
    from maxwell_daemon.daemon import Daemon

    cfg = load_config(config)
    daemon = Daemon(cfg)

    async def _boot() -> None:
        await daemon.start(worker_count=workers)

    asyncio.run(_boot())

    try:
        fastapi_app = create_app(daemon, auth_token=cfg.api.auth_token)
        console.print(f"[green]✓[/green] Maxwell-Daemon serving on http://{host}:{port}")
        uvicorn.run(fastapi_app, host=host, port=port, log_level="info")
    finally:
        asyncio.run(daemon.stop())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
