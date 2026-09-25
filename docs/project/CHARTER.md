# Project Charter

> Drafted 2026-09-25 by the fleet charter sweep (Gemini) from README, git history, and open issues/PRs.
> The project-steward role keeps this current; owners should correct feature statuses.

## End Goal

Maxwell-Daemon provides an autonomous local control plane orchestrating a multi-agent AI development team (Strategist, Implementer, Crucible) with policy-gated execution, Bring-Your-Own-CLI (BYO-CLI) tooling, and an embedded browser UI. "Done" means maintaining a stable, append-only HTTP and WebSocket API contract (`/api/v1/*`) as an optional backend provider for Runner Dashboard while remaining stable and self-contained in maintenance mode pending archive review.

## Non-Goals

- Hard container or kernel isolation (the execution model uses policy gates and host subprocesses; true sandboxing is not provided).
- Operating as the active default fleet execution engine (superseded by Runner Dashboard Staff Hub).
- Outbound network callbacks into sibling repositories (`Runner_Dashboard` or `Repository_Management`).
- Serving as the fleet-wide operator dashboard (provided exclusively by `runner-dashboard`).
- Active feature expansion or multi-node cluster deployment while in maintenance mode.

## Features

| ID | Feature | Status | Tracking | Notes |
| --- | --- | --- | --- | --- |
| F1 | Cognitive Multi-Agent Pipeline | shipped | - | Orchestrates Strategist, Implementer, and Crucible state machines |
| F2 | Policy-Gated Host Executor | shipped | #991 | Argv allowlist, workspace-root check, and timeout policy gates |
| F3 | OpenAPI Schema and Contract Enforcement | shipped | #997 | Drift-checked FastAPI route inventory and append-only contract |
| F4 | Append-Only Status Reporting | shipped | #766 | Stable /api/status and /api/v2/status metrics and state envelopes |
| F5 | WebSocket Live Event Bus | shipped | - | Streams typed task and action lifecycle events over /api/v1/events |
| F6 | Task State Machine and SQLite Store | shipped | #970 | SQLite task DAG persistence with retry policy and stall detection |
| F7 | Fleet Coordinator and Manifest | shipped | #764 | Multi-repo management via fleet.yaml with per-kind concurrency caps |
| F8 | Local Web Dashboard and Desktop Launcher | shipped | #1166 | Browser UI at /ui/ with Electron wrapper and cross-platform scripts |
| F9 | Bring-Your-Own-CLI Integration | shipped | - | Execution wrappers for local tools including Jules, Claude, and Ollama |
| F10 | RepoSchematic and Memory Annealer | shipped | - | Token-efficient codebase compression and architectural state summaries |
| F11 | JWT and RBAC Authentication | shipped | #964 | Token minting, verification, and role-based route protection |
| F12 | Multi-IDE Extensions Suite | shipped | #988 | Editor plugins for VS Code, JetBrains, Obsidian, and Zed |
| F13 | Isolated Container Sandbox Backend | parked | #1015 | Real rootless Docker/OCI sandbox deferred; repo in maintenance mode |
| F14 | PostgreSQL Persistence Backend | parked | #926 | Persistence protocol and Postgres storage engine deferred |
| F15 | Distributed Tracing and Redis Rate Limiting | parked | #927 | OpenTelemetry spans and Redis rate limiting deferred from Phase 2/3 |
| F16 | Modern React Web UI Rebuild | parked | #930 | Frontend rewrite deferred as fleet moved to Runner Dashboard |

## Links

- Status (generated): [`STATUS.md`](STATUS.md)
- Steward playbook: Repository_Management `docs/fleet-project-steward.md`
