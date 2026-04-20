# Configuration reference

`~/.config/maxwell-daemon/maxwell-daemon.yaml` (override with `MAXWELL_CONFIG=...`).

All `${VAR}` and `${VAR:-default}` references are expanded at load time against the environment.

## Minimal

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

## Full shape

```yaml
version: "1"

backends:
  claude:
    type: claude              # adapter name in the registry
    model: claude-sonnet-4-6
    api_key: ${ANTHROPIC_API_KEY}
    enabled: true

  local:
    type: ollama
    model: llama3.1
    base_url: http://localhost:11434

  gpt:
    type: openai
    model: gpt-4o-mini
    api_key: ${OPENAI_API_KEY}

  azure:
    type: azure
    model: gpt-4o
    endpoint: https://my-resource.openai.azure.com
    api_key: ${AZURE_OPENAI_API_KEY}
    api_version: "2024-10-21"

agent:
  default_backend: claude
  max_turns: 200
  discovery_interval_seconds: 300
  delivery_interval_seconds: 60
  reasoning_effort: medium
  temperature: 1.0

repos:
  - name: my-service
    path: ~/work/my-service
    slots: 2
    backend: claude            # override the default for this repo
    model: claude-opus-4-7

  - name: scratchpad
    path: ~/work/scratchpad
    backend: local             # route this one to Ollama

fleet:
  discovery_method: manual     # or: mdns
  heartbeat_seconds: 30
  machines:
    - name: worker-1
      host: 192.168.1.10
      port: 8080
      capacity: 4
      tags: [production, gpu]

api:
  enabled: true
  host: 127.0.0.1
  port: 8080
  auth_token: ${MAXWELL_API_TOKEN}

budget:
  monthly_limit_usd: 200.0
  alert_thresholds: [0.75, 0.9, 1.0]
  hard_stop: false             # true → refuse requests once exceeded
```

## Precedence rules

When the router picks a backend for a task, it uses (highest first):

1. Explicit `backend=` on the API call.
2. The repo's `backend:` field.
3. The global `agent.default_backend`.

`model` follows the same order with an extra wrinkle: a repo-level `model` only applies if the repo's `backend` matches the backend being routed to.
