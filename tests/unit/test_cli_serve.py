"""CLI ``serve`` wiring tests — config path threaded into the Daemon (#976)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import maxwell_daemon.cli.main as cli_main
from maxwell_daemon.cli.main import app

_MINIMAL_YAML = {
    "backends": {"primary": {"type": "recording", "model": "test-model"}},
    "agent": {"default_backend": "primary"},
}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def config_file(tmp_path: Path, register_recording_backend: None) -> Path:
    p = tmp_path / "explicit.yaml"
    p.write_text(yaml.safe_dump(_MINIMAL_YAML), encoding="utf-8")
    return p


def test_serve_threads_explicit_config_path_into_daemon(
    runner: CliRunner, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve --config X`` must build ``Daemon(cfg, config_path=X)`` so a
    SIGHUP/SIGUSR1 hot-reload re-reads X — not the default path (#976)."""
    captured: dict[str, Any] = {}

    class _FakeDaemon:
        def __init__(self, cfg: Any, *, config_path: Path | None = None) -> None:
            captured["config_path"] = config_path

    # ``serve`` imports Daemon lazily (``from maxwell_daemon.daemon import
    # Daemon``), so patch it at its source module. Stub the event loop entry so
    # we capture wiring without booting uvicorn or an actual asyncio loop.
    import maxwell_daemon.daemon as daemon_mod

    def _fake_run(coro: Any, *_a: Any, **_k: Any) -> None:
        # Close the coroutine so it isn't flagged as "never awaited".
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(daemon_mod, "Daemon", _FakeDaemon, raising=False)
    monkeypatch.setattr(cli_main.asyncio, "run", _fake_run)

    result = runner.invoke(app, ["serve", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    assert captured["config_path"] == config_file.expanduser()
