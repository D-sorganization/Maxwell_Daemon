# Configuration Reference

Maxwell-Daemon loads YAML from
`~/.config/maxwell-daemon/maxwell-daemon.yaml` by default. Set
`MAXWELL_CONFIG=/path/to/maxwell-daemon.yaml` to load a different file.

Environment references in the form `${VAR}` and `${VAR:-default}` are expanded
before validation. The loader validates the resulting document against the
Pydantic config models and rejects unknown fields.

For provider API keys, Maxwell also supports `api_key_secret_ref`. When the
loader sees a literal plaintext `backends.<name>.api_key`, it migrates that
value into the OS keyring, rewrites the YAML to `api_key_secret_ref`, and keeps
the raw key out of subsequent saves.

## Configuration Surface Map

Maxwell-Daemon reads configuration from five distinct surfaces. They do **not**
override one another field-by-field — each owns a different concern. The table
lists every surface, its format, default location, and how to point at it (#989).

| Surface | Format | Default location | Selected by | Owns |
| --- | --- | --- | --- | --- |
| Main daemon config | **YAML** | `$XDG_CONFIG_HOME/maxwell-daemon/maxwell-daemon.yaml` (≈ `~/.config/maxwell-daemon/maxwell-daemon.yaml`) | `MAXWELL_CONFIG` env var → default path | Backends, agent loop, budget, memory, role. |
| Fleet manifest | **YAML** | `./fleet.yaml`, then `~/.maxwell-daemon/fleet.yaml` | `MAXWELL_FLEET_CONFIG` → `./fleet.yaml` → `~/.maxwell-daemon/fleet.yaml` | The repos the fleet manages + shared defaults. |
| Environment / `.env` | `KEY=value` | process env; `.env` for local dev (see `.env.example`) | the shell / process manager | Secrets, `MAXWELL_*` paths and overrides, `${VAR}` substitution into the YAML above. |
| CI ratchets / budgets | **JSON** | `scripts/config/*.json` (e.g. `coverage_floor.json`, `file_size_budget.json`) | committed in-repo | CI gate thresholds; not runtime config. |
| Per-workspace hooks | **YAML** | `<workspace>/.maxwell/workspace_hooks.yaml` | presence in a repo workspace; falls back to `workspace_hooks` in the main config | Lifecycle hook commands run around task execution. |

**Precedence notes**

- The main config file is chosen by the single `MAXWELL_CONFIG` env var; if unset,
  the default path above is used. The on-disk format is **YAML** — there is no
  TOML config file (a common doc error fixed in #989).
- `${VAR}` / `${VAR:-default}` references inside the YAML are resolved from the
  process environment (surface 3) before validation.
- The fleet manifest is independent of the main config and resolved by its own
  order (explicit path → `MAXWELL_FLEET_CONFIG` → `./fleet.yaml` →
  `~/.maxwell-daemon/fleet.yaml`); see [SPEC.md](../../SPEC.md) for its schema.
- Workspace hooks prefer the per-repo `.maxwell/workspace_hooks.yaml` and fall
  back to the `workspace_hooks` block in the main config.

## Minimal Configuration

```yaml
version: "1"
backends:
  claude:
    type: claude
    model: claude-sonnet-4-6
    api_key: ${ANTHROPIC_API_KEY}
agent:
  default_backend: claude
```

`backends` must contain at least one entry, and `agent.default_backend` must
refer to one of those backend names.

## Top-Level Keys

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `version` | string | `"1"` | Configuration schema version. |
| `role` | `standalone`, `coordinator`, or `worker` | `standalone` | Runtime role for single-node or fleet operation. |
| `backends` | map of backend objects | required | Named LLM backend definitions. |
| `agent` | object | default object | Agent loop timing, routing, and generation defaults. |
| `memory` | object | default object | Local memory workspace and background cycle settings. |
| `tools` | object | default object | Tool approval mode. |
| `repos` | list of repo objects | `[]` | Per-repository routing and executor overrides. |
| `fleet` | object | default object | Worker discovery, capacity, and coordinator settings. |
| `api` | object | default object | HTTP API listener, auth, TLS, JWT, and rate limiting. |
| `budget` | object | default object | Cost limits, alerting, and per-task caps. |
| `github` | object | default object | GitHub webhook allowlists and routing rules. |

## Backends

Backend map keys are local names used by routing rules. Each value has the
following fields.

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `type` | string | required | Backend adapter name, such as `claude`, `openai`, `ollama`, `google`, or `azure`. |
| `model` | string | required | Default provider model identifier. |
| `api_key` | string | `null` | Secret token, usually loaded from an environment variable. |
| `api_key_secret_ref` | string | `null` | Keyring-backed secret reference used instead of storing the raw key in YAML. |
| `base_url` | string | `null` | Base endpoint for local or OpenAI-compatible providers. |
| `enabled` | boolean | `true` | Set `false` to keep a backend configured but unavailable. |
| `tier_map` | map | `{}` | Maps model tiers such as `simple`, `moderate`, and `complex` to provider model ids. |
| `fallback_backend` | string | `null` | Backend name to use after the configured spend threshold is reached. |
| `fallback_threshold_percent` | number | `80.0` | Budget percentage that activates `fallback_backend`. Must be between `0` and `100`. |
| `cost_per_million_input_tokens` | number | `null` | Override input token pricing for cost tracking. |
| `cost_per_million_output_tokens` | number | `null` | Override output token pricing for cost tracking. |

```yaml
backends:
  claude:
    type: claude
    model: claude-sonnet-4-6
    api_key_secret_ref: maxwell-daemon/backends/claude/api_key
    tier_map:
      simple: claude-haiku-4-5
      complex: claude-opus-4-7
    fallback_backend: local
    fallback_threshold_percent: 80.0

  local:
    type: ollama
    model: llama3.1
    base_url: http://localhost:11434
```

Every `fallback_backend` must refer to another configured backend name.

## Agent

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `default_backend` | string | `claude` | Must match a key under `backends`. |
| `max_turns` | integer | `200` | At least `1`. |
| `discovery_interval_seconds` | integer | `300` | At least `10`. |
| `delivery_interval_seconds` | integer | `60` | At least `10`. |
| `task_retention_days` | integer | `30` | At least `0`; `0` disables pruning. |
| `task_prune_interval_seconds` | integer | `86400` | At least `60`. |
| `reasoning_effort` | `low`, `medium`, or `high` | `medium` | Passed through to compatible backends. |
| `temperature` | number | `1.0` | Between `0.0` and `2.0`. |

```yaml
agent:
  default_backend: claude
  max_turns: 200
  discovery_interval_seconds: 300
  delivery_interval_seconds: 60
  task_retention_days: 30
  task_prune_interval_seconds: 86400
  reasoning_effort: medium
  temperature: 1.0
```

## Memory

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `workspace_path` | path | `~/.local/share/maxwell-daemon` | Workspace root containing `.maxwell/memory` and `.maxwell/raw_logs`. |
| `dream_interval_seconds` | integer | `0` | Seconds between background memory anneal cycles; `0` disables cycles. |

```yaml
memory:
  workspace_path: ~/.local/share/maxwell-daemon
  dream_interval_seconds: 0
```

## Tools

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `approval_tier` | `suggest`, `auto-edit`, or `full-auto` | `full-auto` | Controls how aggressively tools may act. |

## Repositories

Repository entries let Maxwell-Daemon route tasks and tune issue-executor
behavior by local project.

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `name` | string | required | Repository name used by routing and display code. |
| `path` | path | required | Local checkout path. `~` is expanded. |
| `slots` | integer | `2` | Maximum concurrent agents for the repo. Must be between `1` and `16`. |
| `backend` | string | `null` | Backend override for this repo. Must exist under `backends` when set. |
| `model` | string | `null` | Model override for this repo. |
| `tags` | list of strings | `[]` | Free-form routing or inventory tags. |
| `test_command` | list of strings | `null` | Repo-specific test command for issue execution. |
| `context_max_chars` | integer | `null` | Maximum context characters gathered for issue execution. |
| `max_test_retries` | integer | `null` | Retry cap for failing test loops. |
| `max_diff_retries` | integer | `null` | Retry cap for unacceptable diff loops. |
| `system_prompt_prefix` | string | `null` | Text prepended to the default agent system prompt. |
| `system_prompt_file` | path | `null` | Markdown file whose content is prepended to the system prompt. |

```yaml
repos:
  - name: my-service
    path: ~/work/my-service
    slots: 2
    backend: claude
    model: claude-opus-4-7
    tags: [production]
    test_command: ["python", "-m", "pytest", "tests/unit"]
```

Routing precedence is:

1. Explicit backend or model passed with the task.
2. Repository-level backend or model.
3. `agent.default_backend` and that backend's default `model`.

## Fleet

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `discovery_method` | `manual` or `mdns` | `manual` | How workers are discovered. |
| `heartbeat_seconds` | integer | `30` | Worker heartbeat interval. Must be at least `5`. |
| `coordinator_poll_seconds` | integer | `30` | Worker poll interval for coordinator tasks. Must be at least `5`. |
| `coordinator_url` | string | `null` | Coordinator daemon URL, such as `https://coordinator:8080`. |
| `machines` | list of machine objects | `[]` | Static worker inventory. |

Machine fields:

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `name` | string | required | Machine identifier. |
| `host` | string | `localhost` | Hostname or IP address. |
| `port` | integer | `50051` | Worker port. |
| `capacity` | integer | `2` | Number of concurrent work slots. Must be at least `1`. |
| `tags` | list of strings | `[]` | Scheduling and inventory tags. |
| `ssh_key` | path | `null` | Optional SSH identity path. |
| `tls` | boolean | `true` | Use HTTPS for worker communication. |
| `tls_verify` | boolean | `true` | Verify TLS certificates. |

```yaml
fleet:
  discovery_method: manual
  heartbeat_seconds: 30
  coordinator_poll_seconds: 30
  coordinator_url: https://coordinator.example.internal:8080
  machines:
    - name: worker-1
      host: worker-1.example.internal
      port: 50051
      capacity: 4
      tags: [gpu, linux]
```

## API

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Enable the HTTP API. |
| `host` | string | `127.0.0.1` | Bind host. |
| `port` | integer | `8080` | Bind port. Must be between `1` and `65535`. |
| `auth_token` | string | `null` | Static bearer token for `/api/v1/*` routes. |
| `tls_cert` | path | `null` | TLS certificate path. |
| `tls_key` | path | `null` | TLS private key path. |
| `jwt_secret` | string | `null` | HMAC secret for JWT role-based access control. |
| `jwt_expiry_seconds` | integer | `3600` | Default JWT lifetime in seconds. |
| `rate_limit_default` | rate-limit object | `null` | Default token bucket for API requests. |
| `rate_limit_groups` | map of rate-limit objects | `{}` | Per-group token buckets. |

Rate-limit objects contain:

| Field | Type | Default | Constraints |
| --- | --- | --- | --- |
| `rate` | number | `10.0` | Tokens refilled per second. Must be greater than `0`. |
| `burst` | integer | `50` | Bucket capacity. Must be at least `1`. |

```yaml
api:
  enabled: true
  host: 127.0.0.1
  port: 8080
  auth_token: ${MAXWELL_API_TOKEN}
  jwt_secret: ${MAXWELL_JWT_SECRET}
  jwt_expiry_seconds: 3600
  rate_limit_default:
    rate: 10.0
    burst: 50
  rate_limit_groups:
    admins:
      rate: 50.0
      burst: 200
```

When `auth_token` is configured, `/api/v1/*` routes require bearer-token
authentication. `/health` and `/metrics` remain available for infrastructure
probes. Use `jwt_secret` when role-based API access is needed.

## Budget

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `monthly_limit_usd` | number | `null` | Monthly spend cap used by budget enforcement. |
| `alert_thresholds` | list of numbers | `[0.75, 0.90, 1.0]` | Fractions of the monthly limit that trigger alerts. |
| `hard_stop` | boolean | `false` | Refuse requests that would exceed the budget. |
| `alert_webhook_url` | string | `null` | URL that receives alert POSTs. |
| `alert_warn_multiplier` | number | `1.1` | Forecast multiplier that triggers warning alerts. Must be greater than `1.0`. |
| `alert_debounce_hours` | integer | `6` | Minimum hours between repeated alerts. |
| `per_task_limit_usd` | number | `null` | Hard cap for a single task or agent-loop invocation. |

```yaml
budget:
  monthly_limit_usd: 200.0
  alert_thresholds: [0.75, 0.9, 1.0]
  hard_stop: false
  alert_webhook_url: ${MAXWELL_BUDGET_WEBHOOK:-}
  alert_warn_multiplier: 1.1
  alert_debounce_hours: 6
  per_task_limit_usd: 20.0
```

## GitHub Webhooks

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `webhook_secret` | string | `null` | Secret used to validate GitHub webhook signatures. |
| `allowed_repos` | list of strings | `[]` | Repository allowlist, usually `owner/name` values. |
| `routes` | list of route objects | `[]` | Event routing table. |

Webhook route fields:

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `event` | string | required | GitHub event name, such as `issues` or `issue_comment`. |
| `action` | string | required | GitHub event action, such as `opened`, `labeled`, or `created`. |
| `mode` | `plan` or `implement` | `plan` | Whether the route plans work or dispatches implementation. |
| `label` | string | `null` | Required issue label. |
| `trigger` | string | `null` | Required comment substring. |

```yaml
github:
  webhook_secret: ${GITHUB_WEBHOOK_SECRET}
  allowed_repos:
    - D-sorganization/Maxwell-Daemon
  routes:
    - event: issues
      action: opened
      mode: plan
      label: agent-ready
    - event: issue_comment
      action: created
      mode: implement
      trigger: "@maxwell implement"
```

## Validation Rules

- Unknown keys are rejected at the top level and inside each backend.
- `backends` cannot be empty.
- `agent.default_backend`, repo `backend`, and backend `fallback_backend` values
  must reference configured backend names.
- Paths in `repos[].path` and `memory.workspace_path` expand `~`.
- Numeric fields enforce the bounds shown in the tables above.
