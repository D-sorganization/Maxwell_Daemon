"""Typed sub-agent task graph models and template selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from maxwell_daemon.contracts import require


class AgentRole(str, Enum):
    """Named role for a graph node."""

    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    QA = "qa"
    REVIEWER = "reviewer"
    SECURITY = "security"
    DOCS = "docs"
    PUBLISHER = "publisher"
    SPECIALIST = "specialist"


class HandoffArtifactKind(str, Enum):
    """Typed artifact kind produced by each node."""

    PLAN = "plan"
    IMPLEMENTATION_DIFF = "implementation_diff"
    QA_REPORT = "qa_report"
    REVIEW_REPORT = "review_report"
    SECURITY_REPORT = "security_report"
    DOCS_PATCH = "docs_patch"
    PUBLISHER_PAYLOAD = "publisher_payload"
    SPECIALIST_NOTE = "specialist_note"


class NodeRunStatus(str, Enum):
    """Execution status for graph runs."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(slots=True, frozen=True)
class TaskGraphNode:
    """Single node in a task graph."""

    id: str
    role: AgentRole
    output_artifact_kind: HandoffArtifactKind
    backend: str | None = None
    model: str | None = None
    depends_on: tuple[str, ...] = field(default=())
    required_artifacts: tuple[HandoffArtifactKind, ...] = field(default=())
    max_retries: int = 1


@dataclass(slots=True, frozen=True)
class TaskGraph:
    """Durable task graph definition."""

    id: str
    work_item_id: str
    nodes: tuple[TaskGraphNode, ...]
    status: NodeRunStatus = NodeRunStatus.QUEUED

    def validate(self) -> None:
        """Validate graph structure, dependencies, and typed handoff rules."""
        node_ids = tuple(node.id for node in self.nodes)
        _check_unique_ids(node_ids)
        node_by_id = {node.id: node for node in self.nodes}

        for node in self.nodes:
            require(
                0 <= node.max_retries <= 5, f"node {node.id!r} has invalid max_retries"
            )
            for dep in node.depends_on:
                require(
                    dep in node_by_id,
                    f"node {node.id!r} depends on unknown node {dep!r}",
                )

            produced_by_deps = {
                node_by_id[dep].output_artifact_kind
                for dep in node.depends_on
                if dep in node_by_id
            }
            for required in node.required_artifacts:
                require(
                    required in produced_by_deps,
                    f"node {node.id!r} requires missing artifact {required.value!r}",
                )

        _check_no_cycles(self.nodes)

    def ready_node_ids(self, completed_node_ids: set[str]) -> tuple[str, ...]:
        """Return node IDs whose dependencies are fully satisfied."""
        return tuple(
            node.id
            for node in self.nodes
            if node.id not in completed_node_ids
            and all(dep in completed_node_ids for dep in node.depends_on)
        )


class GraphTemplateKind(str, Enum):
    """Built-in template kind."""

    MICRO_DELIVERY = "micro-delivery"
    STANDARD_DELIVERY = "standard-delivery"
    SECURITY_SENSITIVE_DELIVERY = "security-sensitive-delivery"


@dataclass(slots=True, frozen=True)
class GraphTemplateSelectorInput:
    """Input used to choose a template."""

    acceptance_criteria_count: int
    risk_level: str
    has_security_labels: bool = False


@dataclass(slots=True, frozen=True)
class GraphTemplate:
    """A reusable graph template."""

    kind: GraphTemplateKind
    nodes: tuple[TaskGraphNode, ...]

    def instantiate(self, graph_id: str, work_item_id: str) -> TaskGraph:
        """Create a validated graph instance from this template."""
        graph = TaskGraph(id=graph_id, work_item_id=work_item_id, nodes=self.nodes)
        graph.validate()
        return graph


def select_template_kind(
    selector_input: GraphTemplateSelectorInput,
) -> GraphTemplateKind:
    """Select a built-in graph template."""
    risk = selector_input.risk_level.strip().lower()
    if selector_input.has_security_labels or risk in {"high", "critical"}:
        return GraphTemplateKind.SECURITY_SENSITIVE_DELIVERY
    if risk in {"low", "minor"} and selector_input.acceptance_criteria_count <= 2:
        return GraphTemplateKind.MICRO_DELIVERY
    return GraphTemplateKind.STANDARD_DELIVERY


def template_for_kind(kind: GraphTemplateKind) -> GraphTemplate:
    """Return a built-in template definition."""
    if kind is GraphTemplateKind.MICRO_DELIVERY:
        return GraphTemplate(
            kind=kind,
            nodes=(
                TaskGraphNode(
                    id="implementer",
                    role=AgentRole.IMPLEMENTER,
                    output_artifact_kind=HandoffArtifactKind.IMPLEMENTATION_DIFF,
                ),
                TaskGraphNode(
                    id="qa",
                    role=AgentRole.QA,
                    output_artifact_kind=HandoffArtifactKind.QA_REPORT,
                    depends_on=("implementer",),
                    required_artifacts=(HandoffArtifactKind.IMPLEMENTATION_DIFF,),
                ),
            ),
        )
    if kind is GraphTemplateKind.SECURITY_SENSITIVE_DELIVERY:
        return GraphTemplate(
            kind=kind,
            nodes=(
                TaskGraphNode(
                    id="planner",
                    role=AgentRole.PLANNER,
                    output_artifact_kind=HandoffArtifactKind.PLAN,
                ),
                TaskGraphNode(
                    id="implementer",
                    role=AgentRole.IMPLEMENTER,
                    output_artifact_kind=HandoffArtifactKind.IMPLEMENTATION_DIFF,
                    depends_on=("planner",),
                    required_artifacts=(HandoffArtifactKind.PLAN,),
                ),
                TaskGraphNode(
                    id="qa",
                    role=AgentRole.QA,
                    output_artifact_kind=HandoffArtifactKind.QA_REPORT,
                    depends_on=("implementer",),
                    required_artifacts=(HandoffArtifactKind.IMPLEMENTATION_DIFF,),
                ),
                TaskGraphNode(
                    id="security",
                    role=AgentRole.SECURITY,
                    output_artifact_kind=HandoffArtifactKind.SECURITY_REPORT,
                    depends_on=("qa",),
                    required_artifacts=(HandoffArtifactKind.QA_REPORT,),
                ),
                TaskGraphNode(
                    id="reviewer",
                    role=AgentRole.REVIEWER,
                    output_artifact_kind=HandoffArtifactKind.REVIEW_REPORT,
                    depends_on=("security",),
                    required_artifacts=(HandoffArtifactKind.SECURITY_REPORT,),
                ),
            ),
        )
    return GraphTemplate(
        kind=GraphTemplateKind.STANDARD_DELIVERY,
        nodes=(
            TaskGraphNode(
                id="planner",
                role=AgentRole.PLANNER,
                output_artifact_kind=HandoffArtifactKind.PLAN,
            ),
            TaskGraphNode(
                id="implementer",
                role=AgentRole.IMPLEMENTER,
                output_artifact_kind=HandoffArtifactKind.IMPLEMENTATION_DIFF,
                depends_on=("planner",),
                required_artifacts=(HandoffArtifactKind.PLAN,),
            ),
            TaskGraphNode(
                id="qa",
                role=AgentRole.QA,
                output_artifact_kind=HandoffArtifactKind.QA_REPORT,
                depends_on=("implementer",),
                required_artifacts=(HandoffArtifactKind.IMPLEMENTATION_DIFF,),
            ),
            TaskGraphNode(
                id="reviewer",
                role=AgentRole.REVIEWER,
                output_artifact_kind=HandoffArtifactKind.REVIEW_REPORT,
                depends_on=("qa",),
                required_artifacts=(HandoffArtifactKind.QA_REPORT,),
            ),
        ),
    )


def _check_unique_ids(node_ids: tuple[str, ...]) -> None:
    seen: set[str] = set()
    for node_id in node_ids:
        require(node_id not in seen, f"duplicate node id {node_id!r}")
        seen.add(node_id)


_WHITE = 0
_GREY = 1
_BLACK = 2


def _check_no_cycles(nodes: tuple[TaskGraphNode, ...]) -> None:
    graph: dict[str, tuple[str, ...]] = {node.id: node.depends_on for node in nodes}
    color: dict[str, int] = dict.fromkeys(graph, _WHITE)

    for start in graph:
        if color[start] != _WHITE:
            continue
        stack: list[tuple[str, list[str]]] = [(start, list(graph[start]))]
        color[start] = _GREY
        while stack:
            node_id, pending = stack[-1]
            if not pending:
                color[node_id] = _BLACK
                stack.pop()
                continue
            nxt = pending.pop()
            if nxt not in color:
                continue
            nxt_color = color[nxt]
            if nxt_color == _GREY:
                require(False, f"task graph cycle detected at {nxt!r}")
            if nxt_color == _WHITE:
                color[nxt] = _GREY
                stack.append((nxt, list(graph[nxt])))
