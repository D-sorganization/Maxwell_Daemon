"""Tests for TddGate — enforces the red-green-refactor discipline on agent output.

Every non-trivial change must:
  1. Start by adding a *failing* test (verified RED).
  2. Make that test pass via the implementation (verified GREEN).
  3. The same test must not have passed before the implementation (that
     would mean the change is untested or the test is a no-op).

All tests inject a recorder test-runner so no real pytest runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from maxwell_daemon.tdd_gate import (
    RedGreenResult,
    RunOutcome,
    TddGate,
    TddViolationError,
)


@dataclass
class _FakeRunner:
    """Replays canned outcomes keyed by the test files touched."""

    outcomes: list[RunOutcome] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    async def __call__(self, *, workspace: Path, test_paths: tuple[str, ...]) -> RunOutcome:
        self.calls.append({"workspace": str(workspace), "test_paths": test_paths})
        if not self.outcomes:
            return RunOutcome(passed=True, returncode=0, output="", duration_seconds=0.0)
        return self.outcomes.pop(0)


# ── Shape ────────────────────────────────────────────────────────────────────


class TestShapes:
    def test_test_run_outcome_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        o = RunOutcome(passed=True, returncode=0, output="ok", duration_seconds=0.1)
        with pytest.raises(FrozenInstanceError):
            o.passed = False  # type: ignore[misc]

    def test_red_green_result_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        r = RedGreenResult(
            red_ran=True,
            red_failed=True,
            green_ran=True,
            green_passed=True,
            honest=True,
            detail="",
        )
        with pytest.raises(FrozenInstanceError):
            r.honest = False  # type: ignore[misc]


# ── Preconditions ────────────────────────────────────────────────────────────


class TestPreconditions:
    def test_requires_workspace_directory(self, tmp_path: Path) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError, match="workspace"):
            TddGate(workspace=tmp_path / "missing", test_runner=_FakeRunner())


# ── Red → Green happy path ───────────────────────────────────────────────────


class TestRedGreenHappyPath:
    async def test_test_fails_red_then_passes_green(self, tmp_path: Path) -> None:
        runner = _FakeRunner(
            outcomes=[
                RunOutcome(passed=False, returncode=1, output="assert 0 == 1"),
                RunOutcome(passed=True, returncode=0, output=""),
            ]
        )
        gate = TddGate(workspace=tmp_path, test_runner=runner)

        async def implement() -> None:
            pass  # caller supplies the implementation step

        result = await gate.verify_red_green(
            test_paths=("tests/unit/test_new.py",),
            implement=implement,
        )
        assert result.red_ran is True
        assert result.red_failed is True
        assert result.green_ran is True
        assert result.green_passed is True
        assert result.honest is True

    async def test_runner_called_twice_red_then_green(self, tmp_path: Path) -> None:
        runner = _FakeRunner(
            outcomes=[
                RunOutcome(passed=False, returncode=1, output=""),
                RunOutcome(passed=True, returncode=0, output=""),
            ]
        )
        gate = TddGate(workspace=tmp_path, test_runner=runner)

        async def implement() -> None:
            pass

        await gate.verify_red_green(test_paths=("tests/unit/test_new.py",), implement=implement)
        assert len(runner.calls) == 2
        assert runner.calls[0]["test_paths"] == ("tests/unit/test_new.py",)


# ── Dishonest tests (never failed) ───────────────────────────────────────────


class TestDishonestTest:
    async def test_green_from_the_start_flagged_as_not_honest(self, tmp_path: Path) -> None:
        """If the new test passes before any implementation, it was a no-op test."""
        runner = _FakeRunner(
            outcomes=[
                RunOutcome(passed=True, returncode=0, output=""),  # RED returned passing
                # No green run expected — gate should raise on red-phase result.
            ]
        )
        gate = TddGate(workspace=tmp_path, test_runner=runner)

        async def implement() -> None:
            pass

        with pytest.raises(TddViolationError, match="test-first violation"):
            await gate.verify_red_green(
                test_paths=("tests/unit/test_noop.py",), implement=implement
            )


# ── Regression cases ─────────────────────────────────────────────────────────


class TestImplementationRegressed:
    async def test_green_run_still_failing_is_reported(self, tmp_path: Path) -> None:
        runner = _FakeRunner(
            outcomes=[
                RunOutcome(passed=False, returncode=1, output="red ok"),
                RunOutcome(passed=False, returncode=1, output="still failing"),
            ]
        )
        gate = TddGate(workspace=tmp_path, test_runner=runner)

        async def implement() -> None:
            pass

        result = await gate.verify_red_green(
            test_paths=("tests/unit/test_x.py",), implement=implement
        )
        assert result.red_failed is True
        assert result.green_passed is False
        assert result.honest is True  # the RED was honest
        assert "still failing" in result.detail


# ── Implement callback receives outcome of RED ──────────────────────────────


class TestImplementCallbackContract:
    async def test_implement_called_once_between_red_and_green(self, tmp_path: Path) -> None:
        call_log: list[str] = []
        runner = _FakeRunner(
            outcomes=[
                RunOutcome(passed=False, returncode=1, output=""),
                RunOutcome(passed=True, returncode=0, output=""),
            ]
        )
        gate = TddGate(workspace=tmp_path, test_runner=runner)

        async def implement() -> None:
            call_log.append("implement")

        await gate.verify_red_green(test_paths=("tests/unit/test_new.py",), implement=implement)
        assert call_log == ["implement"]

    async def test_implement_not_called_when_test_is_dishonest(self, tmp_path: Path) -> None:
        call_log: list[str] = []
        runner = _FakeRunner(outcomes=[RunOutcome(passed=True, returncode=0, output="")])
        gate = TddGate(workspace=tmp_path, test_runner=runner)

        async def implement() -> None:
            call_log.append("implement")

        with pytest.raises(TddViolationError):
            await gate.verify_red_green(
                test_paths=("tests/unit/test_noop.py",), implement=implement
            )
        assert call_log == []


# ── PR body annotation ──────────────────────────────────────────────────────


class TestPrBodyAnnotation:
    def test_passing_result_produces_audit_line(self) -> None:
        result = RedGreenResult(
            red_ran=True,
            red_failed=True,
            green_ran=True,
            green_passed=True,
            honest=True,
            detail="",
        )
        line = result.to_pr_body_note()
        assert "TDD" in line
        assert "red" in line.lower() and "green" in line.lower()
