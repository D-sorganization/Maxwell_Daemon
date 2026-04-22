"""Structured sandbox execution artifact serialization."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from maxwell_daemon.sandbox.policy import GateDecision

if TYPE_CHECKING:
    from maxwell_daemon.sandbox.runner import SandboxRunResult


def serialize_gate_decision(decision: GateDecision, *, command_display: str) -> dict[str, Any]:
    return decision.to_dict(command_display=command_display)


def build_execution_payload(
    *,
    gate_id: str | None,
    gate_name: str | None,
    policy_name: str | None,
    decision: GateDecision,
    command_display: str,
    result: SandboxRunResult,
    summary: str,
    stdout: str,
    stderr: str,
    error: str,
    env_keys: Sequence[str],
) -> dict[str, Any]:
    return {
        "gate": {
            "id": gate_id,
            "name": gate_name,
            "policy": policy_name,
            "decision": serialize_gate_decision(decision, command_display=command_display),
        },
        "execution": {
            "returncode": result.returncode,
            "duration_seconds": result.duration_seconds,
            "timed_out": result.timed_out,
            "summary": summary,
            "stdout": stdout,
            "stderr": stderr,
            "error": error,
            "env_keys": list(env_keys),
        },
    }
