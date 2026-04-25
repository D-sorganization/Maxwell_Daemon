"""Policy models for safe validation sandbox commands.

The policy layer is intentionally pure: it validates workspace paths, command
allow/deny rules, environment exposure, and redacted summaries without spawning
processes. Execution adapters live in :mod:`maxwell_daemon.sandbox.runner`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from maxwell_daemon.contracts import ensure, require

DecisionStatus = Literal["passed", "failed", "timeout", "policy_denied", "path_denied", "error"]

_DEFAULT_DENIED_COMMANDS = frozenset(
    {
        "cmd",
        "del",
        "erase",
        "format",
        "mkfs",
        "powershell",
        "pwsh",
        "rd",
        "rm",
        "rmdir",
        "sh",
        "shutdown",
    }
)
_DEFAULT_SECRET_KEY_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASS",
    "API_KEY",
    "PRIVATE_KEY",
)


@dataclass(slots=True, frozen=True)
class GateEvidence:
    """One structured evidence item attached to a gate-like decision."""

    name: str
    value: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}


@dataclass(slots=True, frozen=True)
class GateDecision:
    """Structured command validation or execution decision."""

    name: str
    passed: bool
    status: DecisionStatus
    command: tuple[str, ...]
    workspace_root: str
    cwd: str
    evidence: tuple[GateEvidence, ...] = ()

    def evidence_value(self, name: str) -> str | None:
        for item in self.evidence:
            if item.name == name:
                return item.value
        return None

    def to_dict(self, *, command_display: str | None = None) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "status": self.status,
            "command": (command_display if command_display is not None else list(self.command)),
            "workspace_root": self.workspace_root,
            "cwd": self.cwd,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(slots=True, frozen=True)
class WorkspacePolicy:
    """Allowed root for validation runs."""

    root: Path

    def __post_init__(self) -> None:
        resolved = self.root.expanduser().resolve()
        require(resolved.is_dir(), f"Sandbox workspace root must be a directory: {resolved}")
        object.__setattr__(self, "root", resolved)

    def resolve_inside(self, candidate: Path | str | None) -> Path | None:
        """Return a resolved path if it stays under the root, otherwise ``None``."""

        raw = self.root if candidate is None else Path(candidate)
        if not raw.is_absolute():
            raw = self.root / raw
        resolved = raw.expanduser().resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return None
        return resolved


@dataclass(slots=True, frozen=True)
class CommandPolicy:
    """Allow/deny rules for argv-list commands."""

    allowed_commands: frozenset[str] = frozenset()
    denied_commands: frozenset[str] = _DEFAULT_DENIED_COMMANDS
    destructive_tokens: frozenset[str] = frozenset(
        {
            "--force",
            "/f",
            "/s",
            "clean",
            "reset",
        }
    )

    def validate(self, argv: tuple[str, ...]) -> tuple[bool, str]:
        require(len(argv) > 0, "Sandbox command must be non-empty")
        executable = Path(argv[0]).name.lower()
        if executable in {cmd.lower() for cmd in self.denied_commands}:
            return False, f"command denied by sandbox policy: {executable}"
        if self.allowed_commands and executable not in {
            cmd.lower() for cmd in self.allowed_commands
        }:
            return False, f"command is not allowlisted by sandbox policy: {executable}"
        lowered_args = {arg.lower() for arg in argv[1:]}
        destructive = lowered_args.intersection(
            {token.lower() for token in self.destructive_tokens}
        )
        if destructive:
            return (
                False,
                f"destructive argument denied by sandbox policy: {sorted(destructive)[0]}",
            )
        return True, "command allowed"


@dataclass(slots=True, frozen=True)
class EnvPolicy:
    """Least-privilege env filtering and redaction."""

    allowlist: frozenset[str] = frozenset()
    secret_keys: frozenset[str] = frozenset()
    secret_values: frozenset[str] = frozenset()
    redaction: str = "[REDACTED]"

    def filter(self, source: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if source is None else source)
        if not self.allowlist:
            return {}
        return {key: env[key] for key in self.allowlist if key in env}

    def redact(self, text: str, *, env: dict[str, str] | None = None) -> str:
        redacted = text
        env_values = dict(os.environ if env is None else env)
        secret_keys = set(self.secret_keys)
        secret_keys.update(
            key
            for key in env_values
            if any(marker in key.upper() for marker in _DEFAULT_SECRET_KEY_MARKERS)
        )
        secret_values = set(self.secret_values)
        secret_values.update(env_values[key] for key in secret_keys if env_values.get(key))
        for value in sorted(secret_values, key=len, reverse=True):
            if value:
                redacted = redacted.replace(value, self.redaction)
        return redacted


@dataclass(slots=True, frozen=True)
class SandboxPolicy:
    """Composable safe defaults for validation command policy."""

    workspace: WorkspacePolicy
    command: CommandPolicy = field(default_factory=CommandPolicy)
    env: EnvPolicy = field(default_factory=EnvPolicy)
    timeout_seconds: float = 300.0
    output_summary_bytes: int = 8192
    network_enabled: bool = False
    allow_gpu: bool = False

    def __post_init__(self) -> None:
        require(self.timeout_seconds > 0, "Sandbox timeout must be positive")
        require(
            self.output_summary_bytes > 0,
            "Sandbox output summary limit must be positive",
        )

    @classmethod
    def for_workspace(
        cls,
        root: Path,
        *,
        allowed_commands: set[str] | frozenset[str] = frozenset(),
        denied_commands: set[str] | frozenset[str] = _DEFAULT_DENIED_COMMANDS,
        env_allowlist: set[str] | frozenset[str] = frozenset(),
        secret_env_keys: set[str] | frozenset[str] = frozenset(),
        timeout_seconds: float = 300.0,
        output_summary_bytes: int = 8192,
        network_enabled: bool = False,
        allow_gpu: bool = False,
    ) -> SandboxPolicy:
        return cls(
            workspace=WorkspacePolicy(root),
            command=CommandPolicy(
                allowed_commands=frozenset(cmd.lower() for cmd in allowed_commands),
                denied_commands=frozenset(cmd.lower() for cmd in denied_commands),
            ),
            env=EnvPolicy(
                allowlist=frozenset(env_allowlist),
                secret_keys=frozenset(secret_env_keys),
            ),
            timeout_seconds=timeout_seconds,
            output_summary_bytes=output_summary_bytes,
            network_enabled=network_enabled,
            allow_gpu=allow_gpu,
        )

    def validate_command(
        self, argv: list[str] | tuple[str, ...], *, cwd: Path | str | None = None
    ) -> GateDecision:
        command = tuple(argv)
        if not command:
            return self._deny("policy_denied", command, cwd, "command must be non-empty")

        resolved_cwd = self.workspace.resolve_inside(cwd)
        if resolved_cwd is None:
            return self._deny(
                "path_denied",
                command,
                cwd,
                f"cwd escapes sandbox workspace: {cwd}",
            )

        allowed, reason = self.command.validate(command)
        if not allowed:
            return GateDecision(
                name="sandbox-command-policy",
                passed=False,
                status="policy_denied",
                command=command,
                workspace_root=str(self.workspace.root),
                cwd=str(resolved_cwd),
                evidence=self._policy_evidence(reason),
            )

        decision = GateDecision(
            name="sandbox-command-policy",
            passed=True,
            status="passed",
            command=command,
            workspace_root=str(self.workspace.root),
            cwd=str(resolved_cwd),
            evidence=(
                GateEvidence("reason", reason),
                GateEvidence("timeout_seconds", f"{self.timeout_seconds:g}"),
                GateEvidence("output_summary_bytes", str(self.output_summary_bytes)),
                GateEvidence("network_enabled", str(self.network_enabled).lower()),
                GateEvidence("allow_gpu", str(self.allow_gpu).lower()),
            ),
        )
        ensure(
            bool(decision.evidence),
            "Sandbox validation decisions must include evidence",
        )
        return decision

    def summarize_output(
        self, stdout: str, stderr: str, *, env: dict[str, str] | None = None
    ) -> str:
        merged = "\n".join(part for part in (stdout, stderr) if part)
        redacted = self.env.redact(merged, env=env)
        encoded = redacted.encode()
        if len(encoded) <= self.output_summary_bytes:
            return redacted
        tail = encoded[-self.output_summary_bytes :].decode(errors="replace")
        return "... truncated ...\n" + tail

    def _deny(
        self,
        status: Literal["policy_denied", "path_denied"],
        command: tuple[str, ...],
        cwd: Path | str | None,
        reason: str,
    ) -> GateDecision:
        safe_cwd = self.workspace.resolve_inside(cwd)
        return GateDecision(
            name="sandbox-command-policy",
            passed=False,
            status=status,
            command=command,
            workspace_root=str(self.workspace.root),
            cwd=str(safe_cwd or cwd or self.workspace.root),
            evidence=self._policy_evidence(reason),
        )

    def _policy_evidence(self, reason: str) -> tuple[GateEvidence, ...]:
        return (
            GateEvidence("reason", reason),
            GateEvidence("timeout_seconds", f"{self.timeout_seconds:g}"),
            GateEvidence("output_summary_bytes", str(self.output_summary_bytes)),
            GateEvidence("network_enabled", str(self.network_enabled).lower()),
            GateEvidence("allow_gpu", str(self.allow_gpu).lower()),
        )
