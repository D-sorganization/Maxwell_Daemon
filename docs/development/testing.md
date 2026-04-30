# Testing & TDD Guide

## Overview

Maxwell Daemon maintains a **91.26% code coverage floor** with strict TDD practices. Every feature should be driven by tests that fail first, then pass after implementation.

## Test Coverage Standards

**Current Floor:** 91.26% (enforced by `scripts/check_coverage_floor.py`)

**Philosophy:** Never lower the floor. If coverage drops, add tests—don't relax the gate.

**Coverage Ratcheting:** The floor automatically increases when tests push coverage higher. Use `scripts/check_coverage_floor.py --update` to ratchet up after adding tests.

**Exclusions from Coverage:**
- Type-checking-only imports (`if TYPE_CHECKING:`)
- Defensive no-cover exceptions (`except ...: raise  # pragma: no cover`)
- Platform-specific code (Windows-only, asyncio signal handlers)

## Test Organization

### Directory Structure

```
tests/
├── unit/                  # Fast, isolated tests (< 2 sec total)
│   ├── test_daemon_runner.py
│   ├── test_cost_analytics.py
│   └── ...
├── integration/           # Slower tests with real dependencies
│   ├── test_end_to_end.py
│   └── ...
├── bdd/                   # Behavior-driven tests
└── conftest.py            # Shared fixtures
```

### Test File Naming

**Pattern:** `test_MODULE_FEATURE_SCENARIO.py`

**Examples:**
- `test_daemon_runner_task_queue.py` (daemon module, task queue feature)
- `test_cost_analytics_period_aggregation.py` (cost_analytics module, period aggregation feature)
- `test_sandbox_runner_timeout_verdict.py` (sandbox runner module, timeout scenario)

## Test Function Naming

**Pattern:** `test_OPERATION_when_CONDITION_then_OUTCOME()`

This BDD-style naming makes test intent crystal clear:

### Good Examples

```python
def test_submit_task_when_queue_full_then_raises_saturation_error():
    """Queue saturation raises QueueSaturationError with backoff_seconds."""
    ...

def test_estimate_cost_when_unknown_model_then_defaults_to_zero():
    """Unknown models are treated as free (local/cached)."""
    ...

def test_decode_token_when_expired_then_raises_invalid_token_error():
    """Expired JWT tokens are rejected."""
    ...

def test_task_queue_dequeue_latency_when_1000_items_then_under_1ms():
    """Task dequeue is a critical performance SLA."""
    ...
```

### Anti-Patterns to Avoid

❌ `test_submit()` - Unclear intent
❌ `test_errors()` - Too vague
❌ `test_TokenBudgetAllocator()` - Describes class, not behavior
❌ `test_with_timeout` - Missing outcome assertion

## Test Docstrings

Every test should have a docstring explaining the **why**, not the **how**:

```python
def test_estimate_cost_when_unknown_model_then_defaults_to_zero() -> None:
    """Estimate cost defaults to zero for unknown models.

    This prevents cost estimation from blocking on novel model names
    (e.g., customer-specific fine-tunes). The allocator will recommend
    the cheapest known model as fallback.

    Regression: Issue #612 (model routing should not fail on unknown names)
    """
    allocator = TokenBudgetAllocator(config, ledger)
    est = allocator.estimate_cost(model="custom-fine-tune-xyz", ...)
    assert est.cost_usd == 0.0
    assert est.confidence == "medium"
```

**Format:**
- First line: One-sentence summary of the test
- Body: Explain why this test matters, edge cases, performance SLAs
- Regression line: Reference bugs or issues this test prevents

## TDD Cycle (Red-Green-Refactor)

Every feature should follow this cycle **before merging**:

1. **Red**: Write failing test that specifies the desired behavior
2. **Green**: Implement minimal code to make the test pass
3. **Refactor**: Clean up without changing behavior

### In Practice

```bash
# 1. Create test file with failing test
$ cat > tests/unit/test_new_feature.py << 'EOF'
def test_feature_when_condition_then_outcome():
    """Clear docstring explaining why this matters."""
    result = new_feature(input)
    assert result == expected
EOF

# 2. Run tests—should FAIL
$ pytest tests/unit/test_new_feature.py -v
# FAILED - as expected

# 3. Implement the feature
$ # ... edit maxwell_daemon/...

# 4. Run tests—should PASS
$ pytest tests/unit/test_new_feature.py -v
# PASSED

# 5. Run full test suite to ensure no regressions
$ pytest tests/unit -v
# All passing, coverage maintained
```

## Async Test Patterns

All async tests must use `pytest-asyncio` with explicit markers:

### ✅ Correct Patterns

```python
import asyncio
import pytest

class TestAsyncQueue:
    @pytest.mark.asyncio
    async def test_queue_put_when_full_then_raises(self) -> None:
        """Queue.full() must be checked before put_nowait()."""
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        queue.put_nowait(1)

        with pytest.raises(asyncio.QueueFull):
            queue.put_nowait(2)

    @pytest.mark.asyncio
    async def test_concurrent_operations_when_multiple_tasks_then_all_complete(self) -> None:
        """Use gather with return_exceptions=True to catch all failures."""
        async def worker(n: int) -> int:
            await asyncio.sleep(0.001)
            return n * 2

        results = await asyncio.gather(
            *[worker(i) for i in range(10)],
            return_exceptions=True
        )
        assert len(results) == 10
        assert all(isinstance(r, int) for r in results)

    @pytest.mark.asyncio
    async def test_operation_when_timeout_then_raises(self) -> None:
        """Use asyncio.wait_for() to detect deadlocks."""
        async def slow_operation() -> None:
            await asyncio.sleep(10)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slow_operation(), timeout=0.1)
```

### ❌ Anti-Patterns

```python
# Bad: mixing time.sleep() (blocks event loop) with async
async def test_bad():
    time.sleep(0.1)  # ❌ Blocks the entire event loop!
    await async_operation()

# Bad: no timeout protection
async def test_bad_no_timeout():
    # This test can hang forever if async_operation deadlocks
    result = await async_operation()

# Bad: ignoring exceptions in gather
async def test_bad_gather():
    results = await asyncio.gather(
        task1(),  # If this fails, gather fails immediately
        task2()
    )
```

## Parametrized Tests

Use `@pytest.mark.parametrize` for combinatorial testing to reduce duplication:

```python
@pytest.mark.parametrize("backend,model", [
    ("anthropic", "claude-opus-4-7"),
    ("anthropic", "claude-sonnet-4-6"),
    ("openai", "gpt-4o"),
])
def test_cost_estimation_across_backends(backend: str, model: str) -> None:
    """Verify cost estimation works for all backend+model combinations."""
    allocator = TokenBudgetAllocator(config, ledger)
    est = allocator.estimate_cost(model=model, prompt_tokens=1000, ...)
    assert est.cost_usd >= 0
    assert est.confidence in ("high", "medium", "low")

@pytest.mark.parametrize("priority,depth", [
    (0, 10),      # Emergency with full queue
    (50, 1),      # High priority with empty queue
    (100, 500),   # Normal priority with large backlog
    (200, 1000),  # Batch with massive queue
])
def test_task_dispatch_across_priorities(priority: int, depth: int) -> None:
    """Verify dispatcher handles all priority+queue_depth combinations."""
    # Implementation...
```

## Testing Fixtures

Define reusable fixtures in `tests/conftest.py` to reduce boilerplate:

```python
# tests/conftest.py
import pytest
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.core.ledger import CostLedger

@pytest.fixture
def mock_config() -> MaxwellDaemonConfig:
    """Minimal valid config for testing."""
    return MaxwellDaemonConfig.default()

@pytest.fixture
def cost_ledger(tmp_path) -> CostLedger:
    """In-memory cost ledger with temp DB."""
    return CostLedger(tmp_path / "test_ledger.db")

# In test files:
def test_example(mock_config: MaxwellDaemonConfig, cost_ledger: CostLedger) -> None:
    """Fixtures are automatically injected."""
    ...
```

## Performance SLAs (Benchmarking)

Critical operations have performance SLAs enforced via timing assertions:

- **Task queue dequeue:** < 1ms
- **TaskStore recovery (1000 tasks):** < 500ms
- **Cost ledger append:** < 10ms

Performance tests catch regressions using direct timing:

```python
import time

def test_queue_dequeue_latency() -> None:
    """Task dequeue must stay under 1ms SLA."""
    queue = TaskQueue(maxsize=1000)
    for i in range(1000):
        queue.put_nowait(Task(id=f"task-{i}"))

    start = time.perf_counter()
    result = queue.get_nowait()
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result is not None
    assert elapsed_ms < 1.0, f"Dequeue took {elapsed_ms:.2f}ms, expected < 1ms"
```

### Phase-1 Benchmark Smoke Suite (`tests/benchmarks/`)

A lightweight `pytest-benchmark` suite lives at `tests/benchmarks/` and
covers three hot-path operations (Phase 1 of #800):

- `GET /api/status` — HTTP read p50.
- `DispatchRequest` Pydantic envelope validation throughput.
- `TaskStore.save` SQLite write throughput.

This suite is **excluded from the default `pytest` invocation** (via
`--ignore=tests/benchmarks` in `pyproject.toml`) because timing
benchmarks can flake on contended runners and would bias coverage.
Run it locally on demand:

```bash
pytest tests/benchmarks/ --benchmark-only --no-cov
```

CI does not run this suite; the existing `pytest benchmarks/`
top-level invocation is unchanged.

## Coverage Exclusions

Mark code that cannot/should not be tested with `# pragma: no cover`:

```python
# Platform-specific (tested on CI)
if sys.platform == "win32":  # pragma: no cover
    setup_windows_handlers()

# Defensive exception (should never happen)
except Exception as exc:  # pragma: no cover
    raise RuntimeError(f"Unreachable: {exc}")

# Type-checking only (no runtime test needed)
if TYPE_CHECKING:
    from typing import TypeAlias
    TaskId: TypeAlias = str
```

## Flaky Test Detection

Tests that fail intermittently must be fixed or skipped:

**Best Practice**: Fix the underlying race condition or use mocked time instead of real timing.

**If skipping is necessary**, use `pytest.mark.skip` with a reason:

```python
import pytest

@pytest.mark.skip(reason="Timing-sensitive test; requires refactoring to use fixed time")
def test_timing_sensitive_operation() -> None:
    """This test is sensitive to system load and timing jitter.

    Timing-based assertions are unreliable on CI. Either:
    1. Use mocked time (pytest-freezegun) with fixed delays
    2. Remove timing assertions and test behavior instead
    3. Use synchronization primitives (events, locks) instead of sleep
    """
    # Implementation...
```

**Better approach**: Refactor timing-sensitive code to use synchronization instead of sleep/timing.

## Test Execution

Run tests by scope:

```bash
# Fast unit tests only (< 2 sec)
pytest tests/unit -v

# Include integration tests (slower)
pytest tests/unit tests/integration -v

# Specific test file
pytest tests/unit/test_token_budget.py -v

# Specific test function
pytest tests/unit/test_token_budget.py::test_estimate_cost_when_unknown_model_then_defaults_to_zero -v

# With coverage report
pytest tests/unit --cov=maxwell_daemon --cov-report=term-missing

# Verbose output with captured print statements
pytest tests/unit -vv -s
```

## Pre-Commit Checks

Before pushing, ensure:

1. **All tests pass:** `pytest tests/unit tests/integration`
2. **Coverage maintained:** `scripts/check_coverage_floor.py`
3. **No lint issues:** `ruff check .`
4. **Type checking passes:** `mypy --strict maxwell_daemon`

## Resources

- **Coverage Script:** `scripts/check_coverage_floor.py`
- **Test Fixtures:** `tests/conftest.py`
- **pytest documentation:** https://docs.pytest.org
- **pytest-asyncio:** https://github.com/pytest-dev/pytest-asyncio
