"""Cross-platform launcher contract tests."""

from __future__ import annotations

import os
from pathlib import Path

from maxwell_daemon.launcher import _subprocess_env, build_plan, default_config_path


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


def test_default_config_path_is_platform_specific() -> None:
    path = default_config_path()

    assert path.name == "maxwell-daemon.yaml"
    assert "maxwell-daemon" in path.parts


def test_root_wrappers_delegate_to_python_launcher() -> None:
    repo = Path(__file__).resolve().parents[2]

    assert "maxwell_daemon.launcher" in (repo / "Launch-Maxwell.bat").read_text()
    assert "maxwell_daemon.launcher" in (repo / "Launch-Maxwell.sh").read_text()
    assert "maxwell_daemon.launcher" in (repo / "Launch-Maxwell.command").read_text()


def test_launcher_subprocess_env_defaults_to_utf8(monkeypatch) -> None:
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    env = _subprocess_env()

    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_launcher_subprocess_env_preserves_explicit_overrides(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-16")

    env = _subprocess_env()

    assert env["PYTHONUTF8"] == "0"
    assert env["PYTHONIOENCODING"] == "utf-16"
