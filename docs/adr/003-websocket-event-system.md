# ADR-003: WebSocket + SSE Event System

**Status:** Accepted
**Date:** 2026-04-27
**Deciders:** Maxwell-Daemon Core Team

---

## Context

The daemon needs to stream agent progress and events to clients. We evaluated polling, SSE, and WebSocket options.

## Decision

Support both Server-Sent Events (SSE) and WebSockets for event streaming.

- **SSE** (`GET /api/v1/events`) — for one-way progress streaming
- **WebSocket** (`WS /api/v1/events`) — for bidirectional control

## Consequences

- **Positive:** SSE works over HTTP, simpler for clients, auto-reconnects
- **Positive:** WebSocket enables bidirectional control (pause, cancel)
- **Negative:** Two event paths to maintain and test
- **Negative:** WebSocket requires connection management (heartbeats, cleanup)

## Rationale

SSE covers the common use case of monitoring task progress. WebSocket enables interactive control. Both are needed for different consumption patterns.

## Testing Policy

Always test event propagation when modifying the daemon loop. Both SSE and WebSocket paths must be verified.

---

## References

- `maxwell_daemon/api/server.py` — WebSocket and SSE handlers
- `AGENTS.md` — Architecture Notes
- `SPEC.md` — WebSocket Events section