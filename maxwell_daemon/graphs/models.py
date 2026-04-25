"""Models for named sub-agent task graphs."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentRole(str, Enum):
    """Named role assigned to a graph node."""

    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    QA = "qa"
    REVIEWER = "reviewer"
    SECURITY_REVIEWER = "security-reviewer"


class GraphStatus(str, Enum):
    """Lifecycle state for a graph definition or planned run."""

    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class NodeRunStatus(str, Enum):
    """Lifecycle state for one node run."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class GraphNode(BaseModel):
    """One named sub-agent task in a graph."""

    model_config = ConfigDict(use_enum_values=False, frozen=True)

    id: str = Field(..., min_length=1)
    role: AgentRole
    description: str = Field(..., min_length=1)
    depends_on: tuple[str, ...] = ()
    max_retries: int = Field(default=1, ge=0, le=5)
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    @field_validator("depends_on", "inputs", "outputs")
    @classmethod
    def _entries_must_be_non_blank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("entries must be non-empty")
        return values

    @field_validator("depends_on")
    @classmethod
    def _dependencies_must_be_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("depends_on entries must be unique")
        return values


class NodeRun(BaseModel):
    """State captured for one planned or completed node run."""

    model_config = ConfigDict(use_enum_values=False)

    graph_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    status: NodeRunStatus = NodeRunStatus.QUEUED
    attempts: int = Field(default=0, ge=0)
    output_ref: str | None = Field(default=None, min_length=1)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskGraph(BaseModel):
    """Validated DAG of named sub-agent tasks."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    nodes: tuple[GraphNode, ...] = Field(..., min_length=1)
    status: GraphStatus = GraphStatus.PLANNED
    created_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def _validate_dag(self) -> TaskGraph:
        node_ids = tuple(node.id for node in self.nodes)
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate node id in task graph")

        known_ids = set(node_ids)
        for node in self.nodes:
            for dependency in node.depends_on:
                if dependency == node.id:
                    raise ValueError(f"node {node.id!r} cannot depend on itself")
                if dependency not in known_ids:
                    raise ValueError(f"unknown dependency {dependency!r} for node {node.id!r}")

        _reject_cycles(self.nodes)
        return self

    def node_by_id(self, node_id: str) -> GraphNode:
        """Return a node by id."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def nodes_in_dependency_order(self) -> tuple[GraphNode, ...]:
        """Return nodes in deterministic topological order."""
        by_id = {node.id: node for node in self.nodes}
        remaining = set(by_id)
        ordered: list[GraphNode] = []

        while remaining:
            ready = sorted(
                node_id
                for node_id in remaining
                if all(dependency not in remaining for dependency in by_id[node_id].depends_on)
            )
            if not ready:
                raise ValueError("task graph contains a dependency cycle")
            for node_id in ready:
                ordered.append(by_id[node_id])
                remaining.remove(node_id)

        return tuple(ordered)


def _reject_cycles(nodes: tuple[GraphNode, ...]) -> None:
    dependencies_by_id = {node.id: node.depends_on for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise ValueError(f"cycle detected at node {node_id!r}")

        visiting.add(node_id)
        for dependency in dependencies_by_id[node_id]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node in nodes:
        visit(node.id)
