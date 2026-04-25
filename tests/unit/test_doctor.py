"""`maxwell-daemon doctor` — preflight health diagnostic.

Checks the kind of things that fail silently at 3am. Each check returns a
CheckResult; the overall exit code is non-zero if any are red.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maxwell_daemon.cli.main import app
from maxwell_daemon.core.doctor import (
    CheckResult,
    Severity,
    check_backends_healthy,
    check_config_loadable,
    check_disk_space,
    check_gh_auth,
    check_ledger_writable,
    run_all_checks,
)


class TestCheckResult:
    def test_ok_status(self) -> None:
        r = CheckResult(name="x", severity=Severity.OK, message="yes")
        assert r.is_ok is True

    def test_warn_not_ok(self) -> None:
        r = CheckResult(name="x", severity=Severity.WARN, message="meh")
        assert r.is_ok is False

    def test_error_not_ok(self) -> None:
        r = CheckResult(name="x", severity=Severity.ERROR, message="nope")
        assert r.is_ok is False


class TestConfigCheck:
    def test_missing_config_reports_error(self, tmp_path: Path) -> None:
        result = check_config_loadable(tmp_path / "nonexistent.yaml")
        assert result.severity is Severity.ERROR
        assert "not found" in result.message.lower()

    def test_invalid_config_reports_error(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text("backends: {}\nagent:\n  default_backend: x\n")
        result = check_config_loadable(path)
        assert result.severity is Severity.ERROR

    def test_valid_config_ok(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text(
            "backends:\n  x:\n    type: ollama\n    model: m\nagent:\n  default_backend: x\n"
        )
        result = check_config_loadable(path)
        assert result.severity is Severity.OK


class TestLedgerCheck:
    def test_writable_path_ok(self, tmp_path: Path) -> None:
        result = check_ledger_writable(tmp_path / "ledger.db")
        assert result.severity is Severity.OK

    def test_unwritable_path_reports_error(self, tmp_path: Path) -> None:
        # Create a read-only parent so even creating the DB fails.
        parent = tmp_path / "ro"
        parent.mkdir()
        parent.chmod(0o555)
        try:
            result = check_ledger_writable(parent / "ledger.db")
            assert result.severity is Severity.ERROR
        finally:
            parent.chmod(0o755)


class TestGhAuthCheck:
    def test_gh_unreachable_reports_warn(self) -> None:
        async def fake_runner(*argv: str, **_: object) -> tuple[int, bytes, bytes]:
            return 127, b"", b"gh: command not found"

        result = asyncio.run(check_gh_auth(runner=fake_runner))
        assert result.severity is Severity.WARN

    def test_gh_auth_missing_reports_warn(self) -> None:
        async def fake_runner(*argv: str, **_: object) -> tuple[int, bytes, bytes]:
            return 1, b"", b"not logged in"

        result = asyncio.run(check_gh_auth(runner=fake_runner))
        assert result.severity is Severity.WARN

    def test_gh_authed_reports_ok(self) -> None:
        async def fake_runner(*argv: str, **_: object) -> tuple[int, bytes, bytes]:
            return 0, b"logged in", b""

        result = asyncio.run(check_gh_auth(runner=fake_runner))
        assert result.severity is Severity.OK


class TestDiskSpaceCheck:
    def test_returns_ok_when_healthy(self, tmp_path: Path) -> None:
        result = check_disk_space(tmp_path, minimum_mb=1)
        assert result.severity is Severity.OK

    def test_returns_warn_below_threshold(self, tmp_path: Path) -> None:
        # Require a stupidly large amount so the check can't pass.
        result = check_disk_space(tmp_path, minimum_mb=10**12)
        assert result.severity is Severity.WARN


class TestBackendsHealthCheck:
    def test_empty_backends_reports_warn(self) -> None:
        result = asyncio.run(check_backends_healthy(backends=[]))
        assert result.severity is Severity.WARN

    def test_all_healthy_reports_ok(self) -> None:
        class _B:
            name = "fake"

            async def health_check(self) -> bool:
                return True

        result = asyncio.run(check_backends_healthy(backends=[_B(), _B()]))
        assert result.severity is Severity.OK

    def test_any_unhealthy_reports_warn(self) -> None:
        class _Healthy:
            name = "ok"

            async def health_check(self) -> bool:
                return True

        class _Broken:
            name = "bad"

            async def health_check(self) -> bool:
                return False

        result = asyncio.run(check_backends_healthy(backends=[_Healthy(), _Broken()]))
        assert result.severity is Severity.WARN
        assert "bad" in result.message


class TestRunAllChecks:
    def test_returns_list_of_results(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text(
            "backends:\n  x:\n    type: ollama\n    model: m\nagent:\n  default_backend: x\n"
        )
        results = asyncio.run(
            run_all_checks(config_path=path, ledger_path=tmp_path / "l.db")
        )
        assert len(results) >= 4
        assert all(isinstance(r, CheckResult) for r in results)


class TestDoctorCommand:
    def test_exit_zero_when_all_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = CliRunner()
        path = tmp_path / "c.yaml"
        path.write_text(
            "backends:\n  x:\n    type: ollama\n    model: m\nagent:\n  default_backend: x\n"
        )

        # Force gh-auth and backends-health to pass so the CLI exits 0.
        from maxwell_daemon.core import doctor

        async def _ok_gh(runner: object = None) -> CheckResult:
            return CheckResult("github cli", Severity.OK, "ok")

        async def _ok_backends(backends: object = None) -> CheckResult:
            return CheckResult("backends", Severity.OK, "ok")

        monkeypatch.setattr(doctor, "check_gh_auth", _ok_gh)
        monkeypatch.setattr(doctor, "check_backends_healthy", _ok_backends)

        r = runner.invoke(
            app,
            [
                "doctor",
                "--config",
                str(path),
                "--ledger",
                str(tmp_path / "l.db"),
            ],
        )
        assert r.exit_code == 0
        assert "healthy" in r.stdout.lower() or "ok" in r.stdout.lower()

    def test_exit_nonzero_when_a_check_fails(self, tmp_path: Path) -> None:
        runner = CliRunner()
        # Missing config → config check fails → non-zero exit.
        r = runner.invoke(
            app,
            [
                "doctor",
                "--config",
                str(tmp_path / "nonexistent.yaml"),
                "--ledger",
                str(tmp_path / "l.db"),
            ],
        )
        assert r.exit_code != 0
