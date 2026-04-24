from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from maxwell_daemon.model_routing.models import ActionRisk, Capability


class ExpectedLatency(str, Enum):
    INTERACTIVE = "interactive"
    BATCH = "batch"


@dataclass(slots=True, frozen=True)
class TaskSignature:
    """Features extracted from a work item to guide model routing."""

    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    required_capabilities: set[Capability] = field(default_factory=set)
    action_risk: ActionRisk = ActionRisk.READ_ONLY
    expected_latency: ExpectedLatency = ExpectedLatency.BATCH
    language_hint: str | None = None
