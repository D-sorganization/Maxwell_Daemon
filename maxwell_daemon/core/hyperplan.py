"""HyperPlan Collaborative Strategist DAG Engine.

This module implements parallel, collaborative planning workflows based on a Directed
Acyclic Graph (DAG) of steps. It supports standard, ensemble, peer review, and cross-review
strategies, executing independent steps in parallel using asyncio.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

from maxwell_daemon.core.roles import Job, Role, RolePlayer

logger = logging.getLogger(__name__)

# ============================================================================
# Prompt Templates
# ============================================================================

PLAN_SYSTEM_PROMPT_SUFFIX = """
<additional_output_section>
Include this section before the final ## TL;DR section:

## Risks & Alternatives
- Key risks with this approach and how they're mitigated
- Alternatives you considered and why you rejected them
- Assumptions that, if wrong, would change the plan
</additional_output_section>"""

REVIEW_SYSTEM_PROMPT = """<current_operating_mode mode="review">
Review the implementation plan provided and produce structured feedback.
You are NOT producing a new plan — only evaluating the given one.

<capabilities>
- Analyze the plan for correctness, completeness, and feasibility
- Identify risks, gaps, and potential failure modes
- Suggest specific improvements with rationale
- Read files and explore the codebase to verify claims in the plan
</capabilities>

<constraints>
- Do not produce a new plan — only review the existing one
- Do not modify any files
- Do not run commands that change state
- Be specific — reference exact sections, steps, or code from the plan
</constraints>

<review_dimensions>
Only raise findings you would be comfortable blocking a PR on. Do not make trivial, nitpicky, or speculative comments. Every finding should be a real bug, a real risk, or a meaningfully better approach.

Every finding must include a Criticality score written as N/10 so the user can decide whether the fix is worth the engineering effort. Score by severity, likelihood, user impact, and engineering risk: 10/10 is a release blocker, 7-9/10 is high risk, 4-6/10 is meaningful but not necessarily blocking, and 1-3/10 is low importance.

Evaluate through these lenses:

1. **Bugs & correctness** — Logic errors, off-by-one mistakes, wrong assumptions, broken edge cases, regressions. If it would break in production, flag it.
2. **Security** — Injection vectors (command, SQL, XSS), unsafe deserialization, secrets in code, missing input validation at trust boundaries, and other OWASP-class vulnerabilities.
3. **Better approaches** — Actively look for objectively much better ways to achieve the same goal, including radically different implementations. Can it unify with an existing pattern in the codebase? Is there already a utility, helper, or convention that does this? Would a different strategy avoid an entire class of bugs or simplify the code significantly? The best review comments are "we already do this over in X, you can reuse that" or "this whole thing collapses if you use Y instead." Don't flag "could also do it this way" alternatives, marginal preferences, or approaches that are only a little better — only suggest a different approach when it is clearly superior enough to justify changing direction.
4. **Test quality** — Tests that don't actually catch regressions: excessive mocking that hides real behavior, verbatim assertions on incidental text or implementation output, trivial/tautological checks, tests that duplicate the implementation logic, and missing coverage of edge cases or failure paths that matter.
5. **Robustness** — Will this code behave correctly under unexpected inputs, concurrency, partial failures, or edge cases? Unhandled errors, race conditions, missing guards at system boundaries, and assumptions that could silently break.
</review_dimensions>

<engineering_guidance>
Engineering standards to enforce when they affect correctness, maintainability, or test confidence:

- **Tight contracts** — Flag loose typing, unnecessary casts, unvalidated inputs, broad object shapes, or public interfaces that can be narrowed with concrete types, schemas, parsers, validators, discriminated unions, or smaller module boundaries.
- **Modularity** — Point out interfaces that can be tightened to make code cleaner, easier to test, and less coupled. Prefer the smallest contract that expresses the real dependency.
- **Simplicity** — Prefer surgical fixes and existing patterns. Do not push abstractions unless they remove real duplication, clarify multiple call sites, or prevent a class of bugs.
- **High-signal tests** — Flag tests that over-mock production behavior, duplicate implementation logic, rely on brittle verbatim checks, assert internals instead of observable behavior, or would pass when behavior is broken. Prefer regression-detecting unit tests and integration tests that exercise edge cases, failure paths, and the real production path.
- **Robustness** — Flag broad catch blocks, swallowed errors, unchecked status codes, missing permissions/error handling/retries where they matter, and schema changes that ignore existing production data.
- **Operational visibility** — For user-facing or production-critical paths, flag missing structured logs, metrics, analytics, or docs explaining how to investigate failures.
- **Infrastructure & data safety** — Flag hidden setup steps, fragmented verification commands, database mutations without explicit intent/transactions/destructive-query checks, and migrations that do not protect existing production data.
- **Comments, docs & local instructions** — Check CLAUDE.md and AGENT.md in touched directories. Flag filler comments, stale docs, and missing rationale for non-obvious tradeoffs. When behavior or workflow changes, point out docs that should be updated.
</engineering_guidance>

<output_format>
## Strengths
What the plan gets right — be specific about which parts are strong and why.

## Weaknesses
What the plan gets wrong or misses — be specific about which parts are weak and why.

## Risks
Potential failure modes, edge cases, or assumptions that could break.

## Suggestions
Specific, actionable improvements. For each suggestion, reference the plan section it applies to.
</output_format>
</current_operating_mode>"""

REVISE_SYSTEM_PROMPT = """<revision_mode>
The user will share peer review feedback on your plan from an independent reviewer.
Evaluate each point on its merits:
- Adopt suggestions that genuinely improve the plan
- Reject suggestions you disagree with (briefly note why in Revision Notes)
- Produce a complete revised plan, not just a diff
- End the revised plan with ## TL;DR containing 3-6 concise bullets; do not use predefined content slots. Do not add anything after this section.
</revision_mode>"""

RECONCILE_SYSTEM_PROMPT = """<current_operating_mode mode="reconcile">
You are given multiple implementation plans and/or reviews for the same task.
Your job is to produce a single, optimal final plan.

<capabilities>
- Analyze and compare multiple plans side-by-side
- Identify the strongest elements from each plan
- Merge, adopt, or synthesize approaches as appropriate
- Read files and explore the codebase to verify claims
</capabilities>

<constraints>
- You MUST produce exactly one final plan
- Do not modify any files
- Do not run commands that change state
- Evaluate objectively — no plan has inherent priority over another
</constraints>

<guidelines>
- Keep explicit user asks in User-Specified Requirements; keep agent-created synthesis in Decisions, Plan, or Reconciliation Notes.
</guidelines>

<evaluation_rubric>
For each plan, evaluate on these dimensions:
1. Correctness — Does it fully address the requirements?
2. Minimality — Only necessary changes, no over-engineering?
3. Testability — Are tests meaningful (not excessive mocking or tautological assertions)?
4. Risk — Edge cases handled? What could go wrong?
5. Reusability — Leverages existing patterns vs. reinventing?
6. Clarity — Could another engineer execute this without questions?
7. Security — Free of injection, unsafe deserialization, or missing trust-boundary validation?
8. Robustness — Handles unexpected inputs, partial failures, and concurrency safely?

You may adopt one plan wholesale if it's clearly superior,
or merge the strongest elements from multiple plans.
</evaluation_rubric>

<output_format>
## Overview
Brief summary of the plan.

## User-Specified Requirements
Explicit requests, constraints, preferences, or acceptance criteria from the user. Do not include agent-created ideas here.

## Outcomes
A bulleted list of outcomes to expect when the task is completed.

## Decisions
When there are meaningful choices to make, present each as a decision with alternatives.

## Plan
Implementation steps with code blocks for key interfaces and signatures.

## Reconciliation Notes
- Which plan(s) formed the basis and why
- What was adopted from each input
- What was rejected and why

## TL;DR
Always end with this section. Include 3-6 concise bullets; do not use predefined content slots. Do not add anything after this section.
</output_format>
</current_operating_mode>"""

# ============================================================================
# Step primitives
# ============================================================================


class HyperPlanStep(ABC):
    """Base class for all HyperPlan steps in the DAG."""

    def __init__(self, id: str, role_player: RolePlayer, inputs: list[str]) -> None:
        self.id = id
        self.role_player = role_player
        self.inputs = inputs

    @abstractmethod
    def build_prompt(self, task_description: str, step_results: dict[str, str]) -> tuple[str, str]:
        """Returns a tuple of (system_prompt, user_message) for step execution."""
        ...


class PlanStep(HyperPlanStep):
    """Generates an initial implementation plan."""

    def __init__(self, id: str, role_player: RolePlayer, inputs: list[str] | None = None) -> None:
        super().__init__(id, role_player, inputs or [])

    def build_prompt(self, task_description: str, step_results: dict[str, str]) -> tuple[str, str]:
        system_prompt = self.role_player.role.system_prompt + "\n" + PLAN_SYSTEM_PROMPT_SUFFIX
        user_message = task_description
        return system_prompt, user_message


class ReviewStep(HyperPlanStep):
    """Reviews an existing plan."""

    def build_prompt(self, task_description: str, step_results: dict[str, str]) -> tuple[str, str]:
        input_step_id = self.inputs[0]
        plan_text = step_results.get(input_step_id, "")
        system_prompt = REVIEW_SYSTEM_PROMPT
        user_message = (
            f"<task_description>\n{task_description}\n</task_description>\n\n"
            f'<plan_to_review id="{input_step_id}">\n{plan_text}\n</plan_to_review>'
        )
        return system_prompt, user_message


class ReviseStep(HyperPlanStep):
    """Revises a plan in response to review feedback."""

    def __init__(
        self, id: str, role_player: RolePlayer, inputs: list[str], resume_step_id: str
    ) -> None:
        super().__init__(id, role_player, inputs)
        self.resume_step_id = resume_step_id

    def build_prompt(self, task_description: str, step_results: dict[str, str]) -> tuple[str, str]:
        review_step_id = self.inputs[0]
        review_text = step_results.get(review_step_id, "")
        original_plan_text = step_results.get(self.resume_step_id, "")

        system_prompt = REVISE_SYSTEM_PROMPT
        user_message = (
            f'Here is the original plan:\n<original_plan id="{self.resume_step_id}">\n'
            f"{original_plan_text}\n</original_plan>\n\n"
            f"I asked an independent reviewer to evaluate your plan. Here's their feedback — "
            f"consider what resonates and what doesn't. Don't assume they're right about everything; "
            f'use your own judgment.\n<peer_review from="{review_step_id}">\n{review_text}\n'
            f"</peer_review>\n\n"
            f"Please produce a revised plan incorporating the feedback you agree with. "
            f'In a "Revision Notes" section at the end, note what you changed and what you kept, '
            f"and briefly explain why for any suggestions you rejected."
        )
        return system_prompt, user_message


class ReconcileStep(HyperPlanStep):
    """Reconciles multiple plans/reviews into a single final plan."""

    def build_prompt(self, task_description: str, step_results: dict[str, str]) -> tuple[str, str]:
        # Sub-methods handle detailed label mapping
        raise NotImplementedError("Use build_reconcile_prompt for ReconcileStep.")

    def build_reconcile_prompt(
        self,
        task_description: str,
        step_results: dict[str, str],
        step_primitives: dict[str, str],
        reviewed_plan_map: dict[str, str],
        shuffle: bool = True,
    ) -> tuple[str, str, list[dict[str, str]]]:
        # Filter to succeeded inputs
        valid_inputs = [inp for inp in self.inputs if inp in step_results]

        shuffled_inputs = list(valid_inputs)
        if shuffle:
            random.shuffle(shuffled_inputs)

        label_mapping = []
        plan_labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

        for i, input_id in enumerate(shuffled_inputs):
            label = plan_labels[i] if i < len(plan_labels) else str(i)
            label_mapping.append({"stepId": input_id, "label": label})

        input_blocks = []
        for i, input_id in enumerate(shuffled_inputs):
            label = label_mapping[i]["label"]
            text = step_results[input_id]
            prim = step_primitives.get(input_id, "plan")
            tag = "plan" if prim == "plan" else "review"

            reviews_attr = ""
            if prim == "review":
                reviewed_plan_id = reviewed_plan_map.get(input_id)
                if reviewed_plan_id:
                    reviewed_label = next(
                        (
                            item["label"]
                            for item in label_mapping
                            if item["stepId"] == reviewed_plan_id
                        ),
                        None,
                    )
                    if reviewed_label:
                        reviews_attr = f' reviews="Plan {reviewed_label}"'

            input_blocks.append(f'<{tag} id="{label}"{reviews_attr}>\n{text}\n</{tag}>')

        system_prompt = RECONCILE_SYSTEM_PROMPT
        user_message = (
            f"<task_description>\n{task_description}\n</task_description>\n\n"
            f'<inputs randomly_ordered="true">\n' + "\n\n".join(input_blocks) + "\n</inputs>"
        )
        return system_prompt, user_message, label_mapping


# ============================================================================
# Strategy definition
# ============================================================================


@dataclass(slots=True)
class HyperPlanStrategy:
    id: str
    name: str
    description: str
    steps: list[HyperPlanStep]
    terminal_step_id: str


# ============================================================================
# Presets
# ============================================================================


def standard_strategy(planner: RolePlayer) -> HyperPlanStrategy:
    """Standard planning strategy (single-agent)."""
    step = PlanStep(id="plan_0", role_player=planner)
    return HyperPlanStrategy(
        id="standard",
        name="Standard",
        description="Plan with a single agent",
        steps=[step],
        terminal_step_id="plan_0",
    )


def ensemble_strategy(planners: list[RolePlayer], reconciler: RolePlayer) -> HyperPlanStrategy:
    """Ensemble planning strategy (N planners, 1 reconciler)."""
    steps: list[HyperPlanStep] = []
    plan_ids = []
    for i, planner in enumerate(planners):
        step_id = f"plan_{i}"
        steps.append(PlanStep(id=step_id, role_player=planner))
        plan_ids.append(step_id)

    reconcile_step = ReconcileStep(id="reconcile_0", role_player=reconciler, inputs=plan_ids)
    steps.append(reconcile_step)

    return HyperPlanStrategy(
        id="ensemble",
        name="Ensemble",
        description="Multiple agents plan in parallel, then reconcile into one plan",
        steps=steps,
        terminal_step_id="reconcile_0",
    )


def peer_review_strategy(planner: RolePlayer, reviewer: RolePlayer) -> HyperPlanStrategy:
    """Peer review planning strategy (1 planner, 1 reviewer, 1 revision)."""
    plan_step = PlanStep(id="plan_a", role_player=planner)
    review_step = ReviewStep(id="review_b", role_player=reviewer, inputs=["plan_a"])
    revise_step = ReviseStep(
        id="revise_a", role_player=planner, inputs=["review_b"], resume_step_id="plan_a"
    )
    return HyperPlanStrategy(
        id="peer-review",
        name="Peer Review",
        description="One agent plans, another reviews, then the planner revises based on feedback",
        steps=[plan_step, review_step, revise_step],
        terminal_step_id="revise_a",
    )


def cross_review_strategy(
    agent_a: RolePlayer, agent_b: RolePlayer, reconciler: RolePlayer
) -> HyperPlanStrategy:
    """Cross-review planning strategy (2 planners cross-review, then reconcile)."""
    plan_a = PlanStep(id="plan_a", role_player=agent_a)
    plan_b = PlanStep(id="plan_b", role_player=agent_b)
    review_a_of_b = ReviewStep(id="review_a_of_b", role_player=agent_a, inputs=["plan_b"])
    review_b_of_a = ReviewStep(id="review_b_of_a", role_player=agent_b, inputs=["plan_a"])
    reconcile = ReconcileStep(
        id="reconcile_0",
        role_player=reconciler,
        inputs=["plan_a", "plan_b", "review_a_of_b", "review_b_of_a"],
    )
    return HyperPlanStrategy(
        id="cross-review",
        name="Cross-Review",
        description="Two agents plan and cross-review each other, then reconcile",
        steps=[plan_a, plan_b, review_a_of_b, review_b_of_a, reconcile],
        terminal_step_id="reconcile_0",
    )


# ============================================================================
# Validation & Depth Sorting
# ============================================================================


def _validate_duplicate_ids(
    strategy: HyperPlanStrategy, errors: list[str]
) -> dict[str, HyperPlanStep]:
    step_map: dict[str, HyperPlanStep] = {}
    for s in strategy.steps:
        if s.id in step_map:
            errors.append("Duplicate step IDs")
        else:
            step_map[s.id] = s
    return step_map


def _validate_terminal_step(
    strategy: HyperPlanStrategy, step_map: dict[str, HyperPlanStep], errors: list[str]
) -> None:
    terminal = step_map.get(strategy.terminal_step_id)
    if not terminal:
        errors.append(f'Terminal step "{strategy.terminal_step_id}" not found')
    elif isinstance(terminal, ReviewStep):
        errors.append(
            "Terminal step must produce a plan (plan, reconcile, or revise), not a review"
        )


def _validate_step_constraints(
    step: HyperPlanStep, step_map: dict[str, HyperPlanStep], errors: list[str]
) -> None:
    if isinstance(step, PlanStep) and len(step.inputs) > 0:
        errors.append(f'Plan step "{step.id}" must have no inputs')

    if isinstance(step, ReviewStep) and len(step.inputs) != 1:
        errors.append(f'Review step "{step.id}" must have exactly 1 input')

    if isinstance(step, ReconcileStep) and len(step.inputs) < 1:
        errors.append(f'Reconcile step "{step.id}" must have at least 1 input')

    if isinstance(step, ReviseStep):
        if len(step.inputs) != 1:
            errors.append(f'Revise step "{step.id}" must have exactly 1 input')

        if not step.resume_step_id:
            errors.append(f'Revise step "{step.id}" must have a resume_step_id')
        elif step.resume_step_id not in step_map:
            errors.append(
                f'Revise step "{step.id}" references unknown resume_step_id "{step.resume_step_id}"'
            )
        else:
            resume_target = step_map[step.resume_step_id]
            if not isinstance(resume_target, PlanStep):
                errors.append(
                    f'Revise step "{step.id}" can only resume a plan step, not "{type(resume_target).__name__}"'
                )

    for inp in step.inputs:
        if inp not in step_map:
            errors.append(f'Step "{step.id}" references unknown input "{inp}"')


def _validate_cycles(
    strategy: HyperPlanStrategy, step_map: dict[str, HyperPlanStep], errors: list[str]
) -> None:
    visited: set[str] = set()
    visiting: set[str] = set()

    def has_cycle(step_id: str) -> bool:
        if step_id in visiting:
            return True
        if step_id in visited:
            return False
        visiting.add(step_id)
        step_obj = step_map.get(step_id)
        if step_obj:
            for inp in step_obj.inputs:
                if has_cycle(inp):
                    return True
        visiting.remove(step_id)
        visited.add(step_id)
        return False

    for step in strategy.steps:
        if has_cycle(step.id):
            errors.append("Strategy contains a cycle")
            break


def _validate_single_terminal(strategy: HyperPlanStrategy, errors: list[str]) -> None:
    depended: set[str] = set()
    for s in strategy.steps:
        depended.update(s.inputs)

    terminals = [s for s in strategy.steps if s.id not in depended]
    if len(terminals) != 1:
        errors.append(
            f"Expected 1 terminal step, found {len(terminals)}: [{', '.join(t.id for t in terminals)}]"
        )
    elif terminals[0].id != strategy.terminal_step_id:
        errors.append(
            f'Terminal step "{strategy.terminal_step_id}" has dependents, or orphan step "{terminals[0].id}" exists'
        )


def validate_strategy(strategy: HyperPlanStrategy) -> list[str]:
    """Validates the structural invariants of a HyperPlan strategy DAG."""
    errors: list[str] = []
    step_map = _validate_duplicate_ids(strategy, errors)
    _validate_terminal_step(strategy, step_map, errors)

    for step in strategy.steps:
        _validate_step_constraints(step, step_map, errors)

    _validate_cycles(strategy, step_map, errors)
    _validate_single_terminal(strategy, errors)

    return errors


def group_by_depth(strategy: HyperPlanStrategy) -> list[list[HyperPlanStep]]:
    """Groups DAG steps by depth layer for parallel execution."""
    step_map = {s.id: s for s in strategy.steps}
    depth_map: dict[str, int] = {}

    def get_depth(step_id: str) -> int:
        if step_id in depth_map:
            return depth_map[step_id]
        step = step_map.get(step_id)
        if not step or len(step.inputs) == 0:
            depth_map[step_id] = 0
            return 0
        max_input_depth = max(get_depth(inp) for inp in step.inputs)
        depth = max_input_depth + 1
        depth_map[step_id] = depth
        return depth

    for step in strategy.steps:
        get_depth(step.id)

    if not depth_map:
        return []

    max_depth = max(depth_map.values())
    layers: list[list[HyperPlanStep]] = []
    for d in range(max_depth + 1):
        layer = [s for s in strategy.steps if depth_map.get(s.id) == d]
        if layer:
            layers.append(layer)
    return layers


# ============================================================================
# DAG Runner Engine
# ============================================================================


class HyperPlanExecutor:
    """Executes a HyperPlan strategy DAG in depth-layered parallel order."""

    def __init__(self, strategy: HyperPlanStrategy) -> None:
        self.strategy = strategy
        self.step_results: dict[str, str] = {}
        errors = validate_strategy(self.strategy)
        if errors:
            raise ValueError(f"Invalid strategy: {', '.join(errors)}")

    async def execute_step(self, step: HyperPlanStep, task_description: str) -> str:
        """Executes a single step using its assigned RolePlayer and prompt builders."""
        logger.info("Executing HyperPlan step: %s (type: %s)", step.id, type(step).__name__)

        if isinstance(step, (PlanStep, ReviewStep, ReviseStep)):
            system_prompt, user_message = step.build_prompt(task_description, self.step_results)
        elif isinstance(step, ReconcileStep):
            step_primitives = {}
            reviewed_plan_map = {}
            for s in self.strategy.steps:
                if isinstance(s, PlanStep):
                    step_primitives[s.id] = "plan"
                elif isinstance(s, ReviewStep):
                    step_primitives[s.id] = "review"
                    if len(s.inputs) > 0:
                        reviewed_plan_map[s.id] = s.inputs[0]
                elif isinstance(s, ReconcileStep):
                    step_primitives[s.id] = "reconcile"
                elif isinstance(s, ReviseStep):
                    step_primitives[s.id] = "revise"

            system_prompt, user_message, _ = step.build_reconcile_prompt(
                task_description, self.step_results, step_primitives, reviewed_plan_map
            )
        else:
            raise TypeError(f"Unknown step type: {type(step)}")

        # Construct temporary Role and RolePlayer to execute with modified prompts
        temp_role = Role(
            name=step.role_player.role.name,
            system_prompt=system_prompt,
            requires_tool_use=step.role_player.role.requires_tool_use,
        )
        temp_player = RolePlayer(
            role=temp_role,
            backend=step.role_player.backend,
            model=step.role_player.model,
        )

        job = Job(instructions=user_message)
        response = await temp_player.execute(job)
        logger.info("Step %s completed successfully", step.id)
        return response.content

    async def execute(self, task_description: str) -> str:
        """Executes the complete DAG layer-by-layer using asyncio.gather."""
        layers = group_by_depth(self.strategy)
        for d, layer in enumerate(layers):
            logger.info("Executing depth layer %d with steps: %s", d, [s.id for s in layer])
            tasks = [self.execute_step(step, task_description) for step in layer]
            results = await asyncio.gather(*tasks)
            for step, result in zip(layer, results, strict=True):
                self.step_results[step.id] = result

        return self.step_results[self.strategy.terminal_step_id]
