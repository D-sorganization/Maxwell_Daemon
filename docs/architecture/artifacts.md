# Durable artifacts

Maxwell stores task and work-item evidence as durable artifacts. Artifacts capture
inspectable outputs such as plans, diffs, command logs, test results, check
results, screenshots, transcripts, handoffs, PR bodies, and structured metadata.

## Storage layout

Artifact metadata lives in SQLite. Blob content lives under the configured
artifact root:

```text
artifacts/
  tasks/{task_id}/{artifact_id}.{ext}
  work-items/{work_item_id}/{artifact_id}.{ext}
```

The store writes blob bytes first with an atomic replace, then inserts metadata.
Readers never need to construct paths directly. They ask `ArtifactStore` for
metadata or content, and the store resolves paths under the artifact root.

## Integrity

Each artifact stores `sha256` and `size_bytes`. Reads recompute both values and
raise an integrity error if bytes were changed, truncated, or replaced. Metadata
paths must be relative and must resolve under the artifact root; tampered paths
that escape the root are rejected.

## Ownership

An artifact belongs to exactly one owner:

- `task_id` for daemon task evidence.
- `work_item_id` for governed work-item evidence.

This keeps list endpoints deterministic and avoids ambiguous lifecycle rules.
Future handoff features can link related artifacts by metadata rather than
giving one artifact multiple owners.

## API

Read endpoints are viewer-scoped:

- `GET /api/v1/tasks/{task_id}/artifacts`
- `GET /api/v1/work-items/{work_item_id}/artifacts`
- `GET /api/v1/artifacts/{artifact_id}`
- `GET /api/v1/artifacts/{artifact_id}/content`

There is no general public upload endpoint. Operators create artifacts through
task execution paths so side effects remain tied to daemon actions and audit
events.

## Issue execution

The GitHub issue executor writes artifacts for the initial plan, initial diff,
applied diff, test result, and generated PR body when an artifact store is
attached. These records make issue-to-PR runs resumable and reviewable without
reconstructing transient logs.
