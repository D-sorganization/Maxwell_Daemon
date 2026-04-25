"""Gate adapter for sandbox validation commands."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from maxwell_daemon.core.artifacts import ArtifactStore
from maxwell_daemon.core.gates import GateAdapterResult, GateDefinition
from maxwell_daemon.sandbox.policy import EnvPolicy, SandboxPolicy
from maxwell_daemon.sandbox.runner import (
    CommandExecutor,
    SandboxCommandRunner,
    SandboxRunResult,
    SubprocessCommandExecutor,
)

__all__ = ["SandboxGateAdapter"]

_POLICY_KEY = "sandbox.policy"
_WORKSPACE_KEY = "sandbox.workspace_root"
_CWD_KEY = "sandbox.cwd"
_COMMAND_KEY = "sandbox.command"
_ENV_KEY = "sandbox.env"
_TIMEOUT_KEY = "sandbox.timeout_seconds"
_OUTPUT_LIMIT_KEY = "sandbox.output_summary_bytes"
_NETWORK_KEY = "sandbox.network_enabled"
_GPU_KEY = "sandbox.allow_gpu"
_TASK_ID_KEY = "sandbox.task_id"
_WORK_ITEM_ID_KEY = "sandbox.work_item_id"

_PRESET_COMMANDS: dict[str, tuple[str, ...]] = {
    "unit-tests": ("python", "-m", "pytest"),
    "lint": ("ruff", "check", "."),
    "typecheck": ("mypy", "."),
}


@dataclass(slots=True)
class _CapturingExecutor(CommandExecutor):
    inner: CommandExecutor
    last_result: SandboxRunResult | None = None

    async def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> SandboxRunResult:
        result = await self.inner.execute(
            argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )
        self.last_result = result
        return result


class SandboxGateAdapter:
    """Execute sandbox gate definitions through the sandbox runner."""

    def __init__(
        self,
        *,
        executor: CommandExecutor | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._capture = _CapturingExecutor(executor or SubprocessCommandExecutor())
        self._runner = SandboxCommandRunner(executor=self._capture)
        self._artifact_store = artifact_store

    async def run(self, gate: GateDefinition) -> GateAdapterResult:
        policy_name = self._read_metadata(gate.metadata, _POLICY_KEY)
        workspace_root = self._read_metadata(gate.metadata, _WORKSPACE_KEY)
        if policy_name is None:
            return self._failure(
                "missing sandbox policy metadata",
                (
                    self._evidence("gate", gate.gate_id),
                    self._evidence("reason", "missing sandbox.policy metadata"),
                ),
            )
        if workspace_root is None:
            return self._failure(
                "missing sandbox workspace metadata",
                (
                    self._evidence("gate", gate.gate_id),
                    self._evidence("policy", policy_name),
                    self._evidence("reason", "missing sandbox.workspace_root metadata"),
                ),
            )

        command = self._resolve_command(gate.metadata, policy_name)
        if command is None:
            return self._failure(
                "missing sandbox command metadata",
                (
                    self._evidence("gate", gate.gate_id),
                    self._evidence("policy", policy_name),
                    self._evidence("workspace_root", workspace_root),
                    self._evidence(
                        "reason", f"missing command metadata for {policy_name}"
                    ),
                ),
            )

        env = self._parse_env(gate.metadata.get(_ENV_KEY))
        timeout_seconds = self._parse_float(
            gate.metadata.get(_TIMEOUT_KEY), default=300.0
        )
        output_summary_bytes = self._parse_int(
            gate.metadata.get(_OUTPUT_LIMIT_KEY), default=8192
        )
        network_enabled = self._parse_bool(
            gate.metadata.get(_NETWORK_KEY), default=False
        )
        allow_gpu = self._parse_bool(gate.metadata.get(_GPU_KEY), default=False)
        cwd = self._read_metadata(gate.metadata, _CWD_KEY)
        task_id = self._read_metadata(gate.metadata, _TASK_ID_KEY)
        work_item_id = self._read_metadata(gate.metadata, _WORK_ITEM_ID_KEY)

        try:
            policy = SandboxPolicy.for_workspace(
                Path(workspace_root),
                timeout_seconds=timeout_seconds,
                output_summary_bytes=output_summary_bytes,
                env_allowlist=set(env),
                network_enabled=network_enabled,
                allow_gpu=allow_gpu,
            )
        except Exception as exc:
            return self._failure(
                "invalid sandbox policy",
                (
                    self._evidence("gate", gate.gate_id),
                    self._evidence("policy", policy_name),
                    self._evidence("workspace_root", workspace_root),
                    self._evidence("reason", self._redact_text(str(exc), env)),
                ),
            )

        try:
            self._capture.last_result = None
            decision = await self._runner.run(
                command,
                policy=policy,
                cwd=cwd,
                env=env,
                artifact_store=self._artifact_store,
                task_id=task_id,
                work_item_id=work_item_id,
                gate_id=gate.gate_id,
                gate_name=gate.name,
                policy_name=policy_name,
            )
        except Exception as exc:
            return self._failure(
                "sandbox command execution error",
                (
                    self._evidence("gate", gate.gate_id),
                    self._evidence("policy", policy_name),
                    self._evidence("workspace_root", workspace_root),
                    self._evidence("command", self._render_command(command, env)),
                    self._evidence("reason", self._redact_text(str(exc), env)),
                ),
            )

        evidence = [
            self._evidence("gate", gate.gate_id),
            self._evidence("policy", policy_name),
            self._evidence("workspace_root", decision.workspace_root),
            self._evidence("cwd", decision.cwd),
            self._evidence("command", self._render_command(decision.command, env)),
            self._evidence("status", decision.status),
            self._evidence("passed", str(decision.passed).lower()),
        ]
        evidence.extend(self._format_decision_evidence(decision.evidence, env))

        capture = self._capture.last_result
        if capture is not None:
            evidence.append(
                self._evidence(
                    "stdout", self._summarize_text(capture.stdout, policy, env)
                )
            )
            evidence.append(
                self._evidence(
                    "stderr", self._summarize_text(capture.stderr, policy, env)
                )
            )
            if capture.error:
                evidence.append(
                    self._evidence("error", self._redact_text(capture.error, env))
                )
            evidence.append(self._evidence("timed_out", str(capture.timed_out).lower()))
            if capture.returncode is not None:
                evidence.append(self._evidence("returncode", str(capture.returncode)))

        message = self._default_message(policy_name, decision.status)
        return GateAdapterResult(
            passed=decision.passed, evidence=tuple(evidence), message=message
        )

    @staticmethod
    def _read_metadata(metadata: Mapping[str, str], key: str) -> str | None:
        value = metadata.get(key)
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _parse_float(value: str | None, *, default: float) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except ValueError:
            return default

    @staticmethod
    def _parse_int(value: str | None, *, default: int) -> int:
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    @staticmethod
    def _parse_bool(value: str | None, *, default: bool) -> bool:
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    def _resolve_command(
        self, metadata: Mapping[str, str], policy_name: str
    ) -> tuple[str, ...] | None:
        command_value = self._read_metadata(metadata, _COMMAND_KEY)
        if command_value is not None:
            parsed = self._parse_command(command_value)
            return parsed
        return _PRESET_COMMANDS.get(policy_name.lower())

    def _parse_command(self, raw: str) -> tuple[str, ...] | None:
        if not raw:
            return None
        if raw.lstrip().startswith("["):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return None
            if not isinstance(decoded, list) or not decoded:
                return None
            command: list[str] = []
            for item in decoded:
                if not isinstance(item, str):
                    return None
                stripped = item.strip()
                if not stripped:
                    return None
                command.append(stripped)
            return tuple(command)
        return tuple(part for part in shlex.split(raw) if part)

    def _parse_env(self, raw: str | None) -> dict[str, str]:
        if raw is None or not raw.strip():
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(decoded, dict):
            return {}
        env: dict[str, str] = {}
        for key, value in decoded.items():
            if isinstance(key, str) and isinstance(value, str):
                env[key] = value
        return env

    def _format_decision_evidence(
        self, evidence: Sequence[object], env: Mapping[str, str]
    ) -> list[str]:
        formatted: list[str] = []
        for item in evidence:
            if hasattr(item, "name") and hasattr(item, "value"):
                evidence_item = cast(Any, item)
                name = str(evidence_item.name)
                value = self._redact_text(str(evidence_item.value), env)
                formatted.append(self._evidence(name, value))
        return formatted

    def _summarize_text(
        self, text: str, policy: SandboxPolicy, env: Mapping[str, str]
    ) -> str:
        redacted = self._redact_text(text, env)
        encoded = redacted.encode()
        if len(encoded) <= policy.output_summary_bytes:
            return redacted
        tail = encoded[-policy.output_summary_bytes :].decode(errors="replace")
        return "... truncated ...\n" + tail

    def _render_command(self, command: Sequence[str], env: Mapping[str, str]) -> str:
        rendered = " ".join(command)
        return self._redact_text(rendered, env)

    def _redact_text(self, text: str, env: Mapping[str, str]) -> str:
        if not text:
            return text
        return EnvPolicy().redact(text, env=dict(env))

    @staticmethod
    def _evidence(name: str, value: str) -> str:
        return f"{name}={value}"

    @staticmethod
    def _default_message(policy_name: str, status: str) -> str:
        if status == "passed":
            return f"{policy_name} sandbox gate passed"
        return f"{policy_name} sandbox gate {status}"

    def _failure(self, message: str, evidence: Sequence[str]) -> GateAdapterResult:
        return GateAdapterResult(
            passed=False, evidence=tuple(evidence), message=message
        )
