# Architecture Specification

## Contract
- Maxwell-Daemon provides a backend API.
- RUNNING tasks that exceed `agent.stall_timeout_seconds` without progress are cancelled and re-queued.
- `/api/status` remains the stable v1 dashboard status response.
- `/api/v2/status` is an append-only dashboard envelope with `generated_at`, `counts`,
  `running`, `retrying`, `codex_totals`, and `rate_limits`. Running task token fields
  are populated from the cost ledger when records exist for the task id; `rate_limits`
  is `null` until backend adapters expose provider rate-limit snapshots.
- `agent.concurrency_by_kind` can cap queued task dispatch by issue mode or task kind without changing the global worker count.
- Fleet-dispatched tasks persist `dispatched_to` and `side_effects_started`. A coordinator may
  transparently requeue a stale dispatched task only while `side_effects_started` is false; once
  side effects have started, stale-worker recovery fails the task so any retry is a new attempt.
- `submit_threadsafe()` must remain safe while daemon maintenance loops are active; cross-thread
  queue scheduling cannot hold the live task mutex while waiting on the event loop.
- CI test lanes enforce bounded execution with the pytest timeout plugin and a matrix job timeout
  so a wedged test cannot block merge readiness indefinitely. Coverage is produced by the py3.12
  lane; py3.10 and py3.11 run compatibility tests without coverage overhead. The test matrix
  targets the desktop Linux runner pool for predictable throughput.

