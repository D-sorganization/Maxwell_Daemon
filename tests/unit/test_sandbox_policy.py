from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from maxwell_daemon.sandbox import (
    CommandExecutor,
    SandboxCommandRunner,
    SandboxPolicy,
    SandboxRunResult,
)


class RecordingExecutor(CommandExecutor):
    def __init__(self, result: SandboxRunResult | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str], float]] = []
        self.result = result or SandboxRunResult(returncode=0, stdout="ok")

    async def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> SandboxRunResult:
        self.calls.append((argv, cwd, dict(env), timeout_seconds))
        return self.result


def policy(tmp_path: Path, **overrides: object) -> SandboxPolicy:
    kwargs = {
        "allowed_commands": {"python", "pytest"},
        "env_allowlist": {"PATH", "MAXWELL_SAFE"},
        "secret_env_keys": {"MAXWELL_TOKEN"},
        "timeout_seconds": 3.0,
        "output_summary_bytes": 80,
    }
    kwargs.update(overrides)
    return SandboxPolicy.for_workspace(tmp_path, **kwargs)  # type: ignore[arg-type]


def test_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    decision = policy(tmp_path).validate_command(
        ["python", "-m", "pytest"], cwd=tmp_path.parent
    )

    assert decision.passed is False
    assert decision.status == "path_denied"
    assert "escapes sandbox workspace" in (decision.evidence_value("reason") or "")
    assert decision.evidence_value("network_enabled") == "false"
    assert decision.evidence_value("output_summary_bytes") == "80"


def test_resolves_relative_workspace_paths_without_escape(tmp_path: Path) -> None:
    sandbox_policy = policy(tmp_path)
    safe_dir = tmp_path / "nested" / "safe"
    safe_dir.mkdir(parents=True)

    resolved = sandbox_policy.workspace.resolve_inside(
        Path("nested") / ".." / "nested" / "safe"
    )

    assert resolved == safe_dir.resolve()
    assert resolved is not None


@pytest.mark.asyncio
async def test_denied_command_fails_before_execution(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    runner = SandboxCommandRunner(executor=executor)

    decision = await runner.run(
        ["rm", "-rf", "."], policy=policy(tmp_path), cwd=tmp_path
    )

    assert decision.passed is False
    assert decision.status == "policy_denied"
    assert executor.calls == []
    assert "command denied" in (decision.evidence_value("reason") or "")
    assert decision.evidence_value("timeout_seconds") == "3"
    assert decision.evidence_value("output_summary_bytes") == "80"


@pytest.mark.asyncio
async def test_env_filtering_passes_only_allowlisted_keys(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    runner = SandboxCommandRunner(executor=executor)

    decision = await runner.run(
        ["python", "-m", "pytest"],
        policy=policy(tmp_path),
        cwd=tmp_path,
        env={"PATH": "bin", "MAXWELL_SAFE": "1", "MAXWELL_TOKEN": "secret-token"},
    )

    assert decision.passed is True
    assert executor.calls[0][2] == {"PATH": "bin", "MAXWELL_SAFE": "1"}
    assert decision.evidence_value("env_keys") == "MAXWELL_SAFE,PATH"
    assert decision.evidence_value("network_enabled") == "false"


@pytest.mark.asyncio
async def test_secrets_are_redacted_from_summaries(tmp_path: Path) -> None:
    executor = RecordingExecutor(
        SandboxRunResult(returncode=1, stdout="token=secret-token\n", stderr="failed")
    )
    runner = SandboxCommandRunner(executor=executor)

    decision = await runner.run(
        ["python", "-m", "pytest"],
        policy=policy(tmp_path),
        cwd=tmp_path,
        env={"MAXWELL_TOKEN": "secret-token", "PATH": "bin"},
    )

    summary = decision.evidence_value("summary") or ""
    assert decision.status == "failed"
    assert "secret-token" not in summary
    assert "[REDACTED]" in summary


@pytest.mark.asyncio
async def test_timeout_result_state_can_be_represented(tmp_path: Path) -> None:
    executor = RecordingExecutor(SandboxRunResult(returncode=None, timed_out=True))
    runner = SandboxCommandRunner(executor=executor)

    decision = await runner.run(
        ["python", "-m", "pytest"], policy=policy(tmp_path), cwd=tmp_path
    )

    assert decision.passed is False
    assert decision.status == "timeout"
    assert decision.evidence_value("timed_out") == "true"
    assert decision.evidence_value("returncode") == ""


@pytest.mark.asyncio
async def test_failed_commands_include_evidence(tmp_path: Path) -> None:
    executor = RecordingExecutor(
        SandboxRunResult(
            returncode=2, stdout="collected 1 item", stderr="AssertionError"
        )
    )
    runner = SandboxCommandRunner(executor=executor)

    decision = await runner.run(["pytest"], policy=policy(tmp_path), cwd=tmp_path)

    assert decision.passed is False
    assert decision.status == "failed"
    assert decision.evidence_value("returncode") == "2"
    assert "AssertionError" in (decision.evidence_value("summary") or "")


def test_output_summary_is_truncated_after_redaction(tmp_path: Path) -> None:
    sandbox_policy = policy(tmp_path, output_summary_bytes=16)

    summary = sandbox_policy.summarize_output(
        "prefix secret-token " + ("x" * 100),
        "",
        env={"MAXWELL_TOKEN": "secret-token"},
    )

    assert summary.startswith("... truncated ...")
    assert "secret-token" not in summary
    assert len(summary.encode()) < 40


def test_network_policy_flags_are_part_of_decision_evidence(tmp_path: Path) -> None:
    sandbox_policy = policy(
        tmp_path,
        network_enabled=True,
        allow_gpu=True,
    )

    decision = sandbox_policy.validate_command(["python", "-m", "pytest"], cwd=tmp_path)

    assert decision.passed is True
    assert decision.evidence_value("network_enabled") == "true"
    assert decision.evidence_value("allow_gpu") == "true"
