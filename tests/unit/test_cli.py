"""CLI — init, status, backends, health, ask via Typer's CliRunner."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from maxwell_daemon.backends import registry
from maxwell_daemon.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def populated_config(tmp_path: Path, register_recording_backend: None) -> Path:
    from maxwell_daemon.config import MaxwellDaemonConfig, save_config

    cfg = MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "primary": {"type": "recording", "model": "test-model"},
            },
            "agent": {"default_backend": "primary"},
        }
    )
    path = tmp_path / "c.yaml"
    save_config(cfg, path)
    return path


class TestVersion:
    def test_version_flag(self, runner: CliRunner) -> None:
        r = runner.invoke(app, ["--version"])
        assert r.exit_code == 0
        assert "maxwell-daemon" in r.stdout.lower()


class TestInit:
    def test_writes_starter_config(self, runner: CliRunner, tmp_path: Path) -> None:
        out_path = tmp_path / "new.yaml"
        r = runner.invoke(app, ["init", "--path", str(out_path)])
        assert r.exit_code == 0
        assert out_path.exists()

    def test_refuses_to_overwrite(self, runner: CliRunner, tmp_path: Path) -> None:
        out_path = tmp_path / "existing.yaml"
        out_path.write_text("backends:\n  x:\n    type: y\n    model: z\n")
        r = runner.invoke(app, ["init", "--path", str(out_path)])
        assert r.exit_code == 1
        assert "already exists" in r.stdout

    def test_force_overwrites(self, runner: CliRunner, tmp_path: Path) -> None:
        out_path = tmp_path / "existing.yaml"
        out_path.write_text("garbage")
        r = runner.invoke(app, ["init", "--path", str(out_path), "--force"])
        assert r.exit_code == 0


class TestStatus:
    def test_reports_configured_backends(self, runner: CliRunner, populated_config: Path) -> None:
        r = runner.invoke(app, ["status", "--config", str(populated_config)])
        assert r.exit_code == 0
        assert "primary" in r.stdout

    def test_reports_missing_config(self, runner: CliRunner, tmp_path: Path) -> None:
        r = runner.invoke(app, ["status", "--config", str(tmp_path / "missing.yaml")])
        assert r.exit_code == 1


class TestBackendsCommand:
    def test_lists_registered_adapters(
        self, runner: CliRunner, register_recording_backend: None
    ) -> None:
        r = runner.invoke(app, ["backends"])
        assert r.exit_code == 0
        assert "recording" in r.stdout


class TestHealth:
    def test_healthy_backend_passes(self, runner: CliRunner, populated_config: Path) -> None:
        r = runner.invoke(app, ["health", "--config", str(populated_config)])
        assert r.exit_code == 0
        assert "healthy" in r.stdout

    def test_unhealthy_backend_fails(self, runner: CliRunner, tmp_path: Path) -> None:
        from maxwell_daemon.config import MaxwellDaemonConfig, save_config
        from tests.conftest import RecordingBackend

        class UnhealthyBackend(RecordingBackend):
            def __init__(self, **kw) -> None:  # type: ignore[no-untyped-def]
                super().__init__(healthy=False, **kw)

        registry._factories["unhealthy"] = UnhealthyBackend
        try:
            cfg = MaxwellDaemonConfig.model_validate(
                {
                    "backends": {"sick": {"type": "unhealthy", "model": "x"}},
                    "agent": {"default_backend": "sick"},
                }
            )
            path = tmp_path / "c.yaml"
            save_config(cfg, path)
            r = runner.invoke(app, ["health", "--config", str(path)])
            assert r.exit_code == 1
            assert "unreachable" in r.stdout
        finally:
            registry._factories.pop("unhealthy", None)


class TestAsk:
    def test_one_shot_prompt(self, runner: CliRunner, populated_config: Path) -> None:
        r = runner.invoke(
            app,
            ["ask", "hello world", "--config", str(populated_config), "--no-stream"],
        )
        assert r.exit_code == 0
        assert "ok" in r.stdout
        assert "tokens" in r.stdout
