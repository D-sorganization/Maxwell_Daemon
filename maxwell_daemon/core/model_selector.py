"""Route tasks to the cheapest model that fits the job.

Heuristics-only for now — no LLM-based classifier — so the decision is fast
and deterministic. Good-enough tiering: short cosmetic issues → cheap model;
long ambiguous / security-labelled issues → expensive model.

Config shape::

    backends:
      claude:
        type: claude
        model: claude-sonnet-4-6   # fallback if tier_map missing an entry
        tier_map:
          simple: claude-haiku-4-5
          moderate: claude-sonnet-4-6
          complex: claude-opus-4-7
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "ComplexityScore",
    "ModelSelection",
    "ModelTier",
    "pick_model_for_issue",
    "score_issue",
    "select_model",
]


class ModelTier(Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


@dataclass(slots=True, frozen=True)
class ComplexityScore:
    tier: ModelTier
    factors: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ModelSelection:
    tier: ModelTier
    model: str
    factors: dict[str, Any]


_COMPLEX_LABELS = frozenset(
    {"p0", "critical", "security", "vulnerability", "regression", "data-loss"}
)
_MODERATE_LABELS = frozenset({"bug", "enhancement", "feature", "refactor", "perf"})
_SIMPLE_LABELS = frozenset({"docs", "typo", "readme", "good-first-issue", "nit", "cosmetic"})
_CODE_BLOCK_RE = re.compile(r"```")


def score_issue(
    *,
    title: str,
    body: str,
    labels: list[str],
) -> ComplexityScore:
    """Classify an issue's complexity by cheap structural heuristics."""
    label_set = {label.lower() for label in labels}
    body_length = len(body)
    code_blocks = len(_CODE_BLOCK_RE.findall(body)) // 2
    title_lower = title.lower()

    factors: dict[str, Any] = {
        "body_length": body_length,
        "code_blocks": code_blocks,
        "labels": sorted(label_set),
    }

    # COMPLEX wins first — severity labels override everything else.
    if label_set & _COMPLEX_LABELS:
        return ComplexityScore(tier=ModelTier.COMPLEX, factors=factors)

    # SIMPLE: docs/typo labels, or very short body with no code, or title
    # that screams cosmetic. A moderate-ish label (bug/feature/...) prevents
    # the downgrade-to-simple heuristic — even a one-line bug report can hide
    # a real bug.
    moderate_labelled = bool(label_set & _MODERATE_LABELS)
    if label_set & _SIMPLE_LABELS:
        return ComplexityScore(tier=ModelTier.SIMPLE, factors=factors)
    if not moderate_labelled:
        if (
            body_length < 200
            and code_blocks == 0
            and any(kw in title_lower for kw in ("typo", "docs", "readme", "comment"))
        ):
            return ComplexityScore(tier=ModelTier.SIMPLE, factors=factors)
        if body_length < 120 and code_blocks == 0:
            return ComplexityScore(tier=ModelTier.SIMPLE, factors=factors)

    # MODERATE: most day-to-day work.
    # COMPLEX escalation: long body with multiple code blocks suggests a
    # hairy repro / stack trace worth paying for the smarter model.
    if body_length > 1500 or code_blocks >= 2:
        return ComplexityScore(tier=ModelTier.COMPLEX, factors=factors)

    return ComplexityScore(tier=ModelTier.MODERATE, factors=factors)


def select_model(
    tier: ModelTier,
    tier_map: dict[str, str],
    *,
    fallback: str | None = None,
) -> str:
    """Resolve a tier to a model name. Falls back through moderate → simple
    → the configured fallback, so a partial tier_map still works.
    """
    # Preferred order per requested tier — pick the closest cheaper option
    # when the exact one is missing, then fall back.
    chain: dict[ModelTier, list[str]] = {
        ModelTier.SIMPLE: ["simple", "moderate", "complex"],
        ModelTier.MODERATE: ["moderate", "simple", "complex"],
        ModelTier.COMPLEX: ["complex", "moderate", "simple"],
    }
    for candidate in chain[tier]:
        if candidate in tier_map:
            return tier_map[candidate]
    if fallback is not None:
        return fallback
    raise ValueError(f"No model in tier_map for {tier.value!r} and no fallback provided")


def pick_model_for_issue(
    *,
    title: str,
    body: str,
    labels: list[str],
    tier_map: dict[str, str],
    fallback: str | None = None,
) -> ModelSelection:
    """One-shot: score the issue, resolve the tier, return the model to use."""
    score = score_issue(title=title, body=body, labels=labels)
    model = select_model(score.tier, tier_map, fallback=fallback)
    return ModelSelection(tier=score.tier, model=model, factors=score.factors)
