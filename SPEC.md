# Architecture Specification

## Contract
- Maxwell-Daemon provides a backend API.
- RUNNING tasks that exceed `agent.stall_timeout_seconds` without progress are cancelled and re-queued.

