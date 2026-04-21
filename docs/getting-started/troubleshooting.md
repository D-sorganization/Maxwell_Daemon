# Troubleshooting

Start with the smallest failing command and work outward: config load, backend
health, API health, then task execution.

## Config Does Not Load

Check which config file is being used:

```bash
echo "$MAXWELL_CONFIG"
maxwell-daemon status
```

Common causes:

- Environment variables referenced as `${VAR}` are unset.
- YAML indentation changed the shape of `backends`, `agent`, or `repos`.
- A repo override names a backend that is not registered.

## Backend Health Fails

Run:

```bash
maxwell-daemon health
```

For remote APIs, confirm the key is present in the same shell that runs the
daemon. For Ollama, confirm the server is up:

```bash
curl http://localhost:11434/api/tags
```

## Tasks Stay Queued

Check that the runner is active:

```bash
maxwell-daemon tasks list
maxwell-daemon-runner
```

If a runner exits immediately, inspect the logs for config errors or missing
repository paths. Repository paths are resolved on the worker machine, not on
the coordinator that created the task.

## API Returns 401

If `api.auth_token` is set, every `/api/v1/*` request must include:

```http
Authorization: Bearer <token>
```

The `/health` and `/metrics` endpoints are intentionally unauthenticated for
infrastructure probes.

## WebSocket Events Disconnect

Slow subscribers are dropped so they do not block the daemon. Consume events
promptly and reconnect from the last task state your client has persisted.

Browser WebSocket APIs cannot set authorization headers, so pass the API token
as a query parameter:

```text
wss://host/api/v1/events?token=<token>
```
