# ADR-001: FastAPI + Vanilla JS UI

**Status:** Accepted
**Date:** 2026-04-27
**Deciders:** Maxwell-Daemon Core Team

---

## Context

The daemon needs an operator UI. We evaluated several frontend stack options.

## Decision

Use FastAPI for the HTTP backend and a vanilla JavaScript SPA served as static files. No build step required.

## Consequences

- **Positive:** Zero npm dependency overhead, instant startup, simple deployment
- **Positive:** UI files are first-class Python package assets
- **Negative:** No modern framework features (React/Vue reactivity)
- **Negative:** Manual DOM manipulation for complex interactions

## Rationale

The project is backend-heavy. A lightweight UI for monitoring and control does not warrant a full frontend build pipeline. The AGENTS.md mandates this as the canonical UI.

---

## References

- `maxwell_daemon/api/ui/`
- `AGENTS.md` — Architecture Notes
