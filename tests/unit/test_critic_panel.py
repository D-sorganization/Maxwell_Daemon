"""Tests for the critic panel and its gate-runtime bridge."""

from __future__ import annotations

import pytest

from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.core.critics import (
    CriticAggregatePolicy,
    CriticFinding,
    CriticPanelRun,
    CriticPanelRunner,
    CriticProfile,
    CriticRunStatus,
    CriticVerdict,
    StaticCritic,
    critic_profile_by_id,
    default_critic_profiles,
)
from maxwell_daemon.core.gates import GateDefinition, GauntletRuntime, InMemoryGateStore


def _profile(
    critic_id: str,
    *,
    required: bool = True,
    timeout_seconds: float | None = None,
    adapter: str = "static",
    retry_limit: int = 0,
) -> CriticProfile:
    return CriticProfile(
        critic_id=critic_id,
        name=critic_id.replace("-", " ").title(),
        adapter=adapter,
        required=required,
        timeout_seconds=timeout_seconds,
        retry_limit=retry_limit,
    )


def _run(
    critic_id: str,
    *,
    status: CriticRunStatus = "passed",
    findings: tuple[CriticFinding, ...] = (),
    adapter: str = "static",
    required: bool = True,
) -> CriticPanelRun:
    profile = _profile(critic_id, required=required, adapter=adapter)
    return CriticPanelRun(profile=profile, status=status, findings=findings)


class TestCriticProfileValidation:
    def test_rejects_blank_identifier(self) -> None:
        with pytest.raises(PreconditionError, match="critic_id"):
            CriticProfile(critic_id=" ", name="Name", adapter="static")

    def test_rejects_non_positive_timeout(self) -> None:
        with pytest.raises(PreconditionError, match="timeout_seconds"):
            _profile("critic-1", timeout_seconds=0)

    def test_rejects_negative_retry_limit(self) -> None:
        with pytest.raises(PreconditionError, match="retry_limit"):
            _profile("critic-1", retry_limit=-1)


class TestBuiltInCriticProfiles:
    def test_catalog_contains_expected_profiles_in_stable_order(self) -> None:
        profiles = default_critic_profiles()

        assert tuple(profile.critic_id for profile in profiles) == (
            "architecture-critic",
            "test-critic",
            "security-critic",
            "maintainability-critic",
            "product-critic",
            "release-critic",
        )
        assert critic_profile_by_id("security-critic") == profiles[2]
        assert critic_profile_by_id("missing-critic") is None

    def test_catalog_profiles_declare_contract_fields(self) -> None:
        for profile in default_critic_profiles():
            assert profile.title == profile.name
            assert profile.scope
            assert profile.required_inputs
            assert profile.forbidden_actions
            assert profile.output_schema_version == "critic.v1"
            assert profile.minimum_model_capability_tags
            assert profile.default_severity_mapping


class TestCriticAggregation:
    async def test_p0_finding_blocks_by_default_policy(self) -> None:
        finding = CriticFinding(
            critic_id="critic-0",
            severity="p0",
            summary="Critical security defect",
            detail="public token leak",
            evidence=("token printed in logs",),
        )
        profile = _profile("critic-0")
        runner = CriticPanelRunner(
            adapters={
                "static": StaticCritic(
                    result=CriticPanelRun(
                        profile=profile, status="failed", findings=(finding,)
                    )
                )
            }
        )

        verdict = await runner.run([profile])

        assert verdict.passed is False
        assert verdict.blocking_findings == (finding,)

    async def test_p1_finding_blocks_and_preserves_evidence(self) -> None:
        finding = CriticFinding(
            critic_id="critic-1",
            severity="p1",
            summary="Blocking defect",
            detail="critical regression",
            file_path="src/app.py",
            line_number=42,
            evidence=("stderr: boom", "stdout: trace"),
        )
        profile = _profile("critic-1")
        runner = CriticPanelRunner(
            adapters={
                "static": StaticCritic(
                    result=CriticPanelRun(
                        profile=profile, status="failed", findings=(finding,)
                    )
                )
            }
        )

        verdict = await runner.run([profile])

        assert verdict.passed is False
        assert verdict.blocking_findings == (finding,)
        assert verdict.findings[0].file_path == "src/app.py"
        assert verdict.findings[0].line_number == 42
        assert verdict.findings[0].evidence == ("stderr: boom", "stdout: trace")

    async def test_p2_note_passes_without_blocking(self) -> None:
        finding = CriticFinding(
            critic_id="critic-2",
            severity="p2",
            summary="Style note",
            detail="rename the helper",
            file_path="src/app.py",
            line_number=11,
            evidence=("prefer a narrower name",),
        )
        profile = _profile("critic-2")
        runner = CriticPanelRunner(
            adapters={
                "static": StaticCritic(
                    result=CriticPanelRun(
                        profile=profile, status="passed", findings=(finding,)
                    )
                )
            }
        )

        verdict = await runner.run([profile])

        assert verdict.passed is True
        assert verdict.blocking_findings == ()
        assert verdict.nonblocking_findings == (finding,)

    async def test_missing_required_critic_fails_closed(self) -> None:
        profile = _profile("critic-3")
        runner = CriticPanelRunner(adapters={})

        verdict = await runner.run([profile])

        assert verdict.passed is False
        assert verdict.missing_required_critic_ids == ("critic-3",)
        assert verdict.blocking_findings
        assert verdict.blocking_findings[0].summary == "Critic Missing"

    async def test_optional_timeout_is_recorded_but_nonblocking_when_allowed(
        self,
    ) -> None:
        profile = _profile("critic-4", required=False, timeout_seconds=0.01)
        slow_run = CriticPanelRun(profile=profile, status="passed")
        runner = CriticPanelRunner(
            adapters={"static": StaticCritic(result=slow_run, delay_seconds=0.1)},
            policy=CriticAggregatePolicy(optional_issue_is_blocking=False),
        )

        verdict = await runner.run([profile])

        assert verdict.passed is True
        assert verdict.timed_out_critic_ids == ("critic-4",)
        assert verdict.nonblocking_findings[0].critic_id == "critic-4"
        assert verdict.nonblocking_findings[0].severity == "p2"

    async def test_malformed_required_critic_is_rejected_closed(self) -> None:
        with pytest.raises(PreconditionError, match="critic_id"):
            _profile(" ", required=True)

    def test_aggregate_verdict_is_stable_regardless_of_result_order(self) -> None:
        first = _run("critic-1")
        second = _run(
            "critic-2",
            findings=(
                CriticFinding(
                    critic_id="critic-2",
                    severity="p2",
                    summary="Note",
                    detail="keep",
                    evidence=("evidence-b",),
                ),
            ),
        )
        policy = CriticAggregatePolicy()

        verdict_a = policy.aggregate((first, second))
        verdict_b = policy.aggregate((second, first))

        assert verdict_a == verdict_b
        assert verdict_a.runs == (first, second)
        assert verdict_a.findings[0].critic_id == "critic-2"

    def test_pass_verdict_cannot_contain_blocking_findings(self) -> None:
        finding = CriticFinding(
            critic_id="critic-5",
            severity="p1",
            summary="Blocker",
        )
        run = _run("critic-5", status="failed", findings=(finding,))

        with pytest.raises(PreconditionError, match="passed"):
            CriticVerdict(passed=True, runs=(run,), findings=(finding,))


class TestGateAdapterBridge:
    async def test_bridge_to_gate_runtime_uses_critic_panel_verdict(self) -> None:
        profile = _profile("critic-6")
        finding = CriticFinding(
            critic_id="critic-6",
            severity="p2",
            summary="Note",
            detail="optional observation",
            evidence=("note evidence",),
        )
        runner = CriticPanelRunner(
            adapters={
                "static": StaticCritic(
                    result=CriticPanelRun(
                        profile=profile, status="passed", findings=(finding,)
                    )
                )
            }
        )
        gate_adapter = runner.as_gate_adapter([profile])
        gauntlet = GauntletRuntime(
            adapters={"critic-panel": gate_adapter}, store=InMemoryGateStore()
        )

        decision = await gauntlet.run(
            [
                GateDefinition(
                    gate_id="critic-panel-gate",
                    name="Critic Panel",
                    adapter="critic-panel",
                )
            ]
        )

        assert decision.passed is True
        assert decision.evidence == ("note evidence",)
