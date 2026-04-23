# Resource-Aware Routing Walkthrough

Maxwell-Daemon is designed to help a home user spend the right resource on the
right job: local models for cheap mechanical work, subscription-backed CLIs for
interactive coding capacity, and paid API models only when the task needs them.

This walkthrough documents the current operator path. It covers the shipped
configuration and runtime surfaces, then calls out the remaining automation
boundary so agents do not overstate subscription-aware routing.

## Current Routing Surfaces

| Surface | What it can do today |
| --- | --- |
| Backend config | Names each provider, model, pricing override, tier map, and optional fallback backend. |
| Repo overrides | Route a repo to a specific backend or model without changing the global default. |
| Explicit task overrides | Force a backend or model for one REST-submitted task or issue dispatch. |
| Budget enforcement | Track month-to-date cost, alert on thresholds, and hard-stop new work after the limit. |
| Backend fallback contract | `BackendRouter.route(..., budget_percent=...)` can switch to `fallback_backend` when a caller supplies budget utilisation. |
| Resource broker contract | `ResourceBroker` ranks accounts, quotas, capabilities, local preference, and budget policy as a pure decision model. |

The background daemon path enforces hard budget limits before running a task, but
does not yet automatically feed live monthly utilisation into every router call
or poll every subscription provider for remaining quota. Until that integration
is complete, use repo overrides, explicit REST overrides, budget hard stops, and
the resource broker contract as the operator-controlled route.

## 1. Name the Available Resources

Use stable local backend names that describe what the user is spending, not only
the provider. A home user should be able to see whether a task is using local
compute, a fixed subscription, or metered API spend.

```yaml
backends:
  local:
    type: ollama
    model: llama3.1
    base_url: http://localhost:11434

  claude_cli:
    type: claude_code
    model: sonnet

  openai_api:
    type: openai
    model: gpt-5.4-mini
    api_key: ${OPENAI_API_KEY}
    cost_per_million_input_tokens: 0.15
    cost_per_million_output_tokens: 0.60
    fallback_backend: local
    fallback_threshold_percent: 80.0

agent:
  default_backend: local
```

The default is intentionally cheap. Escalate only when a repo, task, or critic
needs a stronger model.

## 2. Add Repo-Level Defaults

Repo overrides are the safest route for recurring background work because the
daemon can route without the operator remembering flags on every dispatch.

```yaml
repos:
  - name: docs-scratch
    path: ~/work/docs-scratch
    backend: local
    model: llama3.1
    tags: [docs, low-risk]
    test_command: ["python", "-m", "mkdocs", "build", "--strict"]

  - name: production-api
    path: ~/work/production-api
    backend: claude_cli
    model: sonnet
    tags: [production, code-edit]
    test_command: ["python", "-m", "pytest", "tests/unit", "-q"]
```

Routing precedence is:

1. explicit backend or model in the task payload;
2. repo-level backend or model;
3. `agent.default_backend` and the backend's default model.

## 3. Set Budget Gates

Budget gates should stop runaway background work before the user burns through a
metered account.

```yaml
budget:
  monthly_limit_usd: 25.00
  alert_thresholds: [0.50, 0.80, 1.0]
  hard_stop: true
  per_task_limit_usd: 1.50
```

Check the ledger before a batch:

```bash
maxwell-daemon cost
curl -fsS -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  http://127.0.0.1:8080/api/v1/cost | jq
```

If `hard_stop` is true and spend is over the monthly limit, the daemon refuses
new work instead of silently switching to a paid backend. If a task needs to
continue, lower the task scope, move the repo to `local`, or explicitly choose a
different configured backend.

## 4. Smoke-Test the Route

Use `ask` to confirm which backend will run before queueing background work.

```bash
maxwell-daemon ask "Summarize the pending docs changes." --no-stream
maxwell-daemon ask "Summarize the pending docs changes." \
  --backend local \
  --model llama3.1 \
  --no-stream
```

The CLI prints the selected backend, model, and route reason before the prompt
runs. Do this for every new backend before adding it to a recurring issue sweep.

## 5. Dispatch an Issue with an Explicit Override

The issue CLI currently routes through repo or global defaults. The REST API can
pin a backend and model for one issue dispatch when the operator wants a one-off
escalation or downgrade.

```bash
curl -fsS -X POST http://127.0.0.1:8080/api/v1/issues/dispatch \
  -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "D-sorganization/Maxwell-Daemon",
    "number": 42,
    "mode": "plan",
    "backend": "local",
    "model": "llama3.1",
    "priority": 100
  }' | jq
```

Use explicit paid-model overrides for narrow work only. A good paid-model task
has a clear issue, acceptance criteria, source-controlled checks, and a small
expected diff.

## 6. Use Fleet Capacity Before Costly Work

Resource-aware routing is not only model choice. The selected machine needs the
tooling and capacity for the requested work.

```bash
maxwell-daemon fleet nodes \
  --repo D-sorganization/Maxwell-Daemon \
  --tool pytest \
  --required-capability python \
  --token "$MAXWELL_API_TOKEN"
```

Prefer local or already-paid resources when they satisfy the task. Escalate to a
paid API backend only for roles that need it, such as architecture, security
review, long-context analysis, or final critic passes.

## 7. Interpret the Resource Broker Contract

`ResourceBroker` is the contract for the fuller subscription-aware router. It is
pure: callers provide accounts, capability profiles, and quota snapshots, then
the broker returns a redacted routing decision.

The broker can represent:

- `ResourceAccount`: provider id, integration kind, auth status, terms mode,
  monthly budget, and disabled state;
- `CapabilityProfile`: backend id, capability tags, context size, estimated
  cost, latency, and concurrency;
- `QuotaSnapshot`: captured quota, confidence, source, reset time, and
  month-to-date spend;
- `RoutingPolicy`: allowed and forbidden providers, local preference, hard or
  soft budget mode, and role-to-capability requirements.

Use its decision shape when designing dashboard or automation work:

```json
{
  "runnable": true,
  "provider_id": "local",
  "backend_id": "ollama",
  "reason_codes": ["prefer_local", "role_capability_required", "selected"],
  "estimated_cost_usd": 0.0,
  "quota_impact": {},
  "alternatives": [],
  "fallback_plan": []
}
```

The decision is safe to show in a dashboard because `to_dict()` excludes account
secrets.

## Operator Rules

- Default to the cheapest capable backend.
- Use repo overrides for recurring work; use explicit REST overrides for one
  task.
- Treat `fallback_backend` as a router contract unless the caller supplies live
  `budget_percent`.
- Keep `hard_stop: true` for unattended background sweeps.
- Escalate model strength by role: planner, implementer, critic, security, and
  release readiness do not need the same model.
- Record routing evidence in the PR or task artifacts when a paid backend is
  chosen.

## Remaining Automation Boundary

The market-leading target is automatic subscription juggling: Maxwell should
observe usage, quota confidence, task role, local fleet capacity, and repo risk,
then choose the best delegate without the user micromanaging products.

That requires follow-up integration beyond this walkthrough:

- poll provider-specific quota where official APIs or CLIs make that reliable;
- feed budget utilisation into daemon task routing, not only direct router
  callers;
- map issue roles to `RoutingPolicy` requirements;
- expose `ResourceBroker` decisions in the gate dashboard;
- record selected alternatives and rejected alternatives as durable artifacts;
- fail closed when quota data is stale or provider terms prohibit automation.
