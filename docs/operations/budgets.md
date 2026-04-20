# Budgets

Maxwell-Daemon records every request's USD cost in a SQLite ledger and can enforce a monthly cap.

## Config

```yaml
budget:
  monthly_limit_usd: 200.0
  alert_thresholds: [0.75, 0.9, 1.0]
  hard_stop: true
```

- `monthly_limit_usd` — cap for calendar month-to-date spend. Omit for unlimited.
- `alert_thresholds` — fractions of the cap at which the status flips to `alert`. Consumers of `/api/v1/cost` can poll this to trigger notifications.
- `hard_stop` — when true, the daemon refuses new tasks once spend exceeds the cap (returns a failed task with `budget_exceeded`). When false, alerts fire but spend continues.

## Checking status

```bash
maxwell-daemon cost
```

Or over the API:

```bash
curl -s localhost:8080/api/v1/cost | jq
```

## Model selection for cost control

The cheapest way to stay under budget is to route the right work to the right backend. A typical split:

- Pattern-matching, mechanical diffs, refactors → **Ollama** (free).
- General delivery, PR review → **Sonnet** or **gpt-4o-mini**.
- Hard reasoning, new architecture, complex bugs → **Opus** or **o1**.

Set the default backend low and override per-repo where something expensive is justified — rather than the other way around.

```yaml
agent:
  default_backend: local    # Ollama by default

repos:
  - name: prod-critical
    path: ~/work/prod
    backend: claude
    model: claude-opus-4-7  # only spend Opus money here
```
