# Fleet architecture

Maxwell-Daemon's fleet model separates queue intake, conductor-side orchestration,
worker execution, and gate decisions so one machine can coordinate many bounded
coding tasks without turning queue discovery into implicit merge authority.

This page describes the current contract-first architecture that is already
visible in the CLI, REST API, scheduler, worker heartbeats, capability
discovery, and gauntlet surfaces.

## High-level flow

```text
GitHub issues / batch files / fleet manifest
                |
                v
    issue dispatch CLI + batch planner
                |
                v
      REST intake / daemon enqueue
                |
                v
   discovery dedup + task/work-item store
                |
                v
    conductor routing + worker assignment
                |
                v
    local or remote execution surfaces
                |
                v
     artifacts + critic/gate evidence
                |
                v
     approve / reject / retry decisions
```

The queue owner decides what enters the system. Workers do not discover issues
for themselves, and a successful worker run does not bypass the gauntlet,
source-controlled checks, or human review.

## Core roles

| Role | Current responsibility | Current evidence |
| --- | --- | --- |
| Queue owner | Picks candidate issues, applies per-repo caps, and submits bounded work | `maxwell-daemon issue dispatch`, `issue dispatch-batch`, `docs/getting-started/fleet-issue-queue.md` |
| Conductor | Expands dispatch payloads into daemon tasks, records durable state, and exposes status over CLI/API | `/api/v1/issues/*`, `/api/v1/tasks/*`, `/api/v1/work-items/*`, task and work-item stores |
| Discovery scheduler | Performs recurring repo scans with deduplication and explicit mode/label filters | `DiscoveryScheduler`, `DiscoveryRepoSpec`, `discovery_dedup.json` |
| Workers | Advertise capabilities, receive bounded tasks, execute them, and report heartbeat/status | `/api/v1/workers`, `/api/v1/fleet`, `/api/v1/fleet/capabilities`, `/api/v1/heartbeat` |
| Gates and critics | Decide whether work can advance after execution evidence is available | `GauntletRuntime`, critic panel, `/api/v1/control-plane/gauntlet` |

## Queue intake and dedup boundaries

Fleet work starts at queue intake, not at a worker.

- `issue dispatch` handles one issue with an explicit mode such as `plan` or
  `implement`.
- `issue dispatch-batch` handles bounded repo scans, curated issue lists, or
  multi-repo fleet manifests.
- `DiscoveryScheduler` is the recurring discovery boundary. It lists open
  issues, filters by labels and repo spec, skips anything already recorded in
  `discovery_dedup.json`, and persists dedup state only after successful
  dispatch.

This split matters operationally: discovery is allowed to find work, but it is
not allowed to silently re-dispatch the same issue or to widen scope beyond the
configured repo, label, and cap limits.

## Conductor and worker separation

The conductor owns the durable control-plane state:

- task/work-item identifiers;
- submitted prompt or issue payload;
- routing metadata;
- artifacts and audit evidence;
- gauntlet and approval state.

Workers own execution only:

- polling or receiving assigned work;
- running the delegated backend/tool flow;
- publishing heartbeat and capability state;
- returning artifacts, findings, and status.

This keeps worker nodes replaceable. A worker can disappear and later rejoin
without becoming the system of record for the queue.

## Capability-aware scheduling

Fleet scheduling is intentionally descriptive rather than magical. Capability
surfaces let operators see what a worker can safely do before assigning costly
or privileged work.

Current fleet-facing surfaces include:

- `/api/v1/fleet` for overall fleet status;
- `/api/v1/fleet/nodes` for node-level visibility;
- `/api/v1/fleet/capabilities` for capability discovery;
- `/api/v1/workers` and `/api/v1/heartbeat` for worker presence and liveness.

Use these surfaces to keep specialized work on the right node, such as a worker
with browser support, a repo-local checkout, or credentials for a narrow tool
integration.

## Execution and review path

After a fleet task starts, the architecture stays fail-closed:

1. The worker runs the delegated plan or implementation task.
2. Artifacts, logs, and structured output are persisted.
3. The critic panel and gauntlet evaluate the evidence.
4. Operators approve, reject, waive, or retry based on the gate result.

This is why Maxwell documents a fleet issue queue and a gate runtime separately.
The queue is intake; the gauntlet is the promotion boundary.

## Safety boundaries

The current fleet architecture is intentionally conservative:

- no automatic merge from issue queue intake;
- no claim that recurring discovery replaces human queue ownership;
- no assumption that every worker has identical tools, repos, or auth;
- no hidden retry loops that erase failed evidence;
- no direct coupling between a worker's local cache and the conductor's durable
  audit trail.

If a future change needs broader autonomy, add it as a separate documented gate
or scheduler capability rather than smuggling it into worker execution.

## Operator checklist

Before enabling recurring fleet discovery:

1. Validate GitHub auth, daemon auth, and repo access.
2. Start with `plan` mode and low per-repo caps.
3. Preserve `discovery_dedup.json` across restarts.
4. Confirm worker capability and heartbeat surfaces are green.
5. Require gauntlet evidence before treating a worker result as review-ready.

Use this page together with the
[Fleet issue queue walkthrough](../getting-started/fleet-issue-queue.md) and
[Gate runtime and critic panel](gate-runtime.md) docs. The walkthrough explains
operator commands; this page explains why those commands are separated into
queue, worker, and gate responsibilities.
