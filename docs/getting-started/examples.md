# Examples

These examples show common Maxwell-Daemon workflows from a fresh checkout. Each
one is intentionally small enough to copy into a local sandbox before wiring the
same pattern into a fleet or GitHub automation.

## Smoke Test a Backend

Use `health` first so credential or network failures are visible before a task
enters the queue.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
maxwell-daemon health
maxwell-daemon ask "Summarize this repository in five bullets"
```

Pin the backend and model when comparing adapters:

```bash
maxwell-daemon ask "List risky files changed on this branch" \
  --backend claude \
  --model claude-3-5-sonnet-latest \
  --no-stream
```

Run the same prompt against a local backend when the task is repetitive and does
not need a frontier model:

```bash
maxwell-daemon ask "Find stale links in docs/" --backend local --model llama3.1
```

## Route Cheap Work to a Local Model

Use repo overrides when a repository has repetitive maintenance work that does
not need the default backend.

```yaml
agent:
  default_backend: claude

backends:
  - name: claude
    provider: anthropic
    model: claude-3-5-sonnet-latest
  - name: local
    provider: ollama
    model: llama3.1

repos:
  - name: docs-scratch
    path: ~/work/docs-scratch
    backend: local
    model: llama3.1
```

Confirm the override is loaded, then smoke-test the local backend directly:

```bash
maxwell-daemon status
maxwell-daemon health
maxwell-daemon ask "Find stale links in docs/" --backend local --model llama3.1
```

## Queue GitHub Issue Work

Use the issue CLI when you want issue-driven implementation against a running
daemon instead of a one-shot prompt.

```bash
maxwell-daemon serve
maxwell-daemon issue dispatch example/my-service 42 --mode implement
maxwell-daemon tasks list --kind issue --repo example/my-service
maxwell-daemon-runner
```

The runner resolves repository context, selects a backend, runs the task, and
records the result in the local task store.

For a short planning pass, keep the issue in plan mode:

```bash
maxwell-daemon issue dispatch example/my-service 42 --mode plan
maxwell-daemon tasks list --status queued
maxwell-daemon tasks show <task-id>
```

Use a small batch file when copying work directly from GitHub notifications:

```text
example/my-service#42:implement
example/web#108:plan
```

Then submit the batch:

```bash
maxwell-daemon issue dispatch-batch --from-file issues.txt --mode plan
```

## Batch Dispatch a Fleet Manifest

Create a `fleet.yaml` when the same issue sweep should run across multiple
repositories with shared defaults and per-repo overrides.

```yaml
version: 1
fleet:
  name: home-lab
  default_slots: 2
  default_budget_per_story: 0.75
  default_pr_target_branch: staging
  default_pr_fallback_to_default: true
  default_watch_labels: [maxwell:ready]

repos:
  - org: example
    name: api
    enabled: true
    watch_labels: [bug, small, maxwell:ready]
    budget_per_story: 0.50
  - org: example
    name: web
    enabled: true
    watch_labels: [documentation]
```

Preview the plan before creating tasks:

```bash
maxwell-daemon issue dispatch-batch \
  --fleet-manifest fleet.yaml \
  --all \
  --label maxwell:ready \
  --max-stories 1 \
  --dry-run
```

Dispatch only after the plan has the expected repositories, label filter, cap,
and mode:

```bash
maxwell-daemon issue dispatch-batch \
  --fleet-manifest fleet.yaml \
  --all \
  --label maxwell:ready \
  --max-stories 1 \
  --mode plan
maxwell-daemon tasks list --kind issue --status queued
```

See the [fleet issue queue walkthrough](fleet-issue-queue.md) for the full
operator flow, scheduler boundaries, and safety gates.

## Review Approval-Gated Actions

Tasks that want to perform write-side effects can expose approval actions. Keep
the daemon running, inspect the task, then approve or reject the action by id.

```bash
maxwell-daemon tasks show <task-id>
maxwell-daemon tasks actions <task-id>
maxwell-daemon action show <action-id>
maxwell-daemon action approve <action-id>
```

Reject the action when the proposed change is too broad or the evidence is weak:

```bash
maxwell-daemon action reject <action-id> --reason "Needs narrower reproduction"
```

## Run Repository Checks Locally

Define repository-carried checks in `.maxwell/checks.yaml`, then use the checks
CLI to list and run the same commands Maxwell will enforce around agent work.

```bash
maxwell-daemon checks list --repo .
maxwell-daemon checks run --repo . --event pre_pr
```

Use event filtering for hooks that should only run at a specific point in the
workflow:

```yaml
checks:
  - id: unit
    name: Unit tests
    command: python -m pytest tests/unit -q
    trigger_events: [pre_pr]
  - id: docs
    name: Documentation build
    command: python -m mkdocs build --strict
    trigger_events: [docs]
```

Then run only the docs gate:

```bash
maxwell-daemon checks run --repo . --event docs
```

## Run a Small Machine Fleet

Each machine needs the same config shape and a unique fleet entry.

```yaml
fleet:
  discovery_method: manual
  heartbeat_seconds: 30
  machines:
    - name: laptop
      host: 192.168.1.20
      port: 8080
      capacity: 2
      tags: [local]
    - name: gpu-box
      host: 192.168.1.30
      port: 8080
      capacity: 6
      tags: [gpu, local]
```

Start the API on each node:

```bash
maxwell-daemon serve
```

Use the fleet status command from the coordinator to confirm nodes are visible
before dispatching long-running work:

```bash
maxwell-daemon fleet status --repo example/my-service --tool codex
```

## Track Cost While Work Runs

Use budget limits for development sandboxes and recurring automation. In strict
mode, Maxwell refuses requests that would exceed the configured limit.

```yaml
budget:
  monthly_limit_usd: 25
  alert_thresholds: [0.75, 0.9, 1.0]
  hard_stop: true
```

Check spend before and after a batch:

```bash
maxwell-daemon cost
maxwell-daemon issue dispatch example/my-service 42 --mode plan
maxwell-daemon cost
```

If the second report is unexpectedly high, lower the repo budget, route the repo
to a cheaper backend, or switch the next run to plan mode before implementation.
