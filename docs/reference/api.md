# REST API reference

Base URL: whatever host/port you gave `conductor serve`. Default `http://127.0.0.1:8080`.

When `api.auth_token` is set in the config, all `/api/v1/*` routes require `Authorization: Bearer <token>`. `/health` and `/metrics` stay unauthenticated so infrastructure can probe them.

## `GET /health`

```json
{
  "status": "ok",
  "version": "0.1.0",
  "uptime_seconds": 42.5
}
```

## `GET /metrics`

Prometheus text format. See [Observability](../operations/observability.md).

## `GET /api/v1/backends`

```json
{"backends": ["claude", "ollama", "openai"]}
```

## `POST /api/v1/tasks`

Submit a task for asynchronous execution. Returns 202 with the initial task state.

```json
// request
{
  "prompt": "explain this function",
  "repo": "my-service",        // optional — triggers repo-override routing
  "backend": "claude",         // optional — force a specific backend
  "model": "claude-opus-4-7"   // optional — force a specific model
}
```

## `GET /api/v1/tasks`

List all tasks known to the daemon (in-memory, not durable across restarts).

## `GET /api/v1/tasks/{id}`

Fetch a single task by id. Returns 404 if not found.

## `GET /api/v1/cost`

```json
{
  "month_to_date_usd": 12.34,
  "by_backend": {"claude": 10.00, "openai": 2.34, "ollama": 0.0}
}
```

## `GET /api/v1/events` (WebSocket)

Streams task lifecycle events as JSON frames. Pass the auth token as `?token=` because browser WebSocket APIs can't set headers.

```
wss://host/api/v1/events?token=s3cret
```

Each frame:

```json
{"kind":"task_started","ts":"2026-04-19T00:12:30Z","payload":{...}}
```

Slow subscribers are dropped rather than blocking the daemon. Consume promptly.
