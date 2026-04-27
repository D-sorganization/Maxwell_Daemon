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

### Log Levels

| Environment | Level | Rationale |
|-------------|-------|-----------|
| Development | `DEBUG` | Maximum visibility for debugging |
| Staging | `INFO` | Balanced noise/signal ratio |
| Production | `INFO` or `WARNING` | Reduce volume, focus on anomalies |

### Output Sinks

- **Console (TTY):** Pretty-printed, human-readable output
- **Console (non-TTY):** JSON-formatted, machine-parseable
- **File:** Rotating file handler with JSON formatting for log aggregation

### Configuration Examples

**Development (pretty console):**

```bash
export MAXWELL_LOG_LEVEL=DEBUG
export MAXWELL_LOG_FORMAT=console
```

**Production (JSON to file):**

```bash
export MAXWELL_LOG_LEVEL=INFO
export MAXWELL_LOG_FORMAT=json
export MAXWELL_LOG_FILE=/var/log/maxwell-daemon/app.log
```

**Redaction control:**

```bash
# Disable secret redaction (NOT recommended in production)
export MAXWELL_REDACT_LOGS=0
```

### Log Fields

Every log line includes:
- `timestamp` — ISO 8601 format
- `level` — log level
- `logger` — logger name
- `event` — log message
- Custom bound context variables

### Audit Log

See `maxwell_daemon/audit.py` for the append-only JSONL audit log with SHA-256 chaining. This is separate from application logging and provides tamper-evident records of all significant operations.

## Events

`GET /api/v1/events` is a WebSocket that streams task lifecycle events as JSON:

```json
{"kind":"task_started","ts":"2026-04-19T00:12:30Z","payload":{"id":"abc123","prompt":"..."}}
{"kind":"task_completed","ts":"2026-04-19T00:12:31Z","payload":{"id":"abc123","cost_usd":0.0012}}
```

Subscribers get their own bounded queue; slow subscribers are dropped rather than blocking the daemon. The ledger is still the durable record — events are best-effort telemetry.

## Grafana dashboard

A starter dashboard JSON is planned (Phase 7, [issue #15](https://github.com/D-sorganization/Maxwell-Daemon/issues/15)).
