# Monitoring

This page enumerates the Prometheus metrics, structlog fields, and starter
alert rules that ship with Maxwell-Daemon.  It is the operator-facing
companion to [`observability.md`](observability.md), which focuses on the
developer-facing logging API.

The complete metric definitions live in
[`maxwell_daemon/metrics.py`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/maxwell_daemon/metrics.py).
Treat that module as the source of truth — this page summarises what is
currently exported.

## Scrape configuration

Maxwell-Daemon serves a Prometheus text-format endpoint at `GET /metrics`.
Add a job to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: maxwell-daemon
    metrics_path: /metrics
    static_configs:
      - targets: ["maxwell-daemon.internal:8080"]
```

If the daemon is fronted by a reverse proxy that requires authentication,
either expose `/metrics` on a separate internal listener or add the
appropriate `authorization` header in the Prometheus job.  The endpoint
itself does not enforce auth.

## Counters

| Metric | Labels | Source |
|--------|--------|--------|
| `maxwell_daemon_requests_total` | `backend`, `model`, `status` | Agent requests partitioned by outcome (`success`, `error`, `budget_exceeded`). |
| `maxwell_daemon_tokens_total` | `backend`, `model` | Total tokens consumed (prompt + completion). |
| `maxwell_daemon_request_cost_usd_total` | `backend`, `model` | Cumulative request cost in USD. |
| `maxwell_daemon_cache_hit_tokens_total` | `backend`, `model` | Tokens served from prompt cache. |
| `maxwell_daemon_free_requests_total` | `backend`, `model` | Successful requests with verified zero billed cost. |
| `maxwell_daemon_gate_verdicts_total` | `verdict`, `severity` | Gate verdicts emitted by the policy layer. |
| `maxwell_ratelimit_rejected_total` | `route_class` | HTTP requests rejected by the per-IP rate limiter. |
| `maxwell_daemon_http_requests_total` | `method`, `endpoint`, `status` | All HTTP requests served by the FastAPI app. |

## Gauges

| Metric | Labels | Description |
|--------|--------|-------------|
| `maxwell_daemon_token_budget_allocation` | `task_id`, `budget_remaining`, `model_chosen` | Safe budget allocation for a task in USD. |
| `maxwell_daemon_cost_forecast_usd` | — | Linear month-end spend forecast from the cost ledger. |
| `maxwell_daemon_active_tasks` | — | Tasks currently in a non-terminal state. |
| `maxwell_daemon_live_tasks_dict_size` | — | Tasks held in the hot in-memory dict. |
| `maxwell_ledger_connections_in_use` | — | Active SQLite connections in the ledger pool. |
| `maxwell_daemon_queue_depth` | — | Current depth of the task queue. |
| `maxwell_daemon_cache_hit_rate` | — | Prompt cache hit rate (0.0 – 1.0). |

## Histograms

| Metric | Labels | Buckets (seconds unless noted) |
|--------|--------|-------------------------------|
| `maxwell_daemon_request_duration_seconds` | `backend`, `model` | 0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300 |
| `maxwell_daemon_queue_latency_ms` | — | 0.1, 0.5, 1, 2.5, 5, 10 (milliseconds) |
| `maxwell_daemon_http_request_duration_seconds` | `endpoint` | 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10 |

When computing percentiles, sum buckets across `instance` first, then apply
`histogram_quantile`:

```promql
histogram_quantile(
  0.95,
  sum by (le, endpoint) (rate(maxwell_daemon_http_request_duration_seconds_bucket[5m]))
)
```

## Structlog fields

Maxwell-Daemon logs through `structlog` (configured in
[`maxwell_daemon/logging.py`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/maxwell_daemon/logging.py)).
Output is JSON when stderr is not a TTY, which is the format you should
ship to Loki, Elastic, Datadog, or Cloud Logging.

Stable fields you can index on:

| Field | Description |
|-------|-------------|
| `timestamp` | ISO-8601 UTC. |
| `level` | `debug` / `info` / `warning` / `error`. |
| `logger` | Logger name (typically the producing module). |
| `event` | The log message. |
| `request_id` | Bound by `bind_context()` for HTTP-scoped flows. |
| `task_id` | Bound when a log line is emitted inside a task lifecycle. |
| `repo` | Repo slug, when bound by the dispatch path. |

Notable `event` values worth alerting on:

| `event` | Meaning |
|---------|---------|
| `stall_detected` | Daemon noticed a task with no progress within `agent.stall_timeout_seconds`. |
| `gate_verdict` | A gate decision was recorded (paired with the `maxwell_daemon_gate_verdicts_total` counter). |
| `task_completed` | Terminal lifecycle event — also published over the `/api/v1/events` WebSocket. |

If you set `MAXWELL_REDACT_LOGS=0` the log redactor is disabled.  Do not
do this in production — secrets in tool outputs (API keys, tokens) will
appear verbatim in logs.

## Sample dashboards

A Grafana dashboard JSON is shipped at
[`deploy/grafana/maxwell-daemon-dashboard.json`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/deploy/grafana/maxwell-daemon-dashboard.json).
It covers HTTP request rate, p50/p95/p99 latency, 5xx error rate, active
tasks, queue depth, agent request rate by backend, token consumption,
cost-per-hour, and the month-end spend forecast.

To import:

1. In Grafana, choose **Dashboards → Import**.
2. Upload the JSON file.
3. Pick your Prometheus datasource for the `DS_PROMETHEUS` variable.

## Sample alerts

Starter Prometheus alert rules ship at
[`deploy/prometheus/alerts.yml`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/deploy/prometheus/alerts.yml).
They are intentionally conservative starting points — review the
thresholds before paging on them.

The shipped rule groups are:

- **availability** — `MaxwellDaemonDown` (`up == 0`),
  `MaxwellDaemonHealthEndpointFailing` (blackbox probe).
- **errors** — `MaxwellDaemonHigh5xxRate` (>5% 5xx),
  `MaxwellDaemonAgentRequestErrors` (per-backend error rate).
- **gate** — `MaxwellDaemonGateStuckClosed` (deny-only verdicts for 15
  minutes).
- **queue** — `MaxwellDaemonQueueDepthGrowing` (positive derivative + >25
  depth), `MaxwellDaemonQueueDepthHigh` (>200 absolute).
- **cost** — `MaxwellDaemonCostForecastHigh` (forecast above operator
  threshold).

## Health probes

Three first-party endpoints are intended for orchestrators and load
balancers:

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health` | none | Pure liveness probe. Returns immediately. |
| `GET /api/health` | none | Liveness + gate state.  Use this for Kubernetes `livenessProbe`. |
| `GET /api/status` | varies | Pipeline state, active task summary.  Use for `readinessProbe`. |
| `GET /api/version` | none | Semver and contract version — also used by upgrade checks. |

Liveness and contract probes (`/api/health`, `/api/version`) are exempted
from the env-driven rate limiter (Phase 1 of #796) by default. Deployments
that explicitly enable the YAML-configured limiter via
`api.rate_limit_default` should add these paths to its `exempt_paths` list
to prevent 429s on orchestrator probes — `install_rate_limiter()` (see
[`maxwell_daemon/api/rate_limit.py`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/maxwell_daemon/api/rate_limit.py))
currently defaults its exemption list to `/health` and `/metrics` only. A
follow-up under #796 will align that limiter's defaults.
