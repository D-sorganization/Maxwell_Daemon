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
- Backends and task execution: `/api/v1/backends`, `/api/v1/tasks`
- Issue dispatch: `/api/v1/issues`, `/api/v1/issues/dispatch`, `/api/v1/issues/batch-dispatch`
- Work items and artifacts: `/api/v1/work-items`, `/api/v1/artifacts`
- Fleet and workers: `/api/v1/fleet`, `/api/v1/workers`
- Audit, cost, and operations: `/api/v1/audit`, `/api/v1/cost`, `/api/reload`
- SSH support: `/api/v1/ssh/*`
- Event streams: `/api/v1/events`, `/api/v1/ssh/shell`

WebSocket routes are documented in the schema as FastAPI routes where supported, but client generators often need handwritten WebSocket adapters.
