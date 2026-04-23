# Fleet Gauntlet Walkthrough

This walkthrough shows the current operator path for delegating GitHub issue
work to Maxwell-Daemon, watching the shared fleet context, and using the gate
control plane to decide whether the result can move forward.

Use it for the home-lab shape Maxwell targets first: one coordinator, one or
more worker machines on a private network, source-controlled checks in the repo,
and a human operator who wants background progress without handing the daemon
unbounded merge authority.

## Prerequisites

- `gh` is installed and authenticated for the target repository.
- `maxwell-daemon serve` is running on the coordinator.
- Worker nodes can reach the coordinator over the configured fleet transport.
  [Tailscale](../operations/tailscale.md) is the recommended private-network
  topology.
- `api.auth_token` or JWT auth is enabled before exposing any `/api/v1/*`
  endpoint beyond localhost.
- The target repo carries the checks Maxwell should enforce, such as
  `.maxwell/checks.yaml`.

The daemon does not install Tailscale, create tailnet policies, or prove that a
pull request is safe to merge by itself. The fleet and gate APIs provide the
evidence stream; the operator chooses whether to retry, waive, or promote work.

## 1. Confirm Fleet Capacity

Start by asking the coordinator which node would take work for the repo and
tool. This confirms that worker registration, Tailscale reachability, and
capability matching agree before a long-running issue task enters the queue.

```bash
maxwell-daemon fleet nodes \
  --repo D-sorganization/Maxwell-Daemon \
  --tool pytest \
  --required-capability python \
  --token "$MAXWELL_API_TOKEN"
```

The same view is available over HTTP for dashboards:

```bash
curl -fsS -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  "http://127.0.0.1:8080/api/v1/fleet/capabilities?repo=D-sorganization/Maxwell-Daemon&tool=pytest&required_capability=python" \
  | jq
```

Do not dispatch until the selected node is the machine you expect and the
rejected nodes explain why they were not eligible.

## 2. Dispatch a Small Issue

Use issue mode when the desired work already has a GitHub issue with acceptance
criteria. `plan` mode is the lowest-risk first pass. `implement` mode can create
a draft pull request when the generated diff is valid.

```bash
maxwell-daemon issue dispatch D-sorganization/Maxwell-Daemon 42 --mode plan
maxwell-daemon tasks list --kind issue --repo D-sorganization/Maxwell-Daemon
```

For implementation work:

```bash
maxwell-daemon issue dispatch D-sorganization/Maxwell-Daemon 42 --mode implement
maxwell-daemon tasks list --status queued --kind issue
```

Record the task id returned by `tasks list` or by the REST task list. The next
steps use `task-123` as a placeholder.

## 3. Assemble Shared Repo Memory

Fleet workers use the coordinator's shared memory store when the daemon runs in
worker mode with `fleet.coordinator_url` configured. Operators can inspect the
same assembled context before retrying or escalating a task.

```bash
curl -fsS -X POST http://127.0.0.1:8080/api/v1/memory/assemble \
  -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "D-sorganization/Maxwell-Daemon",
    "issue_title": "Add docs for a small feature",
    "issue_body": "Acceptance: docs build strict passes.",
    "task_id": "task-123",
    "max_chars": 8000
  }' | jq -r .context
```

This endpoint is for codebase experience: prior plans, outcomes, diffs, and
lessons that help future delegates avoid repeating mistakes. Keep API tokens,
passwords, private customer data, and machine-local secrets out of memories and
artifacts.

After a task is finished, record its outcome so future delegates can learn from
the result:

```bash
curl -fsS -X POST http://127.0.0.1:8080/api/v1/memory/record \
  -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task-123",
    "repo": "D-sorganization/Maxwell-Daemon",
    "issue_number": 42,
    "issue_title": "Add docs for a small feature",
    "issue_body": "Acceptance: docs build strict passes.",
    "plan": "Add a focused docs page and regression test.",
    "applied_diff": true,
    "pr_url": "https://github.com/D-sorganization/Maxwell-Daemon/pull/123",
    "outcome": "merged after mkdocs and unit docs checks passed"
  }' | jq
```

## 4. Inspect the Gate Control Plane

The gate control plane is the operator view of Maxwell's gauntlet. It summarizes
intake, delegate progress, verification, critic findings, selected backend,
available actions, and the next required operator move.

```bash
curl -fsS -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  "http://127.0.0.1:8080/api/v1/control-plane/gauntlet?limit=20" \
  | jq '.[] | select(.task_id == "task-123")'
```

Treat the fields as gates:

| Field | Gate question |
| --- | --- |
| `final_decision` | Can this task move forward, or is it blocked, failed, cancelled, pending, or waived? |
| `gates` | Which stage passed, failed, is running, or is blocked? |
| `critic_findings` | What adversarial review notes must be handled before promotion? |
| `delegates` | Which worker, role, backend, and session produced the work? |
| `resource_routing` | Which backend was chosen, and were cheaper or local alternatives considered? |
| `actions` | Can the operator retry or waive the failed gate? |

A completed task is not the same as a mergeable pull request. Use this view with
repo checks, CI status, and human review before marking work ready.

## 5. Review Durable Evidence

Inspect the task evidence before retrying, waiving, or promoting. Issue
execution can write plans, diffs, test output, and pull request bodies as
artifacts.

```bash
curl -fsS -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  http://127.0.0.1:8080/api/v1/tasks/task-123/artifacts \
  | jq
```

Fetch a specific artifact only after checking its kind and size:

```bash
curl -fsS -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  http://127.0.0.1:8080/api/v1/artifacts/artifact-456/content
```

The evidence should prove the acceptance criteria in the issue. If the task
claims tests passed, the artifact should include the exact command and output.
If the task opened a pull request, the artifact list should make the plan,
diff, and PR body inspectable without reconstructing transient logs.

## 6. Run the Critic Gauntlet

Maxwell's current critic panel contract lives in the core gate runtime and is
surfaced through the control plane for task-level blockers. Use an adversarial
review checklist before advancing a PR:

| Critic | Blocking examples |
| --- | --- |
| Architecture | Broad refactor outside the issue scope; new dependency boundary violation. |
| Tests | No failing test first; acceptance criteria not covered; CI logs missing. |
| Security | Secret exposure; public daemon endpoint; unsafe shell or filesystem behavior. |
| Maintainability | Duplicated policy logic; undocumented config drift; brittle string parsing. |
| Product fit | The change makes the home-user path harder or requires unnecessary subscription juggling. |
| Release readiness | Missing rollback note, migration note, or docs update for user-visible behavior. |

Blockers should lead to another delegate pass or a direct patch. Non-blocking
notes should remain visible in the PR review so the operator can decide whether
to address them now or file follow-up work.

## 7. Retry or Waive

Retry failed work when the blocker is actionable and the next run should use the
same issue contract:

```bash
curl -fsS -X POST \
  http://127.0.0.1:8080/api/v1/control-plane/gauntlet/task-123/retry \
  -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_id":"task-123","expected_status":"failed"}' | jq
```

Waive only when the operator intentionally accepts a known failed gate. Waivers
preserve the failed task state and add actor and reason metadata; they do not
rewrite the original evidence into a pass.

```bash
curl -fsS -X POST \
  http://127.0.0.1:8080/api/v1/control-plane/gauntlet/task-123/waive \
  -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target_id": "task-123",
    "expected_status": "failed",
    "actor": "operator@example.com",
    "reason": "Accepted for a docs-only draft; follow-up issue tracks strict automation."
  }' | jq
```

Do not use waivers for flaky or missing gates. Fix unreliable checks, narrow the
issue, or route to a more capable delegate instead.

## Ready-to-Promote Checklist

Before moving a delegate PR out of draft, confirm:

- the linked issue has concrete acceptance criteria;
- shared memory was assembled from the coordinator and did not include secrets;
- selected fleet node and backend are appropriate for the task;
- artifacts include the plan, diff, PR body, and exact check output;
- `GET /api/v1/control-plane/gauntlet` shows no unhandled blocking critic
  finding;
- source-controlled checks and CI passed on the PR head;
- every waiver has a named actor, reason, and follow-up issue when needed.

When any item is false, keep the PR in draft and send the task back through the
gauntlet.
