# Observability

## Metrics

Maxwell-Daemon exposes Prometheus metrics at `/metrics`:

| Metric                                    | Type      | Labels                  |
|-------------------------------------------|-----------|-------------------------|
| `maxwell_daemon_requests_total`                | counter   | backend, model, status  |
| `maxwell_daemon_tokens_total`                  | counter   | backend, model          |
| `maxwell_daemon_request_cost_usd_total`        | counter   | backend, model          |
| `maxwell_daemon_request_duration_seconds`      | histogram | backend, model          |

`status` is one of `success`, `error`, or `budget_exceeded`. Token and cost counters only increment for successful requests so failed requests don't pollute spend dashboards.

## Logs

Structured via `structlog`. JSON output when stderr isn't a TTY (ship it straight to Loki / ELK / Datadog), pretty console otherwise.

Bind request-scoped context with `maxwell_daemon.logging.bind_context`:

```python
from maxwell_daemon.logging import bind_context, get_logger

log = get_logger(__name__)
with bind_context(request_id=req.id, repo="my-repo"):
    log.info("starting agent")
```

Every log line inside the `with` block carries `request_id` and `repo` — even from library code that's unaware of the context.

## Events

`GET /api/v1/events` is a WebSocket that streams task lifecycle events as JSON:

```json
{"kind":"task_started","ts":"2026-04-19T00:12:30Z","payload":{"id":"abc123","prompt":"..."}}
{"kind":"task_completed","ts":"2026-04-19T00:12:31Z","payload":{"id":"abc123","cost_usd":0.0012}}
```

Subscribers get their own bounded queue; slow subscribers are dropped rather than blocking the daemon. The ledger is still the durable record — events are best-effort telemetry.

## Grafana dashboard

A starter dashboard JSON is planned (Phase 7, [issue #15](https://github.com/D-sorganization/Maxwell-Daemon/issues/15)).
