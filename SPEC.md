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

