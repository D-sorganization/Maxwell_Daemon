# Architecture Specification

## Contract
- Maxwell-Daemon provides a backend API.
- RUNNING tasks that exceed `agent.stall_timeout_seconds` without progress are cancelled and re-queued.
- `/api/status` remains the stable v1 dashboard status response.
- `/api/v2/status` is an append-only dashboard envelope with `generated_at`, `counts`,
  `running`, `retrying`, `codex_totals`, and `rate_limits`. Running task token fields
  are populated from the cost ledger when records exist for the task id; `rate_limits`
  is `null` until backend adapters expose provider rate-limit snapshots.

