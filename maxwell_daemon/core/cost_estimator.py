"""Pre-flight cost estimation and workspace cost rollup.

Provides:
- ``estimate_task_cost(provider, model, messages)`` — estimates USD cost before running a task
- ``workspace_cost_rollup(ledger, workspace_id)`` — aggregates cost for a workspace
- ``CostEstimate`` — typed result dataclass

Estimation accuracy
-------------------
Token counts are approximated from message character lengths using a 4 chars
per token heuristic (reasonable for English prose).  Actual token counts vary
by model tokenizer.  The estimate is intentionally conservative on the output
side: it uses a configurable ``expected_completion_ratio`` (default 0.5 —
assume the model outputs half as many tokens as it received).

The pricing table is sourced from ``maxwell_daemon.backends.pricing`` so there
is a single source of truth for costs across estimation and actual billing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from maxwell_daemon.backends.pricing import get_rates, is_free_provider
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "CostEstimate",
    "WorkspaceCostRollup",
    "estimate_task_cost",
    "workspace_cost_rollup",
]

# Characters-per-token approximation.  GPT-4 / Claude tokenizers average
# ~4 chars per token for English text; we use this for estimation only.
_CHARS_PER_TOKEN: float = 4.0


@dataclass(slots=True, frozen=True)
class CostEstimate:
    """Pre-flight cost estimate for a task.

    Attributes
    ----------
    provider:
        Backend name (e.g. ``"claude"``, ``"openai"``).
    model:
        Model identifier string.
    estimated_prompt_tokens:
        Token count estimated from input messages.
    estimated_completion_tokens:
        Projected output token count.
    estimated_cost_usd:
        Total estimated cost in US dollars.
    is_free:
        ``True`` for local / free providers such as Ollama.
    note:
        Human-readable caveat about estimation accuracy.
    """

    provider: str
    model: str
    estimated_prompt_tokens: int
    estimated_completion_tokens: int
    estimated_cost_usd: float
    is_free: bool
    note: str = "Estimated using 4 chars/token heuristic; actual cost may vary."


@dataclass(slots=True, frozen=True)
class WorkspaceCostRollup:
    """Aggregated cost for a workspace over a time window."""

    workspace_id: str
    total_cost_usd: float
    call_count: int
    period_start: datetime
    period_end: datetime


def _count_message_chars(messages: list[dict[str, Any]] | list[Any]) -> int:
    """Sum character lengths across all message content fields."""
    total = 0
    for msg in messages:
        if isinstance(msg, dict):
            content = msg.get("content") or ""
        elif hasattr(msg, "content"):
            content = msg.content or ""
        else:
            content = str(msg)
        total += len(content)
    return total


def estimate_task_cost(
    provider: str,
    model: str,
    messages: list[dict[str, Any]] | list[Any],
    *,
    expected_completion_ratio: float = 0.5,
) -> CostEstimate:
    """Estimate the USD cost of running *messages* through *model*.

    Parameters
    ----------
    provider:
        Backend name as registered in the pricing table (e.g. ``"claude"``).
    model:
        Model identifier (e.g. ``"claude-sonnet-4-6"``).
    messages:
        List of message dicts (``{"role": ..., "content": ...}``) or objects
        with a ``.content`` attribute.
    expected_completion_ratio:
        Fraction of prompt tokens expected as completion tokens.  Default 0.5
        (model outputs half the input length).  Adjust for your workload.

    Returns
    -------
    CostEstimate
        Typed estimate with token counts and USD cost.
    """
    if is_free_provider(provider):
        return CostEstimate(
            provider=provider,
            model=model,
            estimated_prompt_tokens=0,
            estimated_completion_tokens=0,
            estimated_cost_usd=0.0,
            is_free=True,
            note=f"Provider {provider!r} is free (local inference).",
        )

    total_chars = _count_message_chars(messages)
    prompt_tokens = max(1, int(total_chars / _CHARS_PER_TOKEN))
    completion_tokens = max(1, int(prompt_tokens * expected_completion_ratio))

    price_in, price_out = get_rates(provider, model)
    cost = (prompt_tokens * price_in / 1_000_000) + (completion_tokens * price_out / 1_000_000)

    log.debug(
        "cost_estimate",
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=cost,
    )

    return CostEstimate(
        provider=provider,
        model=model,
        estimated_prompt_tokens=prompt_tokens,
        estimated_completion_tokens=completion_tokens,
        estimated_cost_usd=cost,
        is_free=False,
    )


def workspace_cost_rollup(
    ledger: Any,
    workspace_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> WorkspaceCostRollup:
    """Return aggregated cost for *workspace_id* from the ledger.

    The ledger does not currently store workspace-level totals, so this
    function falls through to month-to-date totals as a reasonable proxy.
    When workspace tagging is added to the ledger schema, this function
    should be updated to filter by ``workspace_id`` directly.

    Parameters
    ----------
    ledger:
        A ``CostLedger`` instance.
    workspace_id:
        Workspace identifier (used as a label in the returned rollup).
    since:
        Start of the rollup window.  Defaults to the start of the current
        calendar month.
    until:
        End of the rollup window.  Defaults to now.
    """
    now = until or datetime.now(timezone.utc)
    start = since or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total = ledger.total_since(start)

    return WorkspaceCostRollup(
        workspace_id=workspace_id,
        total_cost_usd=total,
        call_count=0,  # full call tracking requires schema extension
        period_start=start,
        period_end=now,
    )
