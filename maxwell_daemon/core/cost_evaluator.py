"""Model selection and token budgeting logic.

Ensures that tasks are routed to the most cost-effective model that can
reasonably handle the task's complexity, while respecting the monthly
and per-task budgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from maxwell_daemon.logging import get_logger

if TYPE_CHECKING:
    from maxwell_daemon.daemon.runner import ConfigSnapshot, Task

log = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class ModelChoice:
    model: str
    confidence_pct: int
    reasoning: str


@dataclass(slots=True, frozen=True)
class TokenBudgetInfo:
    budget_remaining_usd: float
    est_context_cost: float
    est_call_cost: float
    safe_allocation: float


class CostEvaluator:
    def __init__(self, snapshot: ConfigSnapshot) -> None:
        self._snapshot = snapshot

    def choose_model(self, task: Task) -> ModelChoice:
        """Pick the best model for the task based on complexity and budget.

        Currently focused on Anthropic/OpenAI tiering:
        - simple   -> Haiku / GPT-4o-mini
        - moderate -> Sonnet / GPT-4o
        - complex  -> Opus / o1
        """
        # 1. Check if model is overridden in task
        if task.model:
            return ModelChoice(
                model=task.model, confidence_pct=100, reasoning="Explicit user override"
            )

        # 2. Get backend and tier map
        # We use the router to find the intended backend for this repo.
        decision = self._snapshot.router.route(repo=task.repo)
        backend_name = decision.backend_name
        backend_cfg = self._snapshot.router._backend_config(backend_name)

        if not backend_cfg or not backend_cfg.tier_map:
            return ModelChoice(
                model=decision.model,
                confidence_pct=100,
                reasoning=f"Using router default for backend {backend_name} (no tier map)",
            )

        # 3. Estimate complexity
        complexity = self._estimate_complexity(task)

        # 4. Check budget and downgrade model if necessary
        budget_info = self.token_budget_for_task(
            task,
            decision.backend_name,
            backend_cfg.tier_map.get(complexity) or decision.model,
        )

        # Agent must choose model based on budget
        # if the budget is very tight, we must downgrade
        if budget_info.safe_allocation < budget_info.est_call_cost:
            # We don't have enough budget for the preferred model, downgrade to simple
            if complexity == "complex":
                complexity = "moderate"
                budget_info = self.token_budget_for_task(
                    task,
                    decision.backend_name,
                    backend_cfg.tier_map.get(complexity) or decision.model,
                )
            if budget_info.safe_allocation < budget_info.est_call_cost and complexity == "moderate":
                complexity = "simple"
                budget_info = self.token_budget_for_task(
                    task,
                    decision.backend_name,
                    backend_cfg.tier_map.get(complexity) or decision.model,
                )

        tier_model = backend_cfg.tier_map.get(complexity)

        if not tier_model:
            model_chosen = decision.model
            reasoning = f"Tier {complexity} not in tier_map, using default"
            confidence_pct = 100
        else:
            model_chosen = tier_model
            reasoning = f"Selected {complexity} tier based on prompt length ({len(task.prompt)} chars) and budget (${budget_info.safe_allocation:.2f})"
            confidence_pct = 85

        # Emit metric: token_budget_allocation{task_id, budget_remaining, model_chosen}
        from maxwell_daemon.metrics import MAXWELL_TOKEN_BUDGET_ALLOCATION

        MAXWELL_TOKEN_BUDGET_ALLOCATION.labels(
            task_id=task.id,
            budget_remaining=f"{budget_info.budget_remaining_usd:.2f}",
            model_chosen=model_chosen,
        ).set(budget_info.safe_allocation)

        return ModelChoice(model=model_chosen, confidence_pct=confidence_pct, reasoning=reasoning)

    def _estimate_complexity(self, task: Task) -> str:
        # Simple heuristic for now: prompt length and keywords
        text = task.prompt.lower()

        complex_keywords = [
            "refactor",
            "architect",
            "optimization",
            "complex",
            "debug",
            "performance",
        ]
        if any(kw in text for kw in complex_keywords) or len(task.prompt) > 20000:
            return "complex"

        simple_keywords = ["summarize", "review", "check", "typo", "formatting"]
        if any(kw in text for kw in simple_keywords) or len(task.prompt) < 500:
            return "simple"

        return "moderate"

    def token_budget_for_task(self, task: Task, provider: str, model: str) -> TokenBudgetInfo:
        """Return the TokenBudgetInfo containing the maximum USD budget allocated for this task.

        Takes the minimum of:
        1. Remaining monthly budget
        2. Per-task limit from config
        3. A conservative fraction of total budget if remaining is high
        """
        check = self._snapshot.budget.check()
        limit = self._snapshot.config.budget.monthly_limit_usd
        per_task_limit = self._snapshot.config.budget.per_task_limit_usd

        remaining = (limit - check.spent_usd) if limit else 100.0  # Default to $100 if no limit

        candidates = [remaining]
        if per_task_limit:
            candidates.append(per_task_limit)

        safe_allocation = max(0.0, min(candidates))

        from maxwell_daemon.backends.pricing import get_rates

        price_in, price_out = get_rates(provider, model)

        # Estimate context cost (repo schema + convo history)
        # Assuming repo schema ~2KB (~500 tokens) + convo history (up to prompt len / 4 tokens)
        est_context_tokens = 500 + (len(task.prompt) // 4)
        est_context_cost = est_context_tokens * price_in / 1_000_000

        # Estimated cost of single LLM call
        est_call_cost = est_context_cost + (
            1000 * price_out / 1_000_000
        )  # Assuming 1000 completion tokens

        return TokenBudgetInfo(
            budget_remaining_usd=remaining,
            est_context_cost=est_context_cost,
            est_call_cost=est_call_cost,
            safe_allocation=safe_allocation,
        )
