# ADR-006: SSH worker side-effect failover boundary

## Status
Accepted

## Context
Fleet and SSH workers execute tasks on remote hosts while the coordinator remains the
source of scheduling state. Transparent failover is useful before execution has changed
external state, but replaying a task after file writes, command execution, or repository
operations can duplicate side effects.

## Decision
Tasks carry a durable `side_effects_started` flag alongside `dispatched_to`. The daemon marks
the flag before an approved side-effect action enters `RUNNING`. Coordinator stale-worker
handling may requeue a dispatched task only when that flag is false. If a worker becomes stale
after side effects started, the coordinator fails the task and leaves retry to the normal
new-attempt path.

## Consequences
- Dispatch recovery remains automatic for tasks that never crossed the side-effect boundary.
- Tasks that may have touched external state stop in a visible failed state instead of being
  invisibly replayed on another host.
- A future SSH-specific side-effect detector can add more marking sites without changing the
  persisted task contract.
