# Design by Contract

Maxwell-Daemon uses lightweight DbC primitives to enforce correctness at module boundaries without reaching for full static verification.

## Primitives

From `maxwell_daemon.contracts`:

```python
require(cond, msg)            # precondition — check at top of function
ensure(cond, msg)             # postcondition — check before return

@precondition(fn, msg)        # decorator form of require
@postcondition(fn, msg)       # decorator form of ensure
@invariant(fn, msg)           # class decorator — run after every public method
```

## Exception hierarchy

```
AssertionError
└── ContractViolation         # base class for all DbC failures
    ├── PreconditionError     # caller's fault
    └── PostconditionError    # implementation's fault
```

## Example

```python
from maxwell_daemon.contracts import postcondition, precondition

@precondition(lambda x: x >= 0, "x must be non-negative")
@postcondition(lambda result: result >= 0, "result must be non-negative")
def sqrt(x: float) -> float:
    return x ** 0.5
```

## Class invariants

Invariants only run after **public** methods (names not starting with `_`). This lets private helpers temporarily break the invariant mid-computation as long as it's restored by the time a public method returns.

```python
from maxwell_daemon.contracts import invariant

@invariant(lambda self: self.balance >= 0, "overdraft not allowed")
class Account:
    def __init__(self): self.balance = 0
    def deposit(self, n): self.balance += n
    def withdraw(self, n): self.balance -= n   # raises if it would go negative
```

## Disabling contracts in production

Contracts are enabled by default because the cost is tiny and the diagnostic value is high. When you're running a bulk workload and every microsecond counts:

```bash
MAXWELL_CONTRACTS=off maxwell-daemon serve
```

Each call reads the env var fresh, so you can toggle contracts at runtime without restarting Python.

## Why not `assert`?

Python's `assert` is stripped under `-O`. Contracts need to be explicit about whether they should still run in optimised builds — so we use dedicated exception classes that can't be accidentally optimised away.
