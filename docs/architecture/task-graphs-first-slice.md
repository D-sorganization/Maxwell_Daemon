# Named Sub-Agent Task Graphs (First Slice)

This document captures the first production-relevant slice for issue [#286](https://github.com/D-sorganization/Maxwell-Daemon/issues/286).

## What this slice adds

- A dedicated `maxwell_daemon.graphs` module with typed graph primitives.
- Named node roles (`planner`, `implementer`, `qa`, `reviewer`, `security`, etc.).
- Typed handoff artifact kinds between nodes (`plan`, `implementation_diff`, `qa_report`, ...).
- Graph validation with design-by-contract rules:
  - unique node ids,
  - dependency ids must exist,
  - DAG/cycle detection,
  - retry bounds,
  - required artifact kinds must be produced by dependencies.
- Built-in templates:
  - `micro-delivery`,
  - `standard-delivery`,
  - `security-sensitive-delivery`.
- A simple template selector that chooses template kind from risk + acceptance-criteria count + security labels.

## Why this slice is intentionally small

The goal is to establish stable, typed orchestration contracts first, without coupling to executor/runtime internals. This keeps overlap low while creating concrete APIs that a runner/service layer can consume next.

## Follow-on implementation slices

1. Add a graph runner service that executes ready nodes and persists node run records.
2. Persist typed node artifacts through the existing artifact store.
3. Expose graph create/start/status APIs and CLI integration.
4. Attribute cost/events per node run.
