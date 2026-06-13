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
- `submit_threadsafe()` delegates task registration to the daemon event-loop thread before waiting
  for the returned task, preventing cross-thread task-map locks from blocking loop callbacks.
- CI test lanes enforce bounded execution with the pytest timeout plugin and a matrix job timeout
  so a wedged test cannot block merge readiness indefinitely. Coverage is produced by the py3.12
  lane; py3.10 and py3.11 run compatibility tests without coverage overhead. The test matrix
  targets the desktop Linux runner pool for predictable throughput.

## HTTP API

The daemon exposes a versioned HTTP surface under `/api/v1/*` (with a small set
of stable legacy aliases such as `/api/status`, `/api/v2/status`, `/api/reload`,
and `/api/webhooks/trigger`). The router families are:

| Family | Prefix(es) | Purpose |
| --- | --- | --- |
| auth | `/api/v1/auth/*` | Token mint / refresh / revoke / introspect (`me`). |
| tasks | `/api/v1/tasks`, `/api/tasks` (legacy) | Submit, list, fetch, cancel tasks. |
| issues | `/api/v1/issues/*`, `/api/v1/templates/*`, `/api/v1/memory/*` | Issue dispatch (plan/implement, A/B, batch), prompt templates, memory assemble/record. |
| backends | `/api/v1/backends/*`, `/api/v1/cost` | Backend inventory + cost ledger summary. |
| actions | `/api/v1/actions/*`, `/api/v1/artifacts/*` | Proposed action approval/rejection + artifact retrieval. |
| work-items | `/api/v1/work-items/*`, `/api/v1/task-graphs/*` | Work-item and task-graph inspection. |
| control-plane | `/api/v1/control-plane/gauntlet*` | Gauntlet run inventory + retry/cancel/waive. |
| fleet | `/api/v1/fleet/*`, `/api/v1/workers/*`, `/api/v1/heartbeat` | Fleet topology, worker registration, heartbeats. |
| ssh | `/api/v1/ssh/*` | SSH key store + session pool management. |
| audit | `/api/v1/audit/*`, `/api/reload`, `/api/v1/admin/prune` | Audit-log read/verify, config hot-reload, retention prune. |
| webhooks | `/api/v1/webhooks/*`, `/api/webhooks/trigger`, `/api/v1/evals/*` | Webhook config/trigger + eval runs. |
| events | `/api/v1/events` (WebSocket) | Live event stream (see Event Bus below). |

The authoritative, drift-checked route inventory lives in
[`docs/reference/openapi.md`](docs/reference/openapi.md) and is enforced against
the live FastAPI schema by `tests/unit/test_openapi_docs_sync.py` (a route added
or removed without updating that doc fails CI). This SPEC intentionally lists
route *families* rather than every path, so the single source of truth for exact
paths and shapes remains the generated OpenAPI schema + that sync test.

**Stability rule:** the `/api/v1/*` and legacy status endpoints are append-only.
New endpoints and new response fields may be added freely; existing request or
response shapes must not change without bumping the major version advertised at
`GET /api/version`.

## Event Bus

Subscribers consume a typed event stream over `/api/v1/events` (WebSocket) and an
in-process `EventBus`. Each event is `{kind, ts, payload}`; `payload` may carry a
normalized `observability` context (task/work-item/action/artifact ids plus
cost/timing). The `kind` values are defined by `EventKind`
(`maxwell_daemon/events.py`):

- Task lifecycle: `task_queued`, `task_started`, `task_completed`, `task_failed`.
- Action lifecycle: `action_proposed`, `action_approved`, `action_rejected`,
  `action_running`, `action_applied`, `action_failed`, `action_skipped`.
- Diagnostics: `test_output`, `budget_alert`, `backend_health`.

Event kinds are append-only: add new kinds rather than repurposing existing ones.

## Task State Machine

Tasks are persisted in the SQLite `TaskStore` (`TaskKind` ∈ {`prompt`, `issue`})
and move through `TaskStatus` (`maxwell_daemon/daemon/task_models.py`):

```
            ┌──────────── dispatched ──────────┐   (fleet coordinator only)
            │                                   │
queued ─────┼──────────────► running ──────────┼──► completed
            │                   │               │
            │                   ├──► failed ◄────┘   (stall / error / dep failure)
            └──► cancelled ◄────┘                    (operator cancel)
```

- `queued` → `running`: a worker claims the task once dependencies are COMPLETED
  and the per-kind concurrency cap (`agent.concurrency_by_kind`) allows it.
- `queued`/`running` → `dispatched`: the coordinator role assigns the task to a
  remote worker (re-checked under lock so a racing cancel is never overwritten).
- `running` → `completed` | `failed`: normal terminal outcomes. A task that
  exceeds `agent.stall_timeout_seconds` is failed and retried under the bounded
  `RetryPolicy`; a task whose dependency terminally failed is failed with reason
  `dependency_failed` rather than re-queued forever.
- any non-terminal → `cancelled`: operator cancellation. Terminal states
  (`completed`, `failed`, `cancelled`) are final.

## Fleet Manifest (`fleet.yaml`)

`fleet.yaml` is a separate file from `maxwell-daemon.yaml`; it lists the repos the
fleet manages plus shared defaults. Resolution order: explicit path →
`MAXWELL_FLEET_CONFIG` → `./fleet.yaml` → `~/.maxwell-daemon/fleet.yaml`. Schema
(`maxwell_daemon/config/fleet.py`):

```yaml
version: 1                       # must be 1
fleet:                           # FleetDefaults — applied to every repo unless overridden
  name: My Fleet                 # required
  auto_promote_staging: false
  discovery_interval_seconds: 300 # >= 10
  default_slots: 2                # 1..32
  default_budget_per_story: 0.50  # >= 0
  default_pr_target_branch: staging
  default_pr_fallback_to_default: true
  default_watch_labels: []
repos:                           # FleetRepoEntry list; unset fields inherit defaults
  - name: Repo1                  # required
    org: my-org                  # required
    slots: 4                     # optional, 1..32
    budget_per_story: 0.25       # optional, >= 0
    pr_target_branch: main       # optional
    pr_fallback_to_default: false
    watch_labels: [maxwell:ready]
    enabled: true                # per-repo, not inheritable
```

Duplicate repo names and `version != 1` are rejected at load time. See
[`docs/architecture/fleet-architecture.md`](docs/architecture/fleet-architecture.md)
for the dispatch/coordinator design.

## Related Documentation

- [`docs/architecture/overview.md`](docs/architecture/overview.md) — system overview.
- [`docs/reference/openapi.md`](docs/reference/openapi.md) — drift-checked route inventory.
- [`docs/architecture/fleet-architecture.md`](docs/architecture/fleet-architecture.md) — fleet topology + remote dispatch.
- [`docs/operations/observability.md`](docs/operations/observability.md) — metrics, logging, event consumption.
- [`docs/operations/security.md`](docs/operations/security.md) — execution policy + isolation caveats.
