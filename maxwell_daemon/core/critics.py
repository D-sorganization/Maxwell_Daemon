"""Critic panel primitives for adversarial review gates.

The critic panel mirrors the gate runtime style: explicit profiles, narrow
adapters, deterministic aggregation, and fail-closed handling for execution
issues.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

from maxwell_daemon.contracts import require
from maxwell_daemon.core.gates import GateAdapterResult, GateDefinition

__all__ = [
    "CriticAdapter",
    "CriticAggregatePolicy",
    "CriticFinding",
    "CriticPanelGateAdapter",
    "CriticPanelRun",
    "CriticPanelRunner",
    "CriticProfile",
    "CriticVerdict",
    "StaticCritic",
    "critic_profile_by_id",
    "default_critic_profiles",
]

CriticSeverity = Literal["p0", "p1", "p2", "p3", "note"]
CriticRunStatus = Literal["passed", "failed", "timed_out", "missing", "error"]
_CRITIC_SEVERITY_ORDER: Mapping[CriticSeverity, int] = {
    "p0": 0,
    "p1": 1,
    "p2": 2,
    "p3": 3,
    "note": 4,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _finding_sort_key(finding: CriticFinding) -> tuple[object, ...]:
    return (
        finding.critic_id,
        _CRITIC_SEVERITY_ORDER[finding.severity],
        finding.file_path or "",
        finding.line_number or 0,
        finding.summary,
        finding.detail,
        finding.evidence,
    )


@dataclass(slots=True, frozen=True)
class CriticProfile:
    """Immutable critic contract used by the panel runner."""

    critic_id: str
    name: str
    adapter: str
    title: str | None = None
    scope: str = "general"
    required_inputs: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    output_schema_version: str = "critic.v1"
    minimum_model_capability_tags: tuple[str, ...] = ()
    default_severity_mapping: Mapping[str, CriticSeverity] = field(default_factory=dict)
    required: bool = True
    timeout_seconds: float | None = None
    retry_limit: int = 0
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require(bool(self.critic_id.strip()), "CriticProfile.critic_id must be non-empty")
        require(bool(self.name.strip()), "CriticProfile.name must be non-empty")
        require(bool(self.adapter.strip()), "CriticProfile.adapter must be non-empty")
        title = self.title or self.name
        require(bool(title.strip()), "CriticProfile.title must be non-empty")
        object.__setattr__(self, "title", title)
        require(bool(self.scope.strip()), "CriticProfile.scope must be non-empty")
        require(
            bool(self.output_schema_version.strip()),
            "CriticProfile.output_schema_version must be non-empty",
        )
        if self.timeout_seconds is not None:
            require(
                self.timeout_seconds > 0,
                "CriticProfile.timeout_seconds must be positive",
            )
        require(self.retry_limit >= 0, "CriticProfile.retry_limit must be non-negative")
        for field_name, values in (
            ("required_inputs", self.required_inputs),
            ("forbidden_actions", self.forbidden_actions),
            ("minimum_model_capability_tags", self.minimum_model_capability_tags),
        ):
            for value in values:
                require(
                    isinstance(value, str) and bool(value.strip()),
                    f"CriticProfile.{field_name} values must be non-empty strings",
                )
        for key, value in self.default_severity_mapping.items():
            require(
                isinstance(key, str) and bool(key.strip()),
                "CriticProfile.default_severity_mapping keys must be non-empty strings",
            )
            require(
                value in _CRITIC_SEVERITY_ORDER,
                "CriticProfile.default_severity_mapping values must be valid severities",
            )
        for key, value in self.metadata.items():
            require(
                isinstance(key, str) and bool(key.strip()),
                "CriticProfile.metadata keys must be non-empty strings",
            )
            require(isinstance(value, str), "CriticProfile.metadata values must be strings")


@dataclass(slots=True, frozen=True)
class CriticFinding:
    """One review finding emitted by a critic."""

    critic_id: str
    severity: CriticSeverity
    summary: str
    detail: str = ""
    file_path: str | None = None
    line_number: int | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require(bool(self.critic_id.strip()), "CriticFinding.critic_id must be non-empty")
        require(bool(self.summary.strip()), "CriticFinding.summary must be non-empty")
        require(
            self.severity in _CRITIC_SEVERITY_ORDER,
            "CriticFinding.severity must be one of p0, p1, p2, p3, or note",
        )
        if self.file_path is not None:
            require(
                bool(self.file_path.strip()),
                "CriticFinding.file_path must be non-empty when provided",
            )
        if self.line_number is not None:
            require(
                self.line_number > 0,
                "CriticFinding.line_number must be positive when provided",
            )
        for item in self.evidence:
            require(
                isinstance(item, str) and bool(item.strip()),
                "CriticFinding.evidence items must be non-empty strings",
            )

    @property
    def is_blocking(self) -> bool:
        return self.severity in ("p0", "p1")


@dataclass(slots=True, frozen=True)
class CriticPanelRun:
    """Execution record for one critic."""

    profile: CriticProfile
    status: CriticRunStatus
    findings: tuple[CriticFinding, ...] = ()
    message: str = ""
    started_at: datetime = field(default_factory=_utc_now)
    finished_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        require(
            self.status in ("passed", "failed", "timed_out", "missing", "error"),
            "CriticPanelRun.status must be valid",
        )
        require(
            self.finished_at >= self.started_at,
            "CriticPanelRun.finished_at must not precede started_at",
        )
        if self.status == "passed":
            require(
                not any(finding.is_blocking for finding in self.findings),
                "CriticPanelRun status passed cannot include blocking findings",
            )

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(slots=True, frozen=True)
class CriticVerdict:
    """Deterministic aggregate verdict for a critic panel run."""

    passed: bool
    runs: tuple[CriticPanelRun, ...] = ()
    findings: tuple[CriticFinding, ...] = ()
    timed_out_critic_ids: tuple[str, ...] = ()
    missing_required_critic_ids: tuple[str, ...] = ()
    errored_critic_ids: tuple[str, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        ordered_runs = tuple(sorted(self.runs, key=lambda run: run.profile.critic_id))
        ordered_findings = tuple(sorted(self.findings, key=_finding_sort_key))
        object.__setattr__(self, "runs", ordered_runs)
        object.__setattr__(self, "findings", ordered_findings)
        blocking_findings = tuple(finding for finding in ordered_findings if finding.is_blocking)
        require(
            self.passed == (not blocking_findings),
            "CriticVerdict.passed must match blocking findings",
        )
        for critic_ids, label in (
            (self.timed_out_critic_ids, "timed_out_critic_ids"),
            (self.missing_required_critic_ids, "missing_required_critic_ids"),
            (self.errored_critic_ids, "errored_critic_ids"),
        ):
            require(
                len(set(critic_ids)) == len(tuple(critic_ids)),
                f"CriticVerdict.{label} must not contain duplicates",
            )

    @property
    def blocking_findings(self) -> tuple[CriticFinding, ...]:
        return tuple(finding for finding in self.findings if finding.is_blocking)

    @property
    def nonblocking_findings(self) -> tuple[CriticFinding, ...]:
        return tuple(finding for finding in self.findings if not finding.is_blocking)


class CriticAdapter(Protocol):
    """Execution protocol for one critic family."""

    async def run(self, profile: CriticProfile) -> CriticPanelRun: ...


@dataclass(slots=True, frozen=True)
class CriticAggregatePolicy:
    """Policy for converting panel runs into a verdict."""

    blocking_severities: tuple[CriticSeverity, ...] = ("p0", "p1")
    optional_issue_is_blocking: bool = False

    def aggregate(self, runs: Sequence[CriticPanelRun]) -> CriticVerdict:
        require(bool(runs), "CriticAggregatePolicy.aggregate: runs must not be empty")
        ordered_runs = tuple(sorted(runs, key=lambda run: run.profile.critic_id))
        findings: list[CriticFinding] = []
        timed_out_ids: list[str] = []
        missing_ids: list[str] = []
        errored_ids: list[str] = []

        for run in ordered_runs:
            findings.extend(run.findings)
            if run.status == "timed_out":
                timed_out_ids.append(run.profile.critic_id)
                findings.append(self._execution_finding(run, "timed out"))
            elif run.status == "missing":
                if run.profile.required:
                    missing_ids.append(run.profile.critic_id)
                findings.append(self._execution_finding(run, "critic missing"))
            elif run.status == "error":
                if run.profile.required:
                    errored_ids.append(run.profile.critic_id)
                findings.append(self._execution_finding(run, run.message or "critic error"))

        ordered_findings = tuple(sorted(findings, key=_finding_sort_key))
        blocking_findings = tuple(
            finding for finding in ordered_findings if finding.severity in self.blocking_severities
        )
        passed = not blocking_findings
        return CriticVerdict(
            passed=passed,
            runs=ordered_runs,
            findings=ordered_findings,
            timed_out_critic_ids=tuple(timed_out_ids),
            missing_required_critic_ids=tuple(missing_ids),
            errored_critic_ids=tuple(errored_ids),
        )

    def _execution_finding(self, run: CriticPanelRun, detail: str) -> CriticFinding:
        blocking = run.profile.required or self.optional_issue_is_blocking
        severity: CriticSeverity = "p1" if blocking else "p2"
        evidence = (run.message,) if run.message else ()
        return CriticFinding(
            critic_id=run.profile.critic_id,
            severity=severity,
            summary=detail.title(),
            detail=detail,
            evidence=evidence,
        )


class CriticPanelRunner:
    """Run critic adapters concurrently and aggregate their outputs."""

    def __init__(
        self,
        *,
        adapters: Mapping[str, CriticAdapter],
        policy: CriticAggregatePolicy | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._policy = policy or CriticAggregatePolicy()

    def as_gate_adapter(self, profiles: Sequence[CriticProfile]) -> CriticPanelGateAdapter:
        return CriticPanelGateAdapter(runner=self, profiles=tuple(profiles))

    async def run(self, profiles: Sequence[CriticProfile]) -> CriticVerdict:
        require(bool(profiles), "CriticPanelRunner.run: profiles must not be empty")
        seen: set[str] = set()
        for profile in profiles:
            require(
                profile.critic_id not in seen,
                f"duplicate critic_id {profile.critic_id!r}",
            )
            seen.add(profile.critic_id)

        runs = await asyncio.gather(*(self._run_one(profile) for profile in profiles))
        return self._policy.aggregate(runs)

    async def _run_one(self, profile: CriticProfile) -> CriticPanelRun:
        adapter = self._adapters.get(profile.adapter)
        started_at = _utc_now()
        if adapter is None:
            return CriticPanelRun(
                profile=profile,
                status="missing",
                message=f"no adapter registered for critic {profile.critic_id!r}",
                started_at=started_at,
                finished_at=_utc_now(),
            )

        try:
            if profile.timeout_seconds is None:
                result = await adapter.run(profile)
            else:
                result = await asyncio.wait_for(
                    adapter.run(profile), timeout=profile.timeout_seconds
                )
        except (TimeoutError, asyncio.TimeoutError):
            return CriticPanelRun(
                profile=profile,
                status="timed_out",
                message=(
                    f"critic {profile.critic_id!r} timed out after "
                    f"{profile.timeout_seconds} seconds"
                ),
                started_at=started_at,
                finished_at=_utc_now(),
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed wrapper; pragma: no cover
            return CriticPanelRun(
                profile=profile,
                status="error",
                message=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
                finished_at=_utc_now(),
            )

        if result.profile.critic_id != profile.critic_id:
            return CriticPanelRun(
                profile=profile,
                status="error",
                message="critic adapter returned a result for the wrong critic",
                started_at=started_at,
                finished_at=_utc_now(),
            )
        if result.profile.adapter != profile.adapter:
            return CriticPanelRun(
                profile=profile,
                status="error",
                message="critic adapter returned a result for the wrong adapter",
                started_at=started_at,
                finished_at=_utc_now(),
            )
        return result


@dataclass(slots=True)
class StaticCritic:
    """Test adapter that returns a scripted run, optionally after a delay."""

    result: CriticPanelRun
    delay_seconds: float = 0.0
    calls: list[str] = field(default_factory=list)

    async def run(self, profile: CriticProfile) -> CriticPanelRun:
        self.calls.append(profile.critic_id)
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        require(
            self.result.profile.critic_id == profile.critic_id,
            "StaticCritic.result.profile must match the requested critic",
        )
        require(
            self.result.profile.adapter == profile.adapter,
            "StaticCritic.result.profile.adapter must match the requested critic",
        )
        return self.result


_DEFAULT_CRITIC_PROFILES: tuple[CriticProfile, ...] = (
    CriticProfile(
        critic_id="architecture-critic",
        name="Architecture Critic",
        adapter="architecture-critic",
        scope=(
            "Checks boundaries, dependency direction, design simplicity, "
            "and fit with local patterns."
        ),
        required_inputs=("goal", "diff", "changed-files", "repo-context"),
        forbidden_actions=("edit-files", "merge-pr", "waive-gate"),
        minimum_model_capability_tags=("architecture-review", "reasoning-medium"),
        default_severity_mapping={
            "boundary-violation": "p1",
            "unnecessary-abstraction": "p2",
        },
        timeout_seconds=120.0,
        retry_limit=1,
        metadata={"category": "architecture"},
    ),
    CriticProfile(
        critic_id="test-critic",
        name="Test Critic",
        adapter="test-critic",
        scope="Checks TDD evidence, regression coverage, and whether tests prove the fix.",
        required_inputs=("goal", "diff", "changed-files", "test-results"),
        forbidden_actions=("edit-files", "merge-pr", "waive-gate"),
        minimum_model_capability_tags=("test-review", "reasoning-medium"),
        default_severity_mapping={
            "missing-regression": "p1",
            "shallow-assertion": "p2",
        },
        timeout_seconds=120.0,
        retry_limit=1,
        metadata={"category": "tests"},
    ),
    CriticProfile(
        critic_id="security-critic",
        name="Security Critic",
        adapter="security-critic",
        scope=(
            "Checks auth, secrets, sandbox escape, path traversal, and destructive-command risks."
        ),
        required_inputs=("goal", "diff", "changed-files", "test-results", "policy"),
        forbidden_actions=("edit-files", "merge-pr", "waive-gate"),
        minimum_model_capability_tags=("security-review", "reasoning-high"),
        default_severity_mapping={
            "secret-exposure": "p0",
            "unsafe-command": "p1",
            "hardening-note": "p2",
        },
        timeout_seconds=180.0,
        retry_limit=1,
        metadata={"category": "security"},
    ),
    CriticProfile(
        critic_id="maintainability-critic",
        name="Maintainability Critic",
        adapter="maintainability-critic",
        scope="Checks readability, DRY boundaries, and long-term operability of the patch.",
        required_inputs=("goal", "diff", "changed-files", "repo-context"),
        forbidden_actions=("edit-files", "merge-pr", "waive-gate"),
        minimum_model_capability_tags=("maintainability-review", "reasoning-medium"),
        default_severity_mapping={
            "operability-regression": "p1",
            "duplication": "p2",
            "cleanup-note": "p3",
        },
        timeout_seconds=120.0,
        retry_limit=1,
        metadata={"category": "maintainability"},
    ),
    CriticProfile(
        critic_id="product-critic",
        name="Product Critic",
        adapter="product-critic",
        scope=(
            "Checks that the patch improves the home-user experience "
            "and avoids enterprise-only complexity."
        ),
        required_inputs=("goal", "diff", "changed-files", "issue-context"),
        forbidden_actions=("edit-files", "merge-pr", "waive-gate"),
        minimum_model_capability_tags=("product-review", "reasoning-medium"),
        default_severity_mapping={
            "home-user-regression": "p1",
            "workflow-friction": "p2",
            "follow-up-idea": "note",
        },
        required=False,
        timeout_seconds=90.0,
        retry_limit=1,
        metadata={"category": "product"},
    ),
    CriticProfile(
        critic_id="release-critic",
        name="Release Critic",
        adapter="release-critic",
        scope=(
            "Checks docs, migration notes, rollback considerations, "
            "and operator guidance when relevant."
        ),
        required_inputs=("goal", "diff", "changed-files", "docs", "release-context"),
        forbidden_actions=("edit-files", "merge-pr", "waive-gate"),
        minimum_model_capability_tags=("release-review", "reasoning-medium"),
        default_severity_mapping={
            "missing-migration-note": "p1",
            "missing-doc-link": "p2",
            "release-note": "note",
        },
        required=False,
        timeout_seconds=90.0,
        retry_limit=1,
        metadata={"category": "release"},
    ),
)


def default_critic_profiles() -> tuple[CriticProfile, ...]:
    """Return the built-in critic profile catalog in stable order."""

    return _DEFAULT_CRITIC_PROFILES


def critic_profile_by_id(critic_id: str) -> CriticProfile | None:
    """Return one built-in critic profile by id when it exists."""

    for profile in _DEFAULT_CRITIC_PROFILES:
        if profile.critic_id == critic_id:
            return profile
    return None


@dataclass(slots=True, frozen=True)
class CriticPanelGateAdapter:
    """Bridge a critic panel into the gate runtime adapter protocol."""

    runner: CriticPanelRunner
    profiles: tuple[CriticProfile, ...]

    def __post_init__(self) -> None:
        require(bool(self.profiles), "CriticPanelGateAdapter.profiles must not be empty")

    async def run(self, gate: GateDefinition) -> GateAdapterResult:
        verdict = await self.runner.run(self.profiles)
        evidence = tuple(item for finding in verdict.findings for item in finding.evidence)
        message = verdict.message or gate.name or "critic panel completed"
        return GateAdapterResult(passed=verdict.passed, evidence=evidence, message=message)
