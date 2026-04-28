# Architecture Specification

## Contract
- Maxwell-Daemon provides a backend API.
- RUNNING tasks that exceed `agent.stall_timeout_seconds` without progress are cancelled and re-queued.
- `agent.concurrency_by_kind` can cap queued task dispatch by issue mode or task kind without changing the global worker count.

