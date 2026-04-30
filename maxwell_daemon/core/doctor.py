"""Preflight health diagnostic.

``maxwell-daemon doctor`` runs every check exposed here and renders a red/yellow/
green summary. Each check is a small pure function so the logic is test-
reachable without spinning up a full daemon.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "CheckResult",
    "Severity",
    "check_backends_healthy",
    "check_config_loadable",
    "check_disk_space",
    "check_gh_auth",
    "check_ledger_writable",
    "run_all_checks",
]

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


class Severity(Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(slots=True, frozen=True)
class CheckResult:
    name: str
    severity: Severity
    message: str

    @property
    def is_ok(self) -> bool:
        return self.severity is Severity.OK


def check_config_loadable(path: Path) -> CheckResult:
    from maxwell_daemon.config import load_config

    if not Path(path).exists():
        return CheckResult("config", Severity.ERROR, f"config not found at {path}")
    try:
        load_config(path)
    except Exception as e:  # noqa: BLE001
        return CheckResult("config", Severity.ERROR, f"config invalid: {e}")
    return CheckResult("config", Severity.OK, f"loaded {path}")


def check_ledger_writable(path: Path) -> CheckResult:
    from maxwell_daemon.core.ledger import CostLedger

    try:
        parent = path.expanduser().parent
        if parent.exists() and parent.stat().st_mode & 0o222 == 0:
            return CheckResult("ledger", Severity.ERROR, f"ledger parent is not writable: {parent}")
        CostLedger(path)
    except Exception as e:  # noqa: BLE001
        return CheckResult("ledger", Severity.ERROR, f"ledger not writable: {e}")
    return CheckResult("ledger", Severity.OK, f"writable at {path}")


def check_disk_space(path: Path, *, minimum_mb: int = 500) -> CheckResult:
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    free_mb = usage.free // (1024 * 1024)
    if free_mb < minimum_mb:
        return CheckResult(
            "disk",
            Severity.WARN,
            f"only {free_mb} MB free at {path} (minimum {minimum_mb} MB)",
        )
    return CheckResult("disk", Severity.OK, f"{free_mb} MB free")


async def _default_runner(*argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


async def check_gh_auth(*, runner: RunnerFn | None = None) -> CheckResult:
    """Verify that ``gh`` is installed and authenticated.

    We warn (not error) because the agent can still run prompt-only tasks
    against the LLM backends; it only needs ``gh`` for issue dispatch.
    """
    runner = runner or _default_runner
    try:
        rc, _, err = await runner("gh", "auth", "status")
    except FileNotFoundError:
        return CheckResult(
            "github cli",
            Severity.WARN,
            "gh CLI not installed — issue dispatch won't work",
        )
    if rc == 127:
        return CheckResult(
            "github cli",
            Severity.WARN,
            "gh CLI not on PATH — issue dispatch won't work",
        )
    if rc != 0:
        detail = err.decode(errors="replace").strip().splitlines()[0] if err else ""
        return CheckResult(
            "github cli",
            Severity.WARN,
            f"gh auth failed: {detail or 'run `gh auth login`'}",
        )
    return CheckResult("github cli", Severity.OK, "authenticated")


async def check_backends_healthy(*, backends: Iterable[Any]) -> CheckResult:
    """Probe every backend's ``health_check`` in parallel."""
    backends = list(backends)
    if not backends:
        return CheckResult("backends", Severity.WARN, "no backends configured")

    async def probe(b: Any) -> tuple[str, bool]:
        try:
            return b.name, await b.health_check()
        except Exception:  # noqa: BLE001
            return b.name, False

    results = await asyncio.gather(*(probe(b) for b in backends))
    unhealthy = [name for name, ok in results if not ok]
    if unhealthy:
        return CheckResult(
            "backends",
            Severity.WARN,
            f"{len(unhealthy)} unhealthy: {', '.join(unhealthy)}",
        )
    return CheckResult("backends", Severity.OK, f"{len(results)} healthy")


async def run_all_checks(*, config_path: Path, ledger_path: Path) -> list[CheckResult]:
    """Run every check in a sensible order. Returns per-check results.

    Order matters for UX: we run the quick filesystem checks first so the
    user sees the obvious ones light up before the slow network checks.
    """
    results: list[CheckResult] = []

    cfg_result = check_config_loadable(config_path)
    results.append(cfg_result)
    results.append(check_ledger_writable(ledger_path))
    results.append(check_disk_space(ledger_path.parent))
    results.append(await check_gh_auth())

    # Backends health requires a loadable config — skip if the config failed.
    if cfg_result.is_ok:
        from maxwell_daemon.config import load_config
        from maxwell_daemon.core import BackendRouter

        cfg = load_config(config_path)
        router = BackendRouter(cfg)
        backends: list[Any] = []
        for name in router.available_backends():
            try:
                backends.append(router.route(backend_override=name).backend)
            except Exception as exc:  # noqa: BLE001
                results.append(
                    CheckResult(
                        "backends",
                        Severity.WARN,
                        f"backend {name} could not be initialized: {exc}",
                    )
                )
        results.append(await check_backends_healthy(backends=backends))

    return results
