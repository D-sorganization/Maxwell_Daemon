"""Cross-platform launcher contract tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from maxwell_daemon.launcher import (
    _open_dashboard_when_ready,
    _subprocess_env,
    build_plan,
    default_config_path,
    execute_plan,
    main,
)


def test_runtime_install_is_default(tmp_path: Path) -> None:
    plan = build_plan(repo_root=tmp_path)

    assert plan.install_args[-1] == "."
    assert "dev" not in " ".join(plan.install_args)


def test_dev_install_is_explicit(tmp_path: Path) -> None:
    plan = build_plan(repo_root=tmp_path, dev=True)

    assert plan.install_args[-1] == ".[dev]"


def test_launcher_uses_local_venv(tmp_path: Path) -> None:
    plan = build_plan(repo_root=tmp_path)

    assert plan.venv_path == tmp_path.resolve() / ".venv"
    if os.name == "nt":
        assert plan.python_path == plan.venv_path / "Scripts" / "python.exe"
    else:
        assert plan.python_path == plan.venv_path / "bin" / "python"


def test_plan_runs_doctor_before_serve(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    plan = build_plan(repo_root=tmp_path, config_path=config, port=9090)

    assert plan.init_args[-2:] == ("--path", str(config))
    assert plan.doctor_args[-2:] == ("--config", str(config))
    assert plan.serve_args[-4:] == ("--config", str(config), "--port", "9090")
    assert plan.ui_url == "http://127.0.0.1:9090/ui/"


def test_default_config_path_is_platform_specific() -> None:
    path = default_config_path()

    assert path.name == "maxwell-daemon.yaml"
    assert "maxwell-daemon" in path.parts


def test_root_wrappers_delegate_to_python_launcher() -> None:
    repo = Path(__file__).resolve().parents[2]

    assert "maxwell_daemon.launcher" in (repo / "Launch-Maxwell.bat").read_text()
    assert "maxwell_daemon.launcher" in (repo / "Launch-Maxwell.sh").read_text()
    assert "maxwell_daemon.launcher" in (repo / "Launch-Maxwell.command").read_text()


def test_open_dashboard_when_ready_uses_browser_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(
        "maxwell_daemon.launcher.request.urlopen", lambda *args, **kwargs: _Response()
    )

    _open_dashboard_when_ready(
        "http://127.0.0.1:8080/ui/",
        opener=opened.append,  # type: ignore[arg-type]
        attempts=1,
        delay_seconds=0,
    )

    assert opened == ["http://127.0.0.1:8080/ui/"]


def test_execute_plan_can_skip_browser_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = build_plan(repo_root=tmp_path)
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr("maxwell_daemon.launcher.ensure_venv", lambda _plan: None)
    monkeypatch.setattr(
        "maxwell_daemon.launcher._launch_dashboard_thread",
        lambda _plan: calls.append(("browser",)),
    )
    monkeypatch.setattr(
        "maxwell_daemon.launcher._run", lambda args, *, cwd: calls.append(tuple(args))
    )

    execute_plan(plan, skip_install=True, open_browser=False)

    assert ("browser",) not in calls
    assert plan.doctor_args in calls
    assert plan.serve_args in calls


def test_pyproject_no_longer_advertises_pyqt_desktop_extra() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert 'desktop = ["PyQt6>=6.7.0"]' not in pyproject
    assert "PyQt6>=" not in pyproject


def test_launcher_subprocess_env_defaults_to_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    env = _subprocess_env()

    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_launcher_subprocess_env_preserves_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-16")

    env = _subprocess_env()

    assert env["PYTHONUTF8"] == "0"
    assert env["PYTHONIOENCODING"] == "utf-16"


def test_main_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["--repo-root", str(tmp_path), "--dry-run"]

    # Mock execute_plan to ensure it's not called
    execute_calls = []
    monkeypatch.setattr(
        "maxwell_daemon.launcher.execute_plan", lambda *args, **kwargs: execute_calls.append(args)
    )

    exit_code = main(argv)

    assert exit_code == 0
    assert len(execute_calls) == 0


def test_main_execute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["--repo-root", str(tmp_path), "--skip-install", "--no-open-browser"]

    execute_calls = []
    monkeypatch.setattr(
        "maxwell_daemon.launcher.execute_plan", lambda *args, **kwargs: execute_calls.append(kwargs)
    )

    exit_code = main(argv)

    assert exit_code == 0
    assert len(execute_calls) == 1
    assert execute_calls[0]["skip_install"] is True
    assert execute_calls[0]["open_browser"] is False


def test_ensure_venv_skips_if_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = build_plan(repo_root=tmp_path)
    plan.python_path.parent.mkdir(parents=True, exist_ok=True)
    plan.python_path.touch()

    venv_calls = []
    monkeypatch.setattr("venv.EnvBuilder.create", lambda *args, **kwargs: venv_calls.append(args))

    from maxwell_daemon.launcher import ensure_venv

    ensure_venv(plan)
    assert len(venv_calls) == 0


def test_open_dashboard_when_ready_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = []

    def mock_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        attempts.append(1)
        if len(attempts) < 2:
            raise TimeoutError()
        from unittest.mock import MagicMock

        return MagicMock()

    monkeypatch.setattr("maxwell_daemon.launcher.request.urlopen", mock_urlopen)
    monkeypatch.setattr("time.sleep", lambda x: None)

    opened: list[str] = []
    _open_dashboard_when_ready(
        "http://127.0.0.1:8080/ui/",
        opener=opened.append,  # type: ignore[arg-type]
        attempts=3,
        delay_seconds=0,
    )

    assert len(attempts) == 2
    assert opened == ["http://127.0.0.1:8080/ui/"]
