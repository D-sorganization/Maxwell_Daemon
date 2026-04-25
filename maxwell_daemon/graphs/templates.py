"""Built-in graph templates for named sub-agent delivery flows."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from maxwell_daemon.graphs.models import AgentRole, GraphNode, TaskGraph


class TaskGraphTemplate(str, Enum):
    """Supported built-in task graph templates."""

    MICRO_DELIVERY = "micro-delivery"
    STANDARD_DELIVERY = "standard-delivery"
    SECURITY_SENSITIVE_DELIVERY = "security-sensitive-delivery"


class TemplateSelectionInput(BaseModel):
    """Minimal metadata used to choose a built-in template."""

    model_config = ConfigDict(frozen=True)

    risk: Literal["low", "normal", "high", "critical"] = "normal"
    estimated_steps: int = Field(default=3, ge=1)
    security_sensitive: bool = False
    labels: tuple[str, ...] = ()


def select_template(selection: TemplateSelectionInput) -> TaskGraphTemplate:
    """Select the smallest deterministic template matching the input metadata."""
    labels = {label.strip().lower() for label in selection.labels}
    has_security_signal = bool(labels & {"security", "auth", "secrets", "permissions"})
    if (
        selection.security_sensitive
        or has_security_signal
        or selection.risk in {"high", "critical"}
    ):
        return TaskGraphTemplate.SECURITY_SENSITIVE_DELIVERY
    if selection.risk == "low" and selection.estimated_steps <= 2:
        return TaskGraphTemplate.MICRO_DELIVERY
    return TaskGraphTemplate.STANDARD_DELIVERY


def build_template_graph(template: TaskGraphTemplate) -> TaskGraph:
    """Build a validated graph for a built-in template."""
    return TaskGraph(
        id=template.value,
        name=template.value,
        nodes=_nodes_for_template(template),
    )


def _nodes_for_template(template: TaskGraphTemplate) -> tuple[GraphNode, ...]:
    if template is TaskGraphTemplate.MICRO_DELIVERY:
        return (
            GraphNode(
                id="implement",
                role=AgentRole.IMPLEMENTER,
                description="Implement and self-check a tightly scoped change.",
                outputs=("change-summary", "verification-notes"),
            ),
        )

    if template is TaskGraphTemplate.STANDARD_DELIVERY:
        return (
            _plan_node(),
            _implement_node(depends_on=("plan",)),
            _qa_node(depends_on=("implement",)),
            _review_node(depends_on=("qa",), inputs=("verification-notes",)),
        )

    return (
        _plan_node(),
        _implement_node(depends_on=("plan",)),
        _qa_node(depends_on=("implement",)),
        GraphNode(
            id="security-review",
            role=AgentRole.SECURITY_REVIEWER,
            description="Review security-sensitive behavior, secrets handling, and permissions.",
            depends_on=("qa",),
            inputs=("change-summary", "verification-notes"),
            outputs=("security-review-notes",),
        ),
        _review_node(
            depends_on=("security-review",), inputs=("security-review-notes",)
        ),
    )


def _plan_node() -> GraphNode:
    return GraphNode(
        id="plan",
        role=AgentRole.PLANNER,
        description="Plan scope, dependencies, and verification for the delivery.",
        outputs=("implementation-plan",),
    )


def _implement_node(*, depends_on: tuple[str, ...]) -> GraphNode:
    return GraphNode(
        id="implement",
        role=AgentRole.IMPLEMENTER,
        description="Implement the planned change.",
        depends_on=depends_on,
        inputs=("implementation-plan",),
        outputs=("change-summary",),
    )


def _qa_node(*, depends_on: tuple[str, ...]) -> GraphNode:
    return GraphNode(
        id="qa",
        role=AgentRole.QA,
        description="Verify acceptance criteria and record evidence.",
        depends_on=depends_on,
        inputs=("change-summary",),
        outputs=("verification-notes",),
    )


def _review_node(*, depends_on: tuple[str, ...], inputs: tuple[str, ...]) -> GraphNode:
    return GraphNode(
        id="review",
        role=AgentRole.REVIEWER,
        description="Review the completed work for handoff readiness.",
        depends_on=depends_on,
        inputs=inputs,
        outputs=("review-notes",),
    )
