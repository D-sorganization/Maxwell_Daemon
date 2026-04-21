# Configuration Reference

Maxwell-Daemon reads YAML configuration from
`~/.config/maxwell-daemon/maxwell-daemon.yaml` unless `MAXWELL_CONFIG` points to
another file.

Environment references in the form `${VAR}` and `${VAR:-default}` are expanded
when the config is loaded.

## Top-Level Keys

| Key | Required | Purpose |
| --- | --- | --- |
| `version` | Yes | Configuration schema version. Use `"1"`. |
| `backends` | Yes | Named LLM backend definitions. |
| `agent` | Yes | Default routing and loop behavior. |
| `repos` | No | Per-repository routing overrides and worker slots. |
| `fleet` | No | Multi-machine discovery and capacity metadata. |
| `api` | No | HTTP API host, port, and authentication settings. |
| `budget` | No | Monthly cost limits and alert thresholds. |

## Backend Fields

Every backend has a registry `type` and provider-specific fields.

```yaml
backends:
  claude:
    type: claude
    model: claude-sonnet-4-6
    api_key: ${ANTHROPIC_API_KEY}
    enabled: true
```

Common fields:

| Field | Purpose |
| --- | --- |
| `type` | Adapter name registered by Maxwell-Daemon. |
| `model` | Provider model identifier. |
| `api_key` | Secret token, usually provided from the environment. |
| `base_url` | Endpoint for local or OpenAI-compatible providers. |
| `enabled` | Set false to keep a backend configured but unavailable. |

## Agent Fields

```yaml
agent:
  default_backend: claude
  max_turns: 200
  discovery_interval_seconds: 300
  delivery_interval_seconds: 60
  reasoning_effort: medium
  temperature: 1.0
```

`default_backend` must match a key under `backends`.

## Repository Overrides

Repository entries let you route work by repo name.

```yaml
repos:
  - name: my-service
    path: ~/work/my-service
    slots: 2
    backend: claude
    model: claude-opus-4-7
```

Routing precedence is:

1. Explicit backend or model passed with the task.
2. Repository-level backend or model.
3. Global agent default.

## API Settings

```yaml
api:
  enabled: true
  host: 127.0.0.1
  port: 8080
  auth_token: ${MAXWELL_API_TOKEN}
```

When `auth_token` is configured, `/api/v1/*` routes require bearer-token
authentication. `/health` and `/metrics` remain available for infrastructure
probes.

## Budget Settings

```yaml
budget:
  monthly_limit_usd: 200.0
  alert_thresholds: [0.75, 0.9, 1.0]
  hard_stop: false
```

Set `hard_stop: true` when exceeding the monthly budget should block additional
paid backend calls instead of only alerting.
