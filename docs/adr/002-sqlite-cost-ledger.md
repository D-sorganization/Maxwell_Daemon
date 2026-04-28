# ADR-002: SQLite Cost Ledger

**Status:** Accepted
**Date:** 2026-04-27
**Deciders:** Maxwell-Daemon Core Team

---

## Context

The daemon needs to track costs per backend call. We evaluated several database options.

## Decision

Use a WAL-mode SQLite file for the cost ledger. No ORM abstraction layer.

## Consequences

- **Positive:** Zero external dependency for persistence, simple backups
- **Positive:** WAL mode enables concurrent reads during writes
- **Positive:** File-based, easy to audit and version-control schema migrations
- **Negative:** Not horizontally scalable across multiple daemon instances
- **Negative:** Requires careful schema migration planning

## Rationale

Costs are append-only records with simple aggregation queries. SQLite WAL mode provides sufficient concurrency for a single-node daemon. Replacing with an ORM would add complexity without benefit.

## Migration Policy

If the daemon needs to scale horizontally, migrate to PostgreSQL with a proper migration script. Never replace SQLite with an ORM-based abstraction without migration.

---

## References

- `maxwell_daemon/core/` — Cost tracking modules
- `AGENTS.md` — Architecture Notes