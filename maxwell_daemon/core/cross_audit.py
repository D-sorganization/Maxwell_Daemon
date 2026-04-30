"""Parallel cross-audit orchestration over roles and backends."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from maxwell_daemon.backends.base import BackendResponse, TokenUsage
from maxwell_daemon.core.roles import Job, Role, RolePlayer
from maxwell_daemon.core.router import BackendRouter

DEFAULT_CROSS_AUDIT_ROLES: dict[str, Role] = {
    "architect": Role(
        name="Architect",
        system_prompt=(
            "You are a senior software architect. Audit the task for design risks, "
            "contract violations, missing abstractions, and integration hazards."
        ),
    ),
    "security": Role(
        name="Security",
        system_prompt=(
            "You are a security reviewer. Audit the task for unsafe inputs, secret "
            "handling, authz/authn gaps, dependency risk, and data exposure."
        ),
    ),
    "validator": Role(
        name="Validator",
        system_prompt=(
            "You are Maxwell Crucible, an adversarial QA validator. Cross-audit the "
            "task for correctness gaps, missing tests, regressions, and edge cases."
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class CrossAuditTarget:
    """One role/backend/model assignment within a cross-audit run."""

    role: Role
    backend_name: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class CrossAuditResult:
    """Result for a single role/backend assignment."""

    role_name: str
    backend_name: str
    model: str
    content: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class CrossAuditReport:
    """Aggregated cross-audit results plus a deterministic summary."""

    prompt: str
    results: list[CrossAuditResult]
    summary: str

    @property
    def succeeded(self) -> bool:
        return any(result.succeeded for result in self.results)

    @property
    def total_usage(self) -> TokenUsage:
        total = TokenUsage()
        for result in self.results:
            total += result.usage
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "summary": self.summary,
            "usage": {
                "prompt_tokens": self.total_usage.prompt_tokens,
                "completion_tokens": self.total_usage.completion_tokens,
                "total_tokens": self.total_usage.total_tokens,
                "cached_tokens": self.total_usage.cached_tokens,
            },
            "results": [
                {
                    "role": result.role_name,
                    "backend": result.backend_name,
                    "model": result.model,
                    "content": result.content,
                    "finish_reason": result.finish_reason,
                    "error": result.error,
                    "usage": {
                        "prompt_tokens": result.usage.prompt_tokens,
                        "completion_tokens": result.usage.completion_tokens,
                        "total_tokens": result.usage.total_tokens,
                        "cached_tokens": result.usage.cached_tokens,
                    },
                }
                for result in self.results
            ],
        }


class CrossAuditService:
    """Runs one prompt across a bounded role/backend matrix."""

    def __init__(self, router: BackendRouter) -> None:
        self._router = router

    async def run(
        self,
        prompt: str,
        *,
        targets: list[CrossAuditTarget] | None = None,
        roles: list[Role] | None = None,
        backend_names: list[str] | None = None,
        repo: str | None = None,
    ) -> CrossAuditReport:
        """Run the same prompt across each target and summarize the responses."""
        audit_targets = (
            targets
            if targets is not None
            else self._build_targets(
                roles=roles,
                backend_names=backend_names,
            )
        )
        if not audit_targets:
            raise ValueError("cross-audit requires at least one target")

        results = await asyncio.gather(
            *(self._run_target(prompt, target, repo=repo) for target in audit_targets)
        )
        return CrossAuditReport(
            prompt=prompt,
            results=list(results),
            summary=self._summarize(results),
        )

    def _build_targets(
        self,
        *,
        roles: list[Role] | None,
        backend_names: list[str] | None,
    ) -> list[CrossAuditTarget]:
        selected_roles = roles or [DEFAULT_CROSS_AUDIT_ROLES["validator"]]
        selected_backends = backend_names or self._router.available_backends()
        return [
            CrossAuditTarget(role=role, backend_name=backend_name)
            for role in selected_roles
            for backend_name in selected_backends
        ]

    async def _run_target(
        self,
        prompt: str,
        target: CrossAuditTarget,
        *,
        repo: str | None,
    ) -> CrossAuditResult:
        backend_label = target.backend_name or "(routed)"
        try:
            decision = self._router.route(
                repo=repo,
                backend_override=target.backend_name,
                model_override=target.model,
            )
            player = RolePlayer(
                role=target.role,
                backend=decision.backend,
                model=decision.model,
            )
            response = await player.execute(Job(instructions=prompt))
            return self._success_result(target.role.name, decision.backend_name, response)
        except Exception as exc:  # noqa: BLE001
            return CrossAuditResult(
                role_name=target.role.name,
                backend_name=backend_label,
                model=target.model or "",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _success_result(
        self,
        role_name: str,
        backend_name: str,
        response: BackendResponse,
    ) -> CrossAuditResult:
        return CrossAuditResult(
            role_name=role_name,
            backend_name=backend_name,
            model=response.model,
            content=response.content,
            usage=response.usage,
            finish_reason=response.finish_reason,
        )

    def _summarize(self, results: list[CrossAuditResult]) -> str:
        successes = [result for result in results if result.succeeded]
        failures = [result for result in results if not result.succeeded]
        lines = [
            f"Cross-audit completed: {len(successes)} succeeded, {len(failures)} failed.",
        ]
        if successes:
            unique_outputs = {self._normalized_content(result.content) for result in successes}
            agreement = "agreement" if len(unique_outputs) == 1 else "divergence"
            lines.append(f"Output comparison: {agreement} across {len(unique_outputs)} view(s).")
            lines.extend(
                f"- {result.role_name} via {result.backend_name}/{result.model}: "
                f"{self._preview(result.content)}"
                for result in successes
            )
        if failures:
            lines.extend(
                f"- {result.role_name} via {result.backend_name}: {result.error}"
                for result in failures
            )
        return "\n".join(lines)

    def _normalized_content(self, content: str) -> str:
        return " ".join(content.split()).strip().lower()

    def _preview(self, content: str, limit: int = 180) -> str:
        normalized = " ".join(content.split()).strip()
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 1]}..."
