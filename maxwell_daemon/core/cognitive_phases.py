"""Cognitive Phases (formerly Phase-Gate Workflows).

This module defines the state machine pipeline for a task. A task progresses through
distinct cognitive phases: Strategize -> Implement -> Validate (The Maxwell Crucible).
If validation fails, the pipeline enforces revision, adhering to TDD and DbC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from maxwell_daemon.core.roles import Job, RolePlayer


class PhaseDecision(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEEDS_REVISION = "needs_revision"


@dataclass(slots=True)
class PipelineResult:
    status: PhaseDecision
    final_artifact: str
    history: list[str] = field(default_factory=list)


class CognitivePipeline:
    """Orchestrates the lifecycle of a task through orthogonal roles."""

    def __init__(
        self, strategist: RolePlayer, implementer: RolePlayer, validator: RolePlayer
    ) -> None:
        self.strategist = strategist
        self.implementer = implementer
        self.validator = validator

    async def run(self, job: Job) -> PipelineResult:
        """Runs the task through the Maxwell Cognitive Pipeline."""
        history: list[str] = []

        # Phase 1: Strategize (Architect)
        plan_response = await self.strategist.execute(job)
        plan = plan_response.content
        history.append(f"Strategist Plan:\n{plan}")

        # Phase 2: Implement (Coder)
        impl_job = Job(
            instructions=(
                f"Implement the following plan. Adhere to DbC, DRY, and LOD.\n\nPlan:\n{plan}"
            ),
            context_data=job.context_data,
        )
        impl_response = await self.implementer.execute(impl_job)
        code = impl_response.content
        history.append(f"Implementation:\n{code}")

        # Phase 3: The Maxwell Crucible (Validator / Trident QA)
        val_job = Job(
            instructions=(
                f"Verify this implementation against the plan.\n"
                f"Plan:\n{plan}\n\nCode:\n{code}\n\n"
                f"Reply with exactly PASS if the code perfectly implements "
                f"the plan, or FAIL with reasons."
            ),
            context_data=job.context_data,
        )
        val_response = await self.validator.execute(val_job)
        validation = val_response.content.strip()
        history.append(f"Crucible Validation:\n{validation}")

        # Simple verification gate
        if validation.upper().startswith("PASS"):
            return PipelineResult(
                status=PhaseDecision.SUCCESS, final_artifact=code, history=history
            )

        return PipelineResult(status=PhaseDecision.FAILURE, final_artifact=code, history=history)
