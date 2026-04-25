"""CLI — init, status, backends, health, ask via Typer's CliRunner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maxwell_daemon.backends import BackendCapabilities, registry
from maxwell_daemon.cli.main import app
from tests.conftest import RecordingBackend


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


class TestFleetStatus:
    def test_renders_registry_status_table(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        payload: dict[str, object] = {
            "repo": "acme/repo",
            "tool": "dispatch",
            "required_capabilities": ["gpu"],
            "selected_node": {"node_id": "node-a", "hostname": "alpha"},
            "nodes": [
                {
                    "node_id": "node-a",
                    "hostname": "alpha",
                    "eligible": True,
                    "score": 100,
                    "reasons": [],
                    "active_sessions": 1,
                    "tailscale_status": {"online": True},
                },
                {
                    "node_id": "node-b",
                    "hostname": "beta",
                    "eligible": False,
                    "score": None,
                    "reasons": ["missing capability gpu"],
                    "active_sessions": 0,
                    "tailscale_status": {"online": False},
                },
            ],
            "explanation": "selected alpha",
        }

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def get(self, url: str, *, params: object, headers: object) -> _Response:
                return _Response()

        monkeypatch.setattr("maxwell_daemon.cli.fleet.httpx.Client", _Client)

        r = runner.invoke(app, ["fleet", "status", "--repo", "acme/repo", "--tool", "dispatch"])

        assert r.exit_code == 0
        assert "Repo:" in r.stdout
        assert "alpha" in r.stdout
        assert "beta" in r.stdout
        assert "missing capability gpu" in r.stdout

    def test_nodes_alias_renders_registry_status_table(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        payload: dict[str, object] = {
            "repo": "acme/repo",
            "tool": "dispatch",
            "required_capabilities": ["gpu"],
            "selected_node": {"node_id": "node-a", "hostname": "alpha"},
            "nodes": [
                {
                    "node_id": "node-a",
                    "hostname": "alpha",
                    "eligible": True,
                    "score": 100,
                    "reasons": [],
                    "active_sessions": 1,
                    "tailscale_status": {"online": True},
                }
            ],
            "explanation": "selected alpha",
        }

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def get(self, url: str, *, params: object, headers: object) -> _Response:
                return _Response()

        monkeypatch.setattr("maxwell_daemon.cli.fleet.httpx.Client", _Client)

        r = runner.invoke(app, ["fleet", "nodes", "--repo", "acme/repo", "--tool", "dispatch"])

        assert r.exit_code == 0
        assert "Repo:" in r.stdout
        assert "alpha" in r.stdout

    def test_fetches_registry_status_and_redacts_private_fields(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        payload: dict[str, object] = {
            "repo": "acme/repo",
            "tool": "dispatch",
            "required_capabilities": ["gpu"],
            "selected_node": {
                "node_id": "node-a",
                "hostname": "alpha",
                "eligible": True,
                "score": 100,
                "reasons": [],
                "capability_names": ["gpu"],
                "capabilities": [
                    {
                        "name": "gpu",
                        "observed_at": "2026-04-22T17:59:00+00:00",
                        "has_value": True,
                    }
                ],
                "policy": {
                    "has_repo_allowlist": True,
                    "has_tool_allowlist": True,
                    "allowed_repo_count": 1,
                    "allowed_tool_count": 1,
                    "max_concurrent_sessions": 2,
                    "heartbeat_stale_after_seconds": 600,
                },
                "active_sessions": 0,
                "heartbeat_at": "2026-04-22T17:59:00+00:00",
                "heartbeat_age_seconds": 60,
                "tailscale_status": {
                    "peer_id": "node-a",
                    "hostname": "alpha",
                    "online": True,
                    "last_seen_at": "2026-04-22T17:58:00+00:00",
                },
            },
            "nodes": [
                {
                    "node_id": "node-a",
                    "hostname": "alpha",
                    "eligible": True,
                    "score": 100,
                    "reasons": [],
                    "capability_names": ["gpu"],
                    "capabilities": [
                        {
                            "name": "gpu",
                            "observed_at": "2026-04-22T17:59:00+00:00",
                            "has_value": True,
                        }
                    ],
                    "policy": {
                        "has_repo_allowlist": True,
                        "has_tool_allowlist": True,
                        "allowed_repo_count": 1,
                        "allowed_tool_count": 1,
                        "max_concurrent_sessions": 2,
                        "heartbeat_stale_after_seconds": 600,
                    },
                    "active_sessions": 0,
                    "heartbeat_at": "2026-04-22T17:59:00+00:00",
                    "heartbeat_age_seconds": 60,
                    "tailscale_status": {
                        "peer_id": "node-a",
                        "hostname": "alpha",
                        "online": True,
                        "last_seen_at": "2026-04-22T17:58:00+00:00",
                    },
                }
            ],
            "explanation": "selected 'alpha' for repo 'acme/repo' and tool 'dispatch'; rejections=[]",
        }

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return payload

        class _Client:
            def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                self.calls: list[tuple[str, dict[str, object]]] = []

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def get(self, url: str, *, params: object, headers: object) -> _Response:
                self.calls.append((url, {"params": params, "headers": headers}))
                return _Response()

        monkeypatch.setattr("maxwell_daemon.cli.fleet.httpx.Client", _Client)

        r = runner.invoke(
            app,
            [
                "fleet",
                "status",
                "--repo",
                "acme/repo",
                "--tool",
                "dispatch",
                "--required-capability",
                "gpu",
                "--json",
            ],
        )

        assert r.exit_code == 0
        body = json.loads(r.stdout)
        assert body["selected_node"]["hostname"] == "alpha"
        assert "tailnet_ip" not in r.stdout
        assert "allowed_repo_count" in r.stdout


class TestMemory:
    def test_memory_status_reports_store_state(
        self, runner: CliRunner, tmp_path: Path, register_recording_backend: None
    ) -> None:
        from maxwell_daemon.config import MaxwellDaemonConfig, save_config

        workspace = tmp_path / "memory-workspace"
        raw_dir = workspace / ".maxwell" / "raw_logs"
        raw_dir.mkdir(parents=True)
        (raw_dir / "session.log").write_text("raw memory", encoding="utf-8")
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {"primary": {"type": "recording", "model": "test-model"}},
                "agent": {"default_backend": "primary"},
                "memory": {
                    "workspace_path": str(workspace),
                    "dream_interval_seconds": 900,
                },
            }
        )
        config_path = tmp_path / "c.yaml"
        save_config(cfg, config_path)

        r = runner.invoke(app, ["memory", "status", "--config", str(config_path)])

        assert r.exit_code == 0
        assert "Raw logs" in r.stdout
        assert "900s" in r.stdout


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

    def test_one_shot_prompt_handles_unknown_cost(self, runner: CliRunner, tmp_path: Path) -> None:
        from maxwell_daemon.config import MaxwellDaemonConfig, save_config

        class UnknownCostBackend(RecordingBackend):
            def capabilities(self, model: str) -> BackendCapabilities:
                return BackendCapabilities()

        registry._factories["unknown-cost"] = UnknownCostBackend
        try:
            cfg = MaxwellDaemonConfig.model_validate(
                {
                    "backends": {"primary": {"type": "unknown-cost", "model": "test-model"}},
                    "agent": {"default_backend": "primary"},
                }
            )
            config_path = tmp_path / "c.yaml"
            save_config(cfg, config_path)

            r = runner.invoke(
                app,
                ["ask", "hello world", "--config", str(config_path), "--no-stream"],
            )
        finally:
            registry._factories.pop("unknown-cost", None)

        assert r.exit_code == 0
        assert "ok" in r.stdout
        assert "cost: unknown" in r.stdout


class TestCrossAudit:
    def test_cross_audit_json_output(self, runner: CliRunner, populated_config: Path) -> None:
        r = runner.invoke(
            app,
            [
                "cross-audit",
                "review this task",
                "--config",
                str(populated_config),
                "--json",
            ],
        )

        assert r.exit_code == 0
        assert '"summary"' in r.stdout
        assert "Cross-audit completed: 1 succeeded, 0 failed" in r.stdout
        assert '"backend": "primary"' in r.stdout

    def test_cross_audit_rejects_unknown_role(
        self, runner: CliRunner, populated_config: Path
    ) -> None:
        r = runner.invoke(
            app,
            [
                "cross-audit",
                "review this task",
                "--config",
                str(populated_config),
                "--role",
                "not-a-role",
            ],
        )

        assert r.exit_code == 2
        assert "Unknown role" in r.stdout
