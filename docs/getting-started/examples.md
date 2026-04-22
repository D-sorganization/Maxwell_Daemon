# Examples

These examples show common Maxwell-Daemon workflows from a fresh checkout.

## Smoke Test a Backend

Use `health` first so credential or network failures are visible before a task
enters the queue.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
maxwell-daemon health
maxwell-daemon ask "Summarize this repository in five bullets"
```

## Route Cheap Work to a Local Model

Use repo overrides when a repository has repetitive maintenance work that does
not need a frontier model.

```yaml
agent:
  default_backend: claude

repos:
  - name: docs-scratch
    path: ~/work/docs-scratch
    backend: local
    model: llama3.1
```

Confirm the override is loaded, then smoke-test the local backend directly:

```bash
maxwell-daemon status
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

## Run a Small Fleet

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

Use the fleet status commands from the coordinator to confirm nodes are visible
before dispatching long-running work.
