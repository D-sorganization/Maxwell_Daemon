# OpenAPI reference

Maxwell-Daemon exposes the FastAPI OpenAPI document at:

```text
GET /openapi.json
```

Use it as the source of truth for generated clients, typed SDKs, and contract checks. The interactive Swagger UI is available at:

```text
GET /docs
```

The ReDoc view is available at:

```text
GET /redoc
```

## Export the schema

Start the daemon locally:

```bash
maxwell-daemon serve --host 127.0.0.1 --port 8080
```

Then export the schema:

```bash
curl http://127.0.0.1:8080/openapi.json > openapi.json
```

If `api.auth_token` is configured, `/openapi.json`, `/docs`, and `/redoc` are still served by FastAPI as schema/documentation routes. Protected API calls listed inside the schema require `Authorization: Bearer <token>`.

## Client generation

Example with `openapi-generator-cli`:

```bash
openapi-generator-cli generate \
  -i openapi.json \
  -g python \
  -o clients/python
```

Example with `npx`:

```bash
npx @openapitools/openapi-generator-cli generate \
  -i http://127.0.0.1:8080/openapi.json \
  -g typescript-fetch \
  -o clients/typescript
```

## Core endpoint groups

The schema includes these route families:

- Health and readiness: `/health`, `/readyz`
- Auth and RBAC identity: `/api/v1/auth/token`, `/api/v1/auth/me`
- Backends and task execution: `/api/v1/backends`, `/api/v1/tasks`
- Gate control plane: `/api/v1/control-plane/gauntlet`
- Actions, work items, task graphs, and artifacts
- Issue dispatch and batch dispatch
- Fleet, delegate sessions, workers, and capability discovery
- Memory assembly and recording
- Audit, cost, config reload, and retention pruning
- SSH support routes when the optional `ssh` extra is installed
- GitHub webhook ingestion

WebSocket routes such as `/api/v1/events` and `/api/v1/ssh/shell` are runtime routes, but client generators often need handwritten WebSocket adapters because they are not represented in the OpenAPI HTTP schema.

## Live route inventory

This inventory is checked against `create_app(...).openapi()` by `tests/unit/test_docs_site_contract.py`. Update this table in the same PR that adds, removes, or renames an HTTP route.

| Path | Methods | Area |
| --- | --- | --- |
| `/api/reload` | `POST` | Operations |
| `/api/v1/actions` | `GET` | Actions |
| `/api/v1/actions/{action_id}` | `GET` | Actions |
| `/api/v1/actions/{action_id}/approve` | `POST` | Actions |
| `/api/v1/actions/{action_id}/reject` | `POST` | Actions |
| `/api/v1/admin/prune` | `GET` | Operations |
| `/api/v1/artifacts/{artifact_id}` | `GET` | Artifacts |
| `/api/v1/artifacts/{artifact_id}/content` | `GET` | Artifacts |
| `/api/v1/audit` | `GET` | Audit |
| `/api/v1/audit/verify` | `GET` | Audit |
| `/api/v1/auth/me` | `GET` | Auth |
| `/api/v1/auth/token` | `POST` | Auth |
| `/api/v1/backends` | `GET` | Backends |
| `/api/v1/control-plane/gauntlet` | `GET` | Gate runtime |
| `/api/v1/control-plane/gauntlet/{task_id}/cancel` | `POST` | Gate runtime |
| `/api/v1/control-plane/gauntlet/{task_id}/retry` | `POST` | Gate runtime |
| `/api/v1/control-plane/gauntlet/{task_id}/waive` | `POST` | Gate runtime |
| `/api/v1/cost` | `GET` | Cost |
| `/api/v1/delegate-sessions` | `GET` | Delegates |
| `/api/v1/delegate-sessions/{session_id}` | `GET` | Delegates |
| `/api/v1/fleet` | `GET` | Fleet |
| `/api/v1/fleet/capabilities` | `GET` | Fleet |
| `/api/v1/fleet/nodes` | `GET` | Fleet |
| `/api/v1/heartbeat` | `POST` | Fleet |
| `/api/v1/issues` | `POST` | Issues |
| `/api/v1/issues/ab-dispatch` | `POST` | Issues |
| `/api/v1/issues/batch-dispatch` | `POST` | Issues |
| `/api/v1/issues/dispatch` | `POST` | Issues |
| `/api/v1/issues/{owner}/{name}` | `GET` | Issues |
| `/api/v1/memory/assemble` | `POST` | Memory |
| `/api/v1/memory/record` | `POST` | Memory |
| `/api/v1/ssh/connect` | `POST` | SSH |
| `/api/v1/ssh/files` | `GET` | SSH |
| `/api/v1/ssh/keys` | `GET` | SSH |
| `/api/v1/ssh/keys/{machine}` | `GET, DELETE` | SSH |
| `/api/v1/ssh/run` | `POST` | SSH |
| `/api/v1/ssh/sessions` | `GET` | SSH |
| `/api/v1/task-graphs` | `POST, GET` | Task graphs |
| `/api/v1/task-graphs/{graph_id}` | `GET` | Task graphs |
| `/api/v1/task-graphs/{graph_id}/start` | `POST` | Task graphs |
| `/api/v1/tasks` | `POST, GET` | Tasks |
| `/api/v1/tasks/{task_id}` | `GET` | Tasks |
| `/api/v1/tasks/{task_id}/actions` | `GET` | Tasks |
| `/api/v1/tasks/{task_id}/artifacts` | `GET` | Tasks |
| `/api/v1/tasks/{task_id}/cancel` | `POST` | Tasks |
| `/api/v1/webhooks/github` | `POST` | Webhooks |
| `/api/v1/work-items` | `POST, GET` | Work items |
| `/api/v1/work-items/{item_id}` | `GET, PATCH` | Work items |
| `/api/v1/work-items/{item_id}/artifacts` | `GET` | Work items |
| `/api/v1/work-items/{item_id}/transition` | `POST` | Work items |
| `/api/v1/workers` | `GET, PUT` | Workers |
| `/health` | `GET` | Health |
| `/readyz` | `GET` | Health |
