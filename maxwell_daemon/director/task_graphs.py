"""Named sub-agent task graph models and built-in delivery templates."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maxwell_daemon.core.artifacts import ArtifactKind
from maxwell_daemon.core.work_items import WorkItem

__all__ = [
    "AgentRole",
    "GraphNode",
    "GraphStatus",
    "NodeRun",
    "NodeRunStatus",
    "TaskGraph",
    "TaskGraphTemplate",
    "build_task_graph",
    "select_task_graph_template",
]


class AgentRole(str, Enum):
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    QA = "qa"
    REVIEWER = "reviewer"
    SECURITY = "security"
    DOCS = "docs"
    PUBLISHER = "publisher"
    SPECIALIST = "specialist"


class GraphStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class NodeRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskGraphTemplate(str, Enum):
    MICRO_DELIVERY = "micro-delivery"
    STANDARD_DELIVERY = "standard-delivery"
    SECURITY_SENSITIVE_DELIVERY = "security-sensitive-delivery"


class GraphNode(BaseModel):
    """One role invocation in a delivery graph."""

    model_config = ConfigDict(use_enum_values=False, frozen=True)

    id: str = Field(..., min_length=1)
    role: AgentRole
    backend: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    depends_on: tuple[str, ...] = ()
    required_artifacts: tuple[ArtifactKind, ...] = ()
    output_artifact_kind: ArtifactKind
    max_retries: int = Field(default=1, ge=0, le=5)
    instructions: str = ""

    @field_validator("depends_on")
    @classmethod
    def _validate_depends_on(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _reject_blank_entries("depends_on", values)
        if len(set(values)) != len(values):
            raise ValueError("depends_on entries must be unique")
        return values


class NodeRun(BaseModel):
    """Execution record for one graph node."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    graph_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    status: NodeRunStatus = NodeRunStatus.QUEUED
    task_id: str | None = Field(default=None, min_length=1)
    artifact_ids: tuple[str, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cost_usd: float = Field(default=0.0, ge=0)
    attempts: int = Field(default=0, ge=0)
    error: str | None = None


class TaskGraph(BaseModel):
    """Validated DAG of named sub-agent roles for one work item."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    work_item_id: str = Field(..., min_length=1)
    template: TaskGraphTemplate
    nodes: tuple[GraphNode, ...] = Field(..., min_length=1)
    status: GraphStatus = GraphStatus.QUEUED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_graph(self) -> TaskGraph:
        node_ids = tuple(node.id for node in self.nodes)
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("task graph node ids must be unique")

        known_ids = set(node_ids)
        for node in self.nodes:
            for dep in node.depends_on:
                if dep not in known_ids:
                    raise ValueError(f"node {node.id!r} depends on unknown node {dep!r}")
                if dep == node.id:
                    raise ValueError(f"node {node.id!r} cannot depend on itself")

        _reject_cycles(self.nodes)
        return self

    def nodes_in_dependency_order(self) -> tuple[GraphNode, ...]:
        """Return nodes in a deterministic topological order."""
        by_id = {node.id: node for node in self.nodes}
        remaining = set(by_id)
        ordered: list[GraphNode] = []
        while remaining:
            ready = sorted(
                node_id
                for node_id in remaining
                if all(dep not in remaining for dep in by_id[node_id].depends_on)
            )
            if not ready:
                raise ValueError("task graph contains a dependency cycle")
            for node_id in ready:
                ordered.append(by_id[node_id])
                remaining.remove(node_id)
        return tuple(ordered)


def select_task_graph_template(
    *,
    risk_level: Literal["low", "medium", "high", "critical"],
    acceptance_criteria_count: int,
    labels: tuple[str, ...] = (),
) -> TaskGraphTemplate:
    """Select the smallest built-in graph that matches work risk."""
    normalized_labels = {label.lower() for label in labels}
    if risk_level in {"high", "critical"} or "security" in normalized_labels:
        return TaskGraphTemplate.SECURITY_SENSITIVE_DELIVERY
    if risk_level == "low" and acceptance_criteria_count <= 2:
        return TaskGraphTemplate.MICRO_DELIVERY
    return TaskGraphTemplate.STANDARD_DELIVERY


def build_task_graph(
    work_item: WorkItem,
    *,
    template: TaskGraphTemplate | None = None,
    graph_id: str | None = None,
    labels: tuple[str, ...] = (),
) -> TaskGraph:
    """Create a validated delivery graph for a work item."""
    selected = template or select_task_graph_template(
        risk_level=work_item.scope.risk_level,
        acceptance_criteria_count=len(work_item.acceptance_criteria),
        labels=labels,
    )
    return TaskGraph(
        id=graph_id or uuid.uuid4().hex,
        work_item_id=work_item.id,
        template=selected,
        nodes=_nodes_for_template(selected),
    )


def _nodes_for_template(template: TaskGraphTemplate) -> tuple[GraphNode, ...]:
    if template is TaskGraphTemplate.MICRO_DELIVERY:
        return (
            GraphNode(
                id="implement-qa",
                role=AgentRole.IMPLEMENTER,
                output_artifact_kind=ArtifactKind.HANDOFF,
                instructions="Implement the work item and include verification notes.",
            ),
        )
    if template is TaskGraphTemplate.STANDARD_DELIVERY:
        return (
            _planner_node(),
            _implementer_node(depends_on=("planner",)),
            _qa_node(depends_on=("implementer",), required_artifacts=(ArtifactKind.DIFF,)),
            _reviewer_node(depends_on=("qa",), required_artifacts=(ArtifactKind.TEST_RESULT,)),
        )
    return (
        _planner_node(),
        _implementer_node(depends_on=("planner",)),
        _qa_node(depends_on=("implementer",), required_artifacts=(ArtifactKind.DIFF,)),
        GraphNode(
            id="security",
            role=AgentRole.SECURITY,
            depends_on=("qa",),
            required_artifacts=(ArtifactKind.TEST_RESULT,),
            output_artifact_kind=ArtifactKind.CHECK_RESULT,
            instructions="Review side effects, secrets, auth, and unsafe operations.",
        ),
        _reviewer_node(depends_on=("security",), required_artifacts=(ArtifactKind.CHECK_RESULT,)),
    )


def _planner_node() -> GraphNode:
    return GraphNode(
        id="planner",
        role=AgentRole.PLANNER,
        output_artifact_kind=ArtifactKind.PLAN,
        instructions="Produce an execution plan with acceptance-criteria mapping.",
    )


def _implementer_node(*, depends_on: tuple[str, ...]) -> GraphNode:
    return GraphNode(
        id="implementer",
        role=AgentRole.IMPLEMENTER,
        depends_on=depends_on,
        required_artifacts=(ArtifactKind.PLAN,),
        output_artifact_kind=ArtifactKind.DIFF,
        instructions="Apply the plan and summarize the resulting diff.",
    )


def _qa_node(
    *,
    depends_on: tuple[str, ...],
    required_artifacts: tuple[ArtifactKind, ...],
) -> GraphNode:
    return GraphNode(
        id="qa",
        role=AgentRole.QA,
        depends_on=depends_on,
        required_artifacts=required_artifacts,
        output_artifact_kind=ArtifactKind.TEST_RESULT,
        instructions="Verify acceptance criteria and report command/test evidence.",
    )


def _reviewer_node(
    *,
    depends_on: tuple[str, ...],
    required_artifacts: tuple[ArtifactKind, ...],
) -> GraphNode:
    return GraphNode(
        id="reviewer",
        role=AgentRole.REVIEWER,
        depends_on=depends_on,
        required_artifacts=required_artifacts,
        output_artifact_kind=ArtifactKind.HANDOFF,
        instructions="Review scope, maintainability, and handoff readiness.",
    )


def _reject_blank_entries(name: str, values: tuple[str, ...]) -> None:
    if any(not value.strip() for value in values):
        raise ValueError(f"{name} entries must be non-empty")


def _reject_cycles(nodes: tuple[GraphNode, ...]) -> None:
    graph: dict[str, tuple[str, ...]] = {node.id: node.depends_on for node in nodes}
    colour: dict[str, int] = dict.fromkeys(graph, 0)
    for start in graph:
        if colour[start] != 0:
            continue
        stack: list[tuple[str, list[str]]] = [(start, list(graph[start]))]
        colour[start] = 1
        while stack:
            node_id, pending = stack[-1]
            if not pending:
                colour[node_id] = 2
                stack.pop()
                continue
            dep = pending.pop()
            state = colour.get(dep, 2)
            if state == 1:
                raise ValueError(f"task graph dependency cycle detected at {dep!r}")
            if state == 0:
                colour[dep] = 1
                stack.append((dep, list(graph[dep])))
