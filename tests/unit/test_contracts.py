"""Design by Contract — precondition, postcondition, invariant enforcement.

Contracts are the foundation of Maxwell-Daemon's correctness guarantees. They're
enabled in dev/test and can be disabled in production via MAXWELL_CONTRACTS=off
when the performance overhead matters.
"""

from __future__ import annotations

import pytest

from maxwell_daemon.contracts import (
    ContractViolation,
    PostconditionError,
    PreconditionError,
    contracts_enabled,
    ensure,
    invariant,
    postcondition,
    precondition,
    require,
)


class TestRequire:
    def test_passes_when_true(self) -> None:
        require(True, "should not raise")

    def test_raises_precondition_error_when_false(self) -> None:
        with pytest.raises(PreconditionError, match="must be positive"):
            require(False, "must be positive")

    def test_precondition_is_subclass_of_contract_violation(self) -> None:
        with pytest.raises(ContractViolation):
            require(False, "x")

    def test_message_is_preserved(self) -> None:
        with pytest.raises(PreconditionError, match="x must be < 10"):
            require(False, "x must be < 10")

    def test_disabled_contracts_skip_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAXWELL_CONTRACTS", "off")
        # With the flag read on each call, no reload is needed.
        require(False, "should be skipped")
        assert not contracts_enabled()


class TestEnsure:
    def test_passes_when_true(self) -> None:
        ensure(True, "ok")

    def test_raises_postcondition_error(self) -> None:
        with pytest.raises(PostconditionError, match="result non-empty"):
            ensure(False, "result non-empty")


class TestPreconditionDecorator:
    def test_enforced_before_call(self) -> None:
        @precondition(lambda x: x >= 0, "x must be non-negative")
        def sqrt_like(x: float) -> float:
            return x**0.5

        assert sqrt_like(4) == 2.0
        with pytest.raises(PreconditionError, match="non-negative"):
            sqrt_like(-1)

    def test_multiple_preconditions_all_checked(self) -> None:
        @precondition(lambda x, y: x > 0, "x > 0")
        @precondition(lambda x, y: y > 0, "y > 0")
        def divide(x: int, y: int) -> float:
            return x / y

        assert divide(6, 2) == 3.0
        with pytest.raises(PreconditionError, match="y > 0"):
            divide(6, -1)
        with pytest.raises(PreconditionError, match="x > 0"):
            divide(-1, 2)

    def test_condition_can_use_kwargs(self) -> None:
        @precondition(lambda *, name: len(name) > 0, "name required")
        def greet(*, name: str) -> str:
            return f"hi {name}"

        assert greet(name="world") == "hi world"
        with pytest.raises(PreconditionError):
            greet(name="")

    def test_async_function_supported(self) -> None:
        import asyncio

        @precondition(lambda n: n > 0, "n > 0")
        async def fetch(n: int) -> int:
            return n * 2

        assert asyncio.run(fetch(3)) == 6
        with pytest.raises(PreconditionError):
            asyncio.run(fetch(0))


class TestPostconditionDecorator:
    def test_checked_against_result(self) -> None:
        @postcondition(lambda result: result >= 0, "result non-negative")
        def abs_safe(x: int) -> int:
            return abs(x)

        assert abs_safe(-5) == 5

    def test_violation_raises(self) -> None:
        @postcondition(lambda result: result > 100, "result > 100")
        def too_small() -> int:
            return 5

        with pytest.raises(PostconditionError, match="result > 100"):
            too_small()

    def test_async_function_supported(self) -> None:
        import asyncio

        @postcondition(lambda result: len(result) > 0, "non-empty list")
        async def get_items() -> list[int]:
            return [1]

        assert asyncio.run(get_items()) == [1]

    def test_async_violation_raises(self) -> None:
        import asyncio

        @postcondition(lambda result: result > 10, "large result")
        async def get_value() -> int:
            return 3

        with pytest.raises(PostconditionError, match="large result"):
            asyncio.run(get_value())


class TestInvariantClassDecorator:
    def test_invariant_checked_after_public_methods(self) -> None:
        @invariant(lambda self: self.balance >= 0, "balance must be non-negative")
        class Account:
            def __init__(self) -> None:
                self.balance = 0

            def deposit(self, amount: int) -> None:
                self.balance += amount

            def withdraw(self, amount: int) -> None:
                self.balance -= amount

        a = Account()
        a.deposit(100)
        assert a.balance == 100

        with pytest.raises(ContractViolation, match="non-negative"):
            a.withdraw(200)

    def test_private_methods_bypass_invariant(self) -> None:
        # Invariants only check *public* boundaries. Private methods can
        # temporarily break invariants while doing multi-step work.
        @invariant(lambda self: self.x == self.y, "x must equal y")
        class Paired:
            def __init__(self) -> None:
                self.x = 0
                self.y = 0

            def update_both(self, v: int) -> None:
                self._set_x(v)
                self._set_y(v)

            def _set_x(self, v: int) -> None:
                self.x = v

            def _set_y(self, v: int) -> None:
                self.y = v

        p = Paired()
        p.update_both(5)
        assert p.x == p.y == 5

    def test_async_public_methods_checked(self) -> None:
        import asyncio

        @invariant(lambda self: self.balance >= 0, "balance must be non-negative")
        class Account:
            def __init__(self) -> None:
                self.balance = 0

            async def deposit(self, amount: int) -> None:
                self.balance += amount

            async def withdraw(self, amount: int) -> None:
                self.balance -= amount

        async def scenario() -> None:
            account = Account()
            await account.deposit(10)
            assert account.balance == 10
            with pytest.raises(ContractViolation, match="non-negative"):
                await account.withdraw(20)

        asyncio.run(scenario())


class TestContractsEnabled:
    def test_reports_state(self) -> None:
        assert contracts_enabled() in (True, False)
