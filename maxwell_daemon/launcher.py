"""Cross-platform first-run launcher for Maxwell-Daemon.

The root ``Launch-Maxwell.*`` wrappers keep OS-specific shell details small and
delegate the actual first-run flow here. The launcher is deliberately boring:
create a local virtual environment if needed, install runtime dependencies,
# initialize a config if one is missing, run doctor, then start the API daemon.
# Trigger CI for coverage check.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import venv
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request

from maxwell_daemon.logging import configure_logging, get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LauncherPlan:
    repo_root: Path
    venv_path: Path
    python_path: Path
    install_args: tuple[str, ...]
    init_args: tuple[str, ...]
    doctor_args: tuple[str, ...]
    serve_args: tuple[str, ...]
    config_path: Path
    ui_url: str


def _venv_python(venv_path: Path) -> Path:
    if os.name == "nt":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "maxwell-daemon" / "maxwell-daemon.yaml"
    return Path.home() / ".config" / "maxwell-daemon" / "maxwell-daemon.yaml"


def build_plan(
    *,
    repo_root: Path,
    dev: bool = False,
    config_path: Path | None = None,
    port: int = 8080,
) -> LauncherPlan:
    root = repo_root.resolve()
    venv_path = root / ".venv"
    python_path = _venv_python(venv_path)
    install_target = ".[dev]" if dev else "."
    config = config_path or default_config_path()
    return LauncherPlan(
        repo_root=root,
        venv_path=venv_path,
        python_path=python_path,
        install_args=(str(python_path), "-m", "pip", "install", "-e", install_target),
        init_args=(
            str(python_path),
            "-m",
            "maxwell_daemon.cli.main",
            "init",
            "--path",
            str(config),
        ),
        doctor_args=(
            str(python_path),
            "-m",
            "maxwell_daemon.cli.main",
            "doctor",
            "--config",
            str(config),
        ),
        serve_args=(
            str(python_path),
            "-m",
            "maxwell_daemon.cli.main",
            "serve",
            "--config",
            str(config),
            "--port",
            str(port),
        ),
        config_path=config,
        ui_url=f"http://127.0.0.1:{port}/ui/",
    )


def _run(args: Sequence[str], *, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, env=_subprocess_env())


def _subprocess_env() -> dict[str, str]:
    """Return the environment for launcher-managed Python subprocesses.

    The launcher shells out to ``maxwell_daemon.cli.main`` for ``init``,
    ``doctor``, and ``serve``. On Windows, those commands may render Rich output
    containing characters such as ``✓``. When stdout/stderr are redirected and no
    UTF-8 mode is active, Python can fall back to a legacy code page like
    CP1252 and fail before the daemon ever starts. Defaulting the subprocess
    environment to UTF-8 keeps the first-run path stable while still allowing an
    operator to override the encoding explicitly.
    """

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def ensure_venv(plan: LauncherPlan) -> None:
    if plan.python_path.exists():
        return
    builder = venv.EnvBuilder(with_pip=True)
    builder.create(plan.venv_path)


def _open_dashboard_when_ready(
    ui_url: str,
    *,
    opener: Callable[[str], bool] = webbrowser.open_new_tab,
    attempts: int = 40,
    delay_seconds: float = 0.25,
) -> None:
    for _ in range(attempts):
        try:
            with request.urlopen(ui_url, timeout=0.5):
                opener(ui_url)
                return
        except (error.URLError, TimeoutError, OSError):
            time.sleep(delay_seconds)


def _launch_dashboard_thread(plan: LauncherPlan) -> None:
    threading.Thread(
        target=_open_dashboard_when_ready,
        args=(plan.ui_url,),
        daemon=True,
        name="maxwell-dashboard-launcher",
    ).start()


def execute_plan(
    plan: LauncherPlan,
    *,
    skip_install: bool = False,
    open_browser: bool = True,
) -> None:
    ensure_venv(plan)
    if not skip_install:
        _run(plan.install_args, cwd=plan.repo_root)
    if not plan.config_path.exists():
        plan.config_path.parent.mkdir(parents=True, exist_ok=True)
        _run(plan.init_args, cwd=plan.repo_root)
    _run(plan.doctor_args, cwd=plan.repo_root)
    if open_browser:
        _launch_dashboard_thread(plan)
    _run(plan.serve_args, cwd=plan.repo_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch Maxwell-Daemon from a source checkout.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Path to the Maxwell-Daemon checkout.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Config path to initialize/use.")
    parser.add_argument("--port", type=int, default=8080, help="API port for maxwell-daemon serve.")
    parser.add_argument("--dev", action="store_true", help="Install developer extras.")
    parser.add_argument("--skip-install", action="store_true", help="Skip pip install.")
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not open the canonical /ui/ dashboard in a browser.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the launch plan and exit.")
    args = parser.parse_args(argv)
    configure_logging(level="INFO")

    plan = build_plan(
        repo_root=args.repo_root,
        dev=args.dev,
        config_path=args.config,
        port=args.port,
    )
    if args.dry_run:
        log.info("repo_root=%s", plan.repo_root)
        log.info("venv=%s", plan.venv_path)
        log.info("python=%s", plan.python_path)
        log.info("install=%s", " ".join(plan.install_args))
        log.info("init=%s", " ".join(plan.init_args))
        log.info("doctor=%s", " ".join(plan.doctor_args))
        log.info("serve=%s", " ".join(plan.serve_args))
        log.info("ui=%s", plan.ui_url)
        return 0

    execute_plan(plan, skip_install=args.skip_install, open_browser=not args.no_open_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
