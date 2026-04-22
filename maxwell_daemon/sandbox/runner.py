"""Execution wrapper for sandbox policy decisions."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from maxwell_daemon.contracts import ensure
from maxwell_daemon.core.artifacts import ArtifactKind, ArtifactStore
from maxwell_daemon.sandbox.artifacts import build_execution_payload
from maxwell_daemon.sandbox.policy import DecisionStatus, GateDecision, GateEvidence, SandboxPolicy


@dataclass(slots=True, frozen=True)
class SandboxRunResult:
    """Raw execution result from an injected command executor."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    error: str = ""


class CommandExecutor(Protocol):
    async def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> SandboxRunResult: ...


class SubprocessCommandExecutor:
    """Small subprocess adapter kept separate from sandbox policy evaluation."""

    async def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> SandboxRunResult:
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=dict(env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            return SandboxRunResult(
                returncode=proc.returncode,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                duration_seconds=time.monotonic() - start,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SandboxRunResult(
                returncode=None,
                duration_seconds=time.monotonic() - start,
                timed_out=True,
                error=f"timeout after {timeout_seconds:g}s",
            )
        except OSError as exc:
            return SandboxRunResult(
                returncode=None,
                duration_seconds=time.monotonic() - start,
                error=str(exc),
            )


class SandboxCommandRunner:
    """Validate policy, filter env, execute, and return a gate-like decision."""

    def __init__(self, *, executor: CommandExecutor | None = None) -> None:
        self._executor = executor or SubprocessCommandExecutor()

    async def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        policy: SandboxPolicy,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        artifact_store: ArtifactStore | None = None,
        task_id: str | None = None,
        work_item_id: str | None = None,
        gate_id: str | None = None,
        gate_name: str | None = None,
        policy_name: str | None = None,
    ) -> GateDecision:
        validation = policy.validate_command(argv, cwd=cwd)
        if not validation.passed:
            return validation

        filtered_env = policy.env.filter(env)
        result = await self._executor.execute(
            validation.command,
            cwd=Path(validation.cwd),
            env=filtered_env,
            timeout_seconds=policy.timeout_seconds,
        )
        summary = policy.summarize_output(result.stdout, result.stderr or result.error, env=env)
        command_display = policy.env.redact(" ".join(validation.command), env=env)
        redacted_stdout = policy.env.redact(result.stdout, env=env)
        redacted_stderr = policy.env.redact(result.stderr, env=env)
        redacted_error = policy.env.redact(result.error, env=env)
        if result.timed_out:
            status: DecisionStatus = "timeout"
            passed = False
        elif result.error:
            status = "error"
            passed = False
        else:
            status = "passed" if result.returncode == 0 else "failed"
            passed = result.returncode == 0

        evidence = (
            *validation.evidence,
            GateEvidence("returncode", "" if result.returncode is None else str(result.returncode)),
            GateEvidence("duration_seconds", f"{result.duration_seconds:.3f}"),
            GateEvidence("summary", summary),
            GateEvidence("env_keys", ",".join(sorted(filtered_env))),
            GateEvidence("timed_out", str(result.timed_out).lower()),
        )
        decision = GateDecision(
            name="sandbox-command",
            passed=passed,
            status=status,
            command=validation.command,
            workspace_root=validation.workspace_root,
            cwd=validation.cwd,
            evidence=evidence,
        )
        if artifact_store is not None and (task_id is not None or work_item_id is not None):
            ensure(
                not (task_id is not None and work_item_id is not None),
                "Sandbox artifacts must belong to exactly one task or work item",
            )
            artifact = artifact_store.put_json(
                kind=ArtifactKind.SANDBOX_EXECUTION,
                name=f"{gate_name or validation.command[0]} sandbox execution",
                value=build_execution_payload(
                    gate_id=gate_id,
                    gate_name=gate_name,
                    policy_name=policy_name,
                    decision=decision,
                    command_display=command_display,
                    result=result,
                    summary=summary,
                    stdout=redacted_stdout,
                    stderr=redacted_stderr,
                    error=redacted_error,
                    env_keys=tuple(sorted(filtered_env)),
                ),
                task_id=task_id,
                work_item_id=work_item_id,
                metadata={
                    "gate_id": gate_id or "",
                    "gate_name": gate_name or "",
                    "policy_name": policy_name or "",
                },
            )
            decision = GateDecision(
                name=decision.name,
                passed=decision.passed,
                status=decision.status,
                command=decision.command,
                workspace_root=decision.workspace_root,
                cwd=decision.cwd,
                evidence=(
                    *decision.evidence,
                    GateEvidence("artifact_id", artifact.id),
                    GateEvidence("artifact_kind", artifact.kind.value),
                ),
            )
        ensure(bool(decision.evidence), "Sandbox command decisions must include evidence")
        return decision
