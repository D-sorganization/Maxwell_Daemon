from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from maxwell_daemon.core.artifacts import ArtifactKind, ArtifactStore
from maxwell_daemon.core.gates import GateDefinition
from maxwell_daemon.sandbox.gates import SandboxGateAdapter
from maxwell_daemon.sandbox.runner import SandboxRunResult


class FakeExecutor:
    def __init__(self, result: SandboxRunResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str], float]] = []

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


def gate(
    tmp_path: Path,
    *,
    policy: str,
    command: list[str] | tuple[str, ...] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    workspace_root: Path | None = None,
    timeout_seconds: float | None = None,
    output_summary_bytes: int | None = None,
    network_enabled: bool | None = None,
    allow_gpu: bool | None = None,
    task_id: str | None = None,
    work_item_id: str | None = None,
) -> GateDefinition:
    metadata: dict[str, str] = {
        "sandbox.policy": policy,
        "sandbox.workspace_root": str(workspace_root or tmp_path),
    }
    if command is not None:
        metadata["sandbox.command"] = json.dumps(list(command))
    if cwd is not None:
        metadata["sandbox.cwd"] = cwd
    if env is not None:
        metadata["sandbox.env"] = json.dumps(env)
    if timeout_seconds is not None:
        metadata["sandbox.timeout_seconds"] = str(timeout_seconds)
    if output_summary_bytes is not None:
        metadata["sandbox.output_summary_bytes"] = str(output_summary_bytes)
    if network_enabled is not None:
        metadata["sandbox.network_enabled"] = str(network_enabled).lower()
    if allow_gpu is not None:
        metadata["sandbox.allow_gpu"] = str(allow_gpu).lower()
    if task_id is not None:
        metadata["sandbox.task_id"] = task_id
    if work_item_id is not None:
        metadata["sandbox.work_item_id"] = work_item_id
    return GateDefinition(
        gate_id="gate-1",
        name="Sandbox Gate",
        adapter="sandbox",
        metadata=metadata,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "command", "expected"),
    [
        ("unit-tests", None, ("python", "-m", "pytest")),
        ("lint", None, ("ruff", "check", ".")),
        ("typecheck", None, ("mypy", ".")),
        (
            "custom-command",
            ["python", "-m", "pytest", "tests/unit/test_sandbox_gate_adapter.py"],
            ("python", "-m", "pytest", "tests/unit/test_sandbox_gate_adapter.py"),
        ),
    ],
)
async def test_supports_preset_and_custom_policy_commands(
    tmp_path: Path,
    policy: str,
    command: list[str] | None,
    expected: tuple[str, ...],
) -> None:
    executor = FakeExecutor(SandboxRunResult(returncode=0, stdout="ok", stderr=""))
    adapter = SandboxGateAdapter(executor=executor)

    decision = await adapter.run(gate(tmp_path, policy=policy, command=command))

    assert decision.passed is True
    assert executor.calls[0][0] == expected
    assert any(item == "stdout=ok" for item in decision.evidence)
    assert any(item == "stderr=" for item in decision.evidence)


@pytest.mark.asyncio
async def test_pass_preserves_snippets_and_redacts_secrets(tmp_path: Path) -> None:
    executor = FakeExecutor(
        SandboxRunResult(
            returncode=0,
            stdout="stdout secret-token\nstill fine",
            stderr="stderr secret-token\n",
        )
    )
    adapter = SandboxGateAdapter(executor=executor)

    decision = await adapter.run(
        gate(
            tmp_path,
            policy="unit-tests",
            env={"MAXWELL_TOKEN": "secret-token"},
            output_summary_bytes=80,
        )
    )

    evidence = "\n".join(decision.evidence)
    assert decision.passed is True
    assert "secret-token" not in evidence
    assert "[REDACTED]" in evidence
    assert "stdout=" in evidence
    assert "stderr=" in evidence
    assert "summary=" in evidence
    assert "output_summary_bytes=80" in evidence


@pytest.mark.asyncio
async def test_nonzero_exit_preserves_failure_evidence(tmp_path: Path) -> None:
    executor = FakeExecutor(
        SandboxRunResult(
            returncode=2,
            stdout="collected 1 item",
            stderr="AssertionError: boom",
        )
    )
    adapter = SandboxGateAdapter(executor=executor)

    decision = await adapter.run(gate(tmp_path, policy="lint"))

    assert decision.passed is False
    evidence = "\n".join(decision.evidence)
    assert "returncode=2" in evidence
    assert "stdout=collected 1 item" in evidence
    assert "stderr=AssertionError: boom" in evidence


@pytest.mark.asyncio
async def test_denied_command_fails_before_execution(tmp_path: Path) -> None:
    executor = FakeExecutor(SandboxRunResult(returncode=0, stdout="unused"))
    adapter = SandboxGateAdapter(executor=executor)

    decision = await adapter.run(
        gate(
            tmp_path,
            policy="custom-command",
            command=["rm", "-rf", "."],
        )
    )

    assert decision.passed is False
    assert executor.calls == []
    assert "command denied" in "\n".join(decision.evidence)
    assert "timeout_seconds=300" in "\n".join(decision.evidence)
    assert "network_enabled=false" in "\n".join(decision.evidence)


@pytest.mark.asyncio
async def test_timeout_failures_are_reported_closed(tmp_path: Path) -> None:
    executor = FakeExecutor(
        SandboxRunResult(returncode=None, timed_out=True, error="timeout after 3s")
    )
    adapter = SandboxGateAdapter(executor=executor)

    decision = await adapter.run(gate(tmp_path, policy="typecheck"))

    assert decision.passed is False
    evidence = "\n".join(decision.evidence)
    assert "timed_out=true" in evidence
    assert "error=timeout after 3s" in evidence


@pytest.mark.asyncio
async def test_missing_workspace_metadata_fails_closed(tmp_path: Path) -> None:
    executor = FakeExecutor(SandboxRunResult(returncode=0, stdout="unused"))
    adapter = SandboxGateAdapter(executor=executor)
    metadata = {
        "sandbox.policy": "unit-tests",
    }
    decision = await adapter.run(
        GateDefinition(
            gate_id="gate-1",
            name="Sandbox Gate",
            adapter="sandbox",
            metadata=metadata,
        )
    )

    assert decision.passed is False
    assert executor.calls == []
    assert "missing sandbox.workspace_root metadata" in "\n".join(decision.evidence)


@pytest.mark.asyncio
async def test_missing_custom_command_metadata_fails_closed(tmp_path: Path) -> None:
    executor = FakeExecutor(SandboxRunResult(returncode=0, stdout="unused"))
    adapter = SandboxGateAdapter(executor=executor)

    decision = await adapter.run(gate(tmp_path, policy="custom-command"))

    assert decision.passed is False
    assert executor.calls == []
    assert "missing command metadata" in "\n".join(decision.evidence)


@pytest.mark.asyncio
async def test_network_flags_flow_into_policy_evidence(tmp_path: Path) -> None:
    executor = FakeExecutor(SandboxRunResult(returncode=0, stdout="ok"))
    adapter = SandboxGateAdapter(executor=executor)

    decision = await adapter.run(
        gate(
            tmp_path,
            policy="unit-tests",
            network_enabled=True,
            allow_gpu=True,
        )
    )

    evidence = "\n".join(decision.evidence)
    assert decision.passed is True
    assert "network_enabled=true" in evidence
    assert "allow_gpu=true" in evidence


@pytest.mark.asyncio
async def test_persists_redacted_execution_artifact_for_task_owner(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(
        SandboxRunResult(
            returncode=1,
            stdout="stdout secret-token",
            stderr="stderr secret-token",
        )
    )
    artifact_store = ArtifactStore(
        tmp_path / "artifacts.db", blob_root=tmp_path / "artifacts"
    )
    adapter = SandboxGateAdapter(executor=executor, artifact_store=artifact_store)

    decision = await adapter.run(
        gate(
            tmp_path,
            policy="custom-command",
            command=["python", "-m", "pytest"],
            env={"MAXWELL_TOKEN": "secret-token"},
            task_id="task-1",
        )
    )

    artifacts = artifact_store.list_for_task(
        "task-1", kind=ArtifactKind.SANDBOX_EXECUTION
    )
    assert len(artifacts) == 1
    artifact = artifacts[0]
    payload = json.loads(artifact_store.read_text(artifact.id))

    assert decision.passed is False
    assert artifact.kind is ArtifactKind.SANDBOX_EXECUTION
    assert payload["gate"]["decision"]["status"] == "failed"
    assert payload["gate"]["decision"]["passed"] is False
    assert payload["execution"]["stdout"] == "stdout [REDACTED]"
    assert payload["execution"]["stderr"] == "stderr [REDACTED]"
    assert payload["execution"]["summary"].startswith("stdout [REDACTED]")
    assert "secret-token" not in json.dumps(payload)
    assert any(item == f"artifact_id={artifact.id}" for item in decision.evidence)
