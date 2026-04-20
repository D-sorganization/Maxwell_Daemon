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
from maxwell_daemon.director.types import Decomposition, Epic, Story, Task

__all__ = [
    "CreateIssueFn",
    "DecomposerFn",
    "Decomposition",
    "Director",
    "Epic",
    "PlanDiff",
    "Story",
    "Task",
    "diff_plans",
    "materialise_plan",
]
