from unittest.mock import MagicMock

from pytest import MonkeyPatch

from maxwell_daemon.core.cost_evaluator import CostEvaluator


def test_estimate_complexity() -> None:
    evaluator = CostEvaluator(snapshot=MagicMock())

    # Test simple
    task = MagicMock()
    task.prompt = "Please summarize this."
    assert evaluator._estimate_complexity(task) == "simple"

    # Test complex
    task.prompt = "We need to refactor and architect the system for optimization."
    assert evaluator._estimate_complexity(task) == "complex"

    # Test moderate
    task.prompt = "Write a basic hello world script." * 20
    assert evaluator._estimate_complexity(task) == "moderate"


def test_choose_model_explicit_override() -> None:
    evaluator = CostEvaluator(snapshot=MagicMock())
    task = MagicMock()
    task.model = "gpt-4"

    choice = evaluator.choose_model(task)
    assert choice.model == "gpt-4"
    assert choice.confidence_pct == 100
    assert choice.reasoning == "Explicit user override"


def test_token_budget_for_task(monkeypatch: MonkeyPatch) -> None:
    # Mock get_rates to return fixed pricing
    import maxwell_daemon.backends.pricing as pricing

    def mock_get_rates(provider: str, model: str) -> tuple[float, float]:
        return (10.0, 20.0)

    monkeypatch.setattr(pricing, "get_rates", mock_get_rates)

    snapshot = MagicMock()
    snapshot.budget.check().spent_usd = 20.0
    snapshot.config.budget.monthly_limit_usd = 100.0
    snapshot.config.budget.per_task_limit_usd = 5.0

    evaluator = CostEvaluator(snapshot=snapshot)
    task = MagicMock()
    task.prompt = "test" * 250  # 1000 chars -> 250 tokens

    budget_info = evaluator.token_budget_for_task(task, "test_provider", "test_model")
    assert budget_info.budget_remaining_usd == 80.0
    assert budget_info.safe_allocation == 5.0
