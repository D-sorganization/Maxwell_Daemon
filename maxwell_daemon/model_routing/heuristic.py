"""Heuristic-based model routing by task complexity, capabilities, and latency.

This module provides ``route_model`` - a lightweight function that picks a
model name suitable for a task without requiring a full ``ModelProfile``
registry.  It is intended as a fast-path for callers that know what they need
but do not have a loaded profile list.

For policy-driven, benchmark-aware routing across a registered set of profiles
see :mod:`maxwell_daemon.model_routing.router`.

Routing heuristics
------------------
Complexity tiers (0-10):
  * **low  (0-3)**: haiku / smallest available - fast and cheap.
  * **mid  (4-6)**: sonnet / mid-tier - good balance.
  * **high (7-10)**: opus / frontier - maximum quality.

Required capabilities shift the selection upward when the baseline model is
known to lack a capability:
  * ``"vision"`` - requires a multimodal model.
  * ``"code"`` - prefers coding-optimised models.
  * ``"long_context"`` - requires a model with >=100K context.
  * ``"tool_use"`` - requires a model with function-calling support.

Latency tiers:
  * ``"fast"`` - prefer the smallest model that meets capability requirements.
  * ``"balanced"`` (default) - standard heuristic.
  * ``"quality"`` - prefer the largest model regardless of complexity.

Provider preference:
  Use ``preferred_provider`` to select between ``"anthropic"`` (default),
  ``"openai"``, or ``"ollama"`` (free local models).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "ModelRecommendation",
    "route_model",
]

LatencyTier = Literal["fast", "balanced", "quality"]
Provider = Literal["anthropic", "openai", "ollama"]

# ---------------------------------------------------------------------------
# Model catalogues - (model_name, capability_tags, complexity_min)
# Each entry: (name, capabilities_set, min_complexity_for_default_selection)
# ---------------------------------------------------------------------------

_ANTHROPIC_MODELS: list[tuple[str, frozenset[str], int]] = [
    ("claude-haiku-4-5", frozenset({"tool_use", "code"}), 0),
    ("claude-sonnet-4-6", frozenset({"tool_use", "code", "vision", "long_context"}), 4),
    ("claude-opus-4-7", frozenset({"tool_use", "code", "vision", "long_context"}), 7),
]

_OPENAI_MODELS: list[tuple[str, frozenset[str], int]] = [
    ("gpt-4o-mini", frozenset({"tool_use", "code", "vision"}), 0),
    ("gpt-4o", frozenset({"tool_use", "code", "vision", "long_context"}), 4),
    ("o1", frozenset({"tool_use", "code", "long_context"}), 7),
]

_OLLAMA_MODELS: list[tuple[str, frozenset[str], int]] = [
    # sensible defaults; users may have different models installed
    ("qwen2.5-coder:7b", frozenset({"tool_use", "code"}), 0),
    ("llama3.1:8b", frozenset({"tool_use", "code"}), 0),
    ("llama3.1:70b", frozenset({"tool_use", "code", "long_context"}), 4),
]

_PROVIDER_CATALOGUES: dict[str, list[tuple[str, frozenset[str], int]]] = {
    "anthropic": _ANTHROPIC_MODELS,
    "openai": _OPENAI_MODELS,
    "ollama": _OLLAMA_MODELS,
}


@dataclass(slots=True, frozen=True)
class ModelRecommendation:
    """Result of a heuristic routing decision.

    Attributes
    ----------
    model:
        Recommended model name.
    provider:
        Provider for the recommended model.
    complexity_tier:
        ``"low"``, ``"mid"``, or ``"high"`` based on *task_complexity*.
    capabilities_matched:
        Subset of *required_capabilities* the chosen model satisfies.
    capabilities_missing:
        Required capabilities not supported by the chosen model (may be
        empty if the model covers all requirements).
    latency_tier:
        The latency preference passed by the caller.
    rationale:
        Human-readable explanation of the routing decision.
    """

    model: str
    provider: str
    complexity_tier: Literal["low", "mid", "high"]
    capabilities_matched: frozenset[str]
    capabilities_missing: frozenset[str]
    latency_tier: LatencyTier
    rationale: str = field(default="")


def _complexity_tier(task_complexity: float) -> Literal["low", "mid", "high"]:
    if task_complexity <= 3:
        return "low"
    if task_complexity <= 6:
        return "mid"
    return "high"


def _min_complexity_for_capabilities(
    required: frozenset[str],
    catalogue: list[tuple[str, frozenset[str], int]],
) -> int:
    """Return the minimum complexity threshold that covers all required capabilities.

    Returns 0 when *required* is empty (no capability bump needed).
    Returns 0 when the first model already covers all requirements.
    Returns the min_c of the smallest model that satisfies all requirements.
    Falls back to the highest min_c if no single model covers everything.
    """
    if not required:
        return 0
    for _, caps, min_complexity in catalogue:
        if required.issubset(caps):
            return min_complexity
    # No single model covers everything; return the highest tier.
    return 7


def _select_balanced(
    required: frozenset[str],
    effective_complexity: float,
    effective_tier: Literal["low", "mid", "high"],
    catalogue: list[tuple[str, frozenset[str], int]],
) -> tuple[str, frozenset[str]]:
    """Select the best model for balanced latency tier."""
    # Pick the largest model whose min_c <= effective_complexity.
    best_entry = catalogue[0]
    for entry in catalogue:
        if entry[2] <= effective_complexity:
            best_entry = entry

    if not required:
        return best_entry[0], best_entry[1]

    # If capabilities are required, check whether a model within the tier
    # ceiling covers more of them than the complexity-based pick.
    _tier_max: dict[Literal["low", "mid", "high"], int] = {"low": 3, "mid": 6, "high": 10}
    ceiling = _tier_max[effective_tier]
    base_score = len(best_entry[1] & required)
    selected_model, best_caps = best_entry[0], best_entry[1]

    for name, caps, min_c in catalogue:
        if min_c > ceiling:
            continue
        score = len(caps & required)
        if score > base_score:
            selected_model = name
            best_caps = caps
            base_score = score

    return selected_model, best_caps


def route_model(
    task_complexity: float,
    required_capabilities: frozenset[str] | set[str] | None = None,
    *,
    latency_tier: LatencyTier = "balanced",
    preferred_provider: Provider = "anthropic",
) -> ModelRecommendation:
    """Select the optimal model based on task characteristics.

    Parameters
    ----------
    task_complexity:
        Float in [0, 10] where 0 = trivial and 10 = hardest possible.
        Values outside this range are clamped silently.
    required_capabilities:
        Set of capability strings the chosen model must support.
        Recognised values: ``"vision"``, ``"code"``, ``"long_context"``,
        ``"tool_use"``.  Unknown capabilities are noted in the rationale.
    latency_tier:
        ``"fast"`` - pick the smallest qualifying model.
        ``"balanced"`` (default) - apply the complexity heuristic.
        ``"quality"`` - always pick the largest model.
    preferred_provider:
        ``"anthropic"`` (default), ``"openai"``, or ``"ollama"``.

    Returns
    -------
    ModelRecommendation
        Routing decision with model name, provider, and rationale.
    """
    required: frozenset[str] = frozenset(required_capabilities or set())
    clamped = max(0.0, min(10.0, float(task_complexity)))
    tier = _complexity_tier(clamped)

    catalogue = _PROVIDER_CATALOGUES.get(preferred_provider, _ANTHROPIC_MODELS)

    if latency_tier == "fast":
        effective_complexity = 0.0
    elif latency_tier == "quality":
        effective_complexity = 10.0
    else:
        # capability bump: if required caps demand a higher tier, bump up.
        cap_min = _min_complexity_for_capabilities(required, catalogue)
        effective_complexity = max(clamped, float(cap_min))

    effective_tier = _complexity_tier(effective_complexity)

    if latency_tier == "fast":
        selected_model, best_caps = catalogue[0][0], catalogue[0][1]
    elif latency_tier == "quality":
        selected_model, best_caps = catalogue[-1][0], catalogue[-1][1]
    else:
        selected_model, best_caps = _select_balanced(
            required, effective_complexity, effective_tier, catalogue
        )

    caps_matched = best_caps & required
    caps_missing = required - best_caps

    notes: list[str] = []
    if caps_missing:
        notes.append(f"missing capabilities: {', '.join(sorted(caps_missing))}")
    if latency_tier == "fast":
        notes.append("latency=fast overrides complexity heuristic")
    elif latency_tier == "quality":
        notes.append("latency=quality selects largest model")

    rationale = (
        f"complexity={clamped:.1f} ({tier}), provider={preferred_provider}, latency={latency_tier}"
    )
    if notes:
        rationale += "; " + "; ".join(notes)

    return ModelRecommendation(
        model=selected_model,
        provider=preferred_provider,
        complexity_tier=tier,
        capabilities_matched=caps_matched,
        capabilities_missing=caps_missing,
        latency_tier=latency_tier,
        rationale=rationale,
    )
