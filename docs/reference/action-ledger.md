# Action Ledger

The action ledger is Maxwell-Daemon's reviewable record for agent side effects.
It turns file edits, diffs, shell commands, checks, pull request operations, and
external calls into explicit action records before they can affect a workspace.

The ledger is the safety boundary for autonomous work: an agent can propose what
it wants to do, Maxwell evaluates policy, and the action either waits for a
human/operator decision or moves through a recorded execution lifecycle.

## Lifecycle

Actions use a central transition model. API handlers, CLI commands, and internal
services should all call the same action service/store methods rather than
editing rows directly.

| Status | Meaning |
| --- | --- |
| `proposed` | The agent requested a side effect. No action has been applied yet. |
| `approved` | An operator or policy approved the proposal. |
| `rejected` | An operator rejected the proposal. The side effect must not run. |
| `running` | An approved or policy-allowed action is executing. |
| `applied` | The side effect completed successfully and recorded its result. |
| `failed` | Execution failed and recorded its error. |
| `reverted` | A previously applied action was reverted. |
| `skipped` | Policy denied execution or the daemon intentionally did not run it. |

Invalid transitions fail closed. For example, a rejected action cannot become
approved or applied, and an already approved action cannot be approved again by a
different actor.

## Approval modes

The default policy layer maps the configured approval tier to a decision for
each proposed action.

| Mode | Behavior |
| --- | --- |
| `suggest` | Every side effect is proposal-only until explicitly approved. |
| `auto-edit` | Scoped file writes, file edits, and diff application can run automatically; commands and other operations require approval. |
| `full-auto` | Policy-approved actions can run automatically, but denied commands and paths outside the allowed workspace remain blocked. |

Unknown action kinds, out-of-scope paths, and denied command names default to
blocked even in `full-auto`.

## API

The API exposes the action ledger for control-plane and UI workflows.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/tasks/{task_id}/actions` | List action records for a task. |
| `GET /api/v1/actions/{action_id}` | Fetch one action record. |
| `POST /api/v1/actions/{action_id}/approve` | Approve a proposed action. |
| `POST /api/v1/actions/{action_id}/reject` | Reject a proposed action with an optional reason. |

When JWT RBAC is enabled, action reads require viewer-or-higher access and
approval/rejection requires operator-or-higher access. A static bearer token is
treated as administrator-level access.

## CLI

The CLI mirrors the API for local operations:

```bash
maxwell-daemon tasks actions TASK_ID
maxwell-daemon action show ACTION_ID
maxwell-daemon action approve ACTION_ID
maxwell-daemon action reject ACTION_ID --reason "unsafe command"
```

Use the CLI when reviewing automation from a terminal or when scripting a
human-in-the-loop workflow.

## Audit behavior

Approval and rejection decisions are written through the audit logger when audit
logging is configured. The audit entry records:

- operation name, such as `action_approved` or `action_rejected`;
- task id;
- action id;
- actor;
- action kind;
- rejection reason, when present.

The audit log is append-only and hash-chained elsewhere in the daemon. Approval
metadata on the action itself is also immutable through the transition rules.

## Proposal-only boundary

An action with `requires_approval=true` is a proposal. The side effect must not
execute before approval. Current built-in file tools can create proposal records
and return an approval-required result in `suggest` mode.

Approved proposal execution should remain explicit: either a follow-up runner
executes the approved action through the action service, or the action stays
proposal-only and the UI/CLI describes it as such. This distinction matters for
user trust because approval should never imply that an unimplemented executor
silently changed files.

## Example

An agent wants to edit `src/app.py` while the daemon runs in `suggest` mode.

1. The tool proposes an action:

   ```json
   {
     "kind": "file_edit",
     "summary": "Update src/app.py",
     "payload": {"path": "src/app.py"},
     "requires_approval": true
   }
   ```

2. The daemon stores the action as `proposed` and returns an approval-required
   result to the agent.
3. An operator inspects the action through the UI, API, or CLI.
4. If the operator rejects it, the action becomes `rejected` and cannot run.
5. If the operator approves it, the action becomes `approved`; an executor must
   then move it to `running` and finally `applied`, `failed`, or `skipped`.

This model keeps the user-facing contract clear: proposals, approvals, and
executions are separate, auditable steps.
