"""Named sub-agent task graph primitives.

First production slice for issue #286:
- Typed node roles and artifact handoff kinds.
- Task graph DAG validation and dependency checks.
- Built-in graph templates with simple selection policy.
"""

from .types import (
    AgentRole,
    GraphTemplate,
    GraphTemplateKind,
    GraphTemplateSelectorInput,
    HandoffArtifactKind,
    NodeRunStatus,
    TaskGraph,
    TaskGraphNode,
    select_template_kind,
)

__all__ = [
    "AgentRole",
    "GraphTemplate",
    "GraphTemplateKind",
    "GraphTemplateSelectorInput",
    "HandoffArtifactKind",
    "NodeRunStatus",
    "TaskGraph",
    "TaskGraphNode",
    "select_template_kind",
]
