"""CLI `daemon serve` and `cost` commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def populated_config(tmp_path: Path, register_recording_backend: None) -> Path:
    from conductor.config import ConductorConfig, save_config

    cfg = ConductorConfig.model_validate(
        {
            "backends": {"primary": {"type": "recording", "model": "m"}},
            "agent": {"default_backend": "primary"},
        }
    )
    path = tmp_path / "c.yaml"
    save_config(cfg, path)
    return path


class TestCostCommand:
    def test_reports_zero_spend_initially(
        self, runner: CliRunner, populated_config: Path, tmp_path: Path
    ) -> None:
        r = runner.invoke(
            app,
            [
                "cost",
                "--config",
                str(populated_config),
                "--ledger",
                str(tmp_path / "ledger.db"),
            ],
        )
        assert r.exit_code == 0
        assert "0.00" in r.stdout

    def test_shows_budget_limit_when_configured(
        self, runner: CliRunner, tmp_path: Path, register_recording_backend: None
    ) -> None:
        from conductor.config import ConductorConfig, save_config

        cfg = ConductorConfig.model_validate(
            {
                "backends": {"primary": {"type": "recording", "model": "m"}},
                "agent": {"default_backend": "primary"},
                "budget": {"monthly_limit_usd": 50.0},
            }
        )
        path = tmp_path / "c.yaml"
        save_config(cfg, path)
        r = runner.invoke(app, ["cost", "--config", str(path), "--ledger", str(tmp_path / "l.db")])
        assert r.exit_code == 0
        assert "$50" in r.stdout or "50.00" in r.stdout
