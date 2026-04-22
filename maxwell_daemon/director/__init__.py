"""Vision-to-tasks Director.

A Director takes a one-paragraph *vision* and produces a validated Epic →
Story → Task plan, which can then be materialised as GitHub issues or diffed
against a previous plan for reconciliation.

The LLM call is pluggable: the Director only knows about a ``DecomposerFn``
coroutine, so the same orchestration works with any backend.
"""

from maxwell_daemon.director.decomposer import DecomposerFn, Director
from maxwell_daemon.director.issue_adapter import CreateIssueFn, materialise_plan
from maxwell_daemon.director.reconciler import PlanDiff, diff_plans
from maxwell_daemon.director.task_graph_runner import (
    GraphExecutionContext,
    GraphExecutionResult,
    GraphNodeExecutor,
    GraphNodeOutput,
    GraphRunner,
)
from maxwell_daemon.director.task_graphs import (
    AgentRole,
    GraphNode,
    GraphStatus,
    NodeRun,
    NodeRunStatus,
    TaskGraph,
    TaskGraphTemplate,
    build_task_graph,
    select_task_graph_template,
)
from maxwell_daemon.director.types import Decomposition, Epic, Story, Task

__all__ = [
    "AgentRole",
    "CreateIssueFn",
    "DecomposerFn",
    "Decomposition",
    "Director",
    "Epic",
    "GraphExecutionContext",
    "GraphExecutionResult",
    "GraphNode",
    "GraphNodeExecutor",
    "GraphNodeOutput",
    "GraphRunner",
    "GraphStatus",
    "NodeRun",
    "NodeRunStatus",
    "PlanDiff",
    "Story",
    "Task",
    "TaskGraph",
    "TaskGraphTemplate",
    "build_task_graph",
    "diff_plans",
    "materialise_plan",
    "select_task_graph_template",
]
