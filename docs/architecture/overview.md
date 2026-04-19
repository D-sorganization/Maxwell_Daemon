# Architecture overview

```
┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│  CLI        │ │  REST API   │ │  WebSocket  │
└──────┬──────┘ └──────┬──────┘ └──────┬──────┘
       │               │               │
       └───────────────┴───────────────┘
                       │
                ┌──────▼──────┐
                │   Daemon    │
                │  - queue    │
                │  - workers  │
                │  - events   │
                └──────┬──────┘
                       │
     ┌─────────────────┼─────────────────┬──────────────┐
     │                 │                 │              │
┌────▼─────┐    ┌──────▼─────┐   ┌───────▼──────┐ ┌─────▼─────┐
│ Router   │    │  Ledger    │   │   Budget     │ │  Metrics  │
│ (config) │    │ (SQLite)   │   │  Enforcer    │ │(Prometheus│
└────┬─────┘    └────────────┘   └──────────────┘ └───────────┘
     │
     │  (picks one)
     ▼
┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
│ Claude │ │ OpenAI │ │ Azure  │ │ Ollama │ │ Google │
│        │ │        │ │        │ │(local) │ │ Vertex │
└────────┘ └────────┘ └────────┘ └────────┘ └────────┘
```

## Components

**`conductor.backends`** — one `ILLMBackend` interface, one `BackendRegistry`, and one adapter per provider. Adapters are imported lazily so users only need the SDKs they actually use.

**`conductor.config`** — Pydantic-validated YAML with `${ENV_VAR}` substitution. Fail-fast on misconfiguration.

**`conductor.core`** — the decoupled primitives:

- `BackendRouter` picks a backend for each task based on repo/override/default precedence.
- `CostLedger` is an append-only SQLite record of every request's token usage and USD cost.
- `BudgetEnforcer` reads the ledger and enforces monthly caps — soft alert thresholds or hard-stop.

**`conductor.daemon`** — owns a task queue, a worker pool, and an `EventBus`. External callers go through `submit()` / `state()` / `events`. Never reaches into other modules for internal state.

**`conductor.api`** — FastAPI app: `/health`, `/api/v1/{backends,tasks,cost}`, `/api/v1/events` (WebSocket), `/metrics` (Prometheus).

**`conductor.events`** — bounded-queue fan-out pub/sub. Slow subscribers get dropped rather than blocking publishers (telemetry is best-effort; the ledger is the durable record).

**`conductor.contracts`** — Design-by-Contract primitives: `require`, `ensure`, `@precondition`, `@postcondition`, `@invariant`. Enabled by default, toggle off in prod via `CONDUCTOR_CONTRACTS=off`.

**`conductor.metrics`** — single `record_request()` entrypoint for Prometheus counters and histograms. Label taxonomy lives in one place so dashboards stay consistent.

**`conductor.logging`** — structlog bridge. JSON in production (ship to Loki/ELK), pretty console on TTY.

## Design principles

- **Orthogonal.** Budget lives next to the ledger, not inside it. Swap either independently.
- **DRY via inheritance where it fits.** `AzureOpenAIBackend` is 52 lines — a subclass of `OpenAIBackend` that only swaps the client.
- **Least-surprise async.** Streams return `AsyncIterator[str]` directly (no extra await at the call site).
- **Observable boundaries.** Every request hits the ledger, metrics, and event bus in that order so nothing is hidden from operators.
