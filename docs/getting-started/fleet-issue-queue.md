# Fleet Issue Queue Walkthrough

Maxwell-Daemon's fleet issue queue turns a GitHub backlog into bounded daemon
tasks across one repo or many repos. The goal is unattended progress without
unbounded autonomy: operators choose the queue, Maxwell creates tasks, agents
produce plans or pull requests, and gates decide whether work is allowed to
advance.

This page documents the shipped operator path. It is not a branch merge queue,
and it does not auto-merge pull requests. Treat it as the intake lane before the
critic gauntlet, CI, and human review gates.

## Current Queue Surfaces

| Surface | Current contract |
| --- | --- |
| Single issue dispatch | `maxwell-daemon issue dispatch owner/repo 42 --mode plan` posts one issue task. |
| Batch file dispatch | `maxwell-daemon issue dispatch-batch --from-file issues.txt --dry-run` preserves the file order and supports per-line `:plan` or `:implement`. |
| Repo scan dispatch | `maxwell-daemon issue dispatch-batch --repo owner/repo --label small --max-stories 2 --dry-run` scans open issues and applies a label filter plus a per-repo cap. |
| Fleet manifest dispatch | `maxwell-daemon issue dispatch-batch --fleet-manifest fleet.yaml --all` expands every enabled repo from the shared fleet manifest. |
| REST intake | `POST /api/v1/issues/batch-dispatch` receives the final issue list from the CLI. |
| Periodic discovery contract | `DiscoveryScheduler` polls configured `DiscoveryRepoSpec` entries and records deduplication in `discovery_dedup.json`. |

The manual CLI path is the stable user interface. `DiscoveryScheduler` is the
internal always-on discovery contract used by daemon wiring and tests; only claim
background discovery where the deployment has explicitly enabled that scheduler.

## 1. Start With a Dry-Run Batch File

Use a text file when you already know the oldest issues you want handled. This is
the safest way to preserve a hand-curated oldest-to-newest queue.

```text
D-sorganization/Maxwell-Daemon#19:plan
D-sorganization/Maxwell-Daemon#470:plan
D-sorganization/Tools#2223:plan
```

Preview the exact payload before the daemon creates tasks:

```bash
maxwell-daemon issue dispatch-batch \
  --from-file issues.txt \
  --mode plan \
  --dry-run
```

Keep the first pass in `plan` mode unless the issue has narrow acceptance
criteria, a clean reproduction, and source-controlled checks.

## 2. Scan One Repo With Labels and Caps

Use repo scanning when the backlog is too large to curate by hand. Keep the
first unattended sweep small.

```bash
maxwell-daemon issue dispatch-batch \
  --repo D-sorganization/Maxwell-Daemon \
  --label documentation \
  --mode plan \
  --max-stories 2 \
  --limit 50 \
  --dry-run
```

`--label` limits the candidate set. `--max-stories` is the per-repo safety cap.
`--limit` controls how many open issues the GitHub lister may inspect for each
repo. If one repo fails during a multi-repo scan, the planner records an empty
summary for that repo so one broken repository does not strand the rest of the
fleet.

## 3. Expand a Fleet Manifest

Create a `fleet.yaml` when the same issue sweep should run across multiple
repositories with shared defaults and per-repo overrides.

```yaml
version: 1
fleet:
  name: home-lab
  discovery_interval_seconds: 900
  default_slots: 2
  default_budget_per_story: 0.75
  default_pr_target_branch: staging
  default_pr_fallback_to_default: true
  default_watch_labels: [maxwell:ready]

repos:
  - org: D-sorganization
    name: Maxwell-Daemon
    slots: 2
    watch_labels: [documentation, maxwell:ready]
    budget_per_story: 0.50
    enabled: true
  - org: D-sorganization
    name: Tools
    slots: 1
    watch_labels: [small, maxwell:ready]
    enabled: true
```

Preview every enabled repo:

```bash
maxwell-daemon issue dispatch-batch \
  --fleet-manifest fleet.yaml \
  --all \
  --label maxwell:ready \
  --mode plan \
  --max-stories 1 \
  --dry-run
```

The current `dispatch-batch` CLI uses the manifest to resolve enabled repos.
Pass `--label` on the command line to filter the GitHub issues for this run. The
manifest's `watch_labels` field is available to scheduler integrations, but the
batch CLI does not automatically turn those per-repo labels into separate
filters.

## 4. Submit the Batch

Remove `--dry-run` only after the summary shows the expected repositories,
eligible counts, submitted counts, and skipped counts.

```bash
maxwell-daemon issue dispatch-batch \
  --fleet-manifest fleet.yaml \
  --all \
  --label maxwell:ready \
  --mode plan \
  --max-stories 1 \
  --auth-token "$MAXWELL_API_TOKEN"
```

The CLI submits one payload to `POST /api/v1/issues/batch-dispatch`. The daemon
returns how many tasks were dispatched and how many failed. Treat any failed
item as a queue blocker until the error is understood.

## 5. Watch the Tasks

Check the queue before starting runners on other machines:

```bash
maxwell-daemon tasks list --kind issue --status queued
maxwell-daemon tasks show <task-id>
```

After a runner starts work, inspect gate and artifact surfaces:

```bash
curl -fsS -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  http://127.0.0.1:8080/api/v1/control-plane/gauntlet | jq

curl -fsS -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  http://127.0.0.1:8080/api/v1/tasks/task-123/artifacts | jq
```

Use the gauntlet state to decide whether the PR is ready for review, needs a
retry, or should be rejected. Passing queue intake is not the same as passing
implementation review.

## 6. Understand Always-On Discovery

`DiscoveryScheduler` is the daemon-side contract for recurring issue discovery.
Each `DiscoveryRepoSpec` names a repo, a required label set, and a dispatch
mode. A scheduler tick lists open issues, skips anything already recorded in the
dedup store, dispatches new matching issues, and persists the updated
`discovery_dedup.json` file only after at least one issue was submitted.

Operational boundaries:

- preserve the dedup file across restarts so the same issue is not dispatched
  repeatedly;
- start with labels such as `maxwell:ready`, `small`, or `documentation` instead
  of broad labels like `bug`;
- keep `mode` at `plan` for recurring discovery until the critic gauntlet and CI
  history show the repo is ready for implementation sweeps;
- use per-repo labels and low caps to avoid flooding a home machine or paid
  subscription account;
- stop the scheduler when GitHub auth, daemon auth, or repo access checks fail.

## Safety Rules

- Run `--dry-run` for every new queue shape.
- Prefer `plan` mode first; use `implement` only for narrow issues with tests.
- Combine `--label`, `--max-stories`, and `--limit` for unattended runs.
- Keep one durable queue owner so multiple automations do not submit the same
  work.
- Preserve `discovery_dedup.json` for scheduled discovery.
- Do not treat the issue queue as a merge queue.
- Do not auto-merge issue PRs from queue intake alone.
- Require source-controlled checks, critic review, and human or CI gates before
  work passes.
- Record dispatch evidence in the PR or task artifacts when an issue advances.

