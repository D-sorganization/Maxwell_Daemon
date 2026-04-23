# Documentation Coverage

Issue #19 tracks the comprehensive documentation site. The site is no longer a
blank epic: MkDocs is configured, GitHub Pages deployment exists, and several
core user paths are documented. Keep this page as the reviewable coverage map so
future agents can advance the remaining gaps without duplicating shipped work.

## Coverage Matrix

| Area | Current repo evidence | Status | Remaining gate |
| --- | --- | --- | --- |
| Getting started | `getting-started/quickstart.md`, `configuration.md`, `autonomous-workflow.md`, `examples.md`, `troubleshooting.md` | Shipped | Keep examples aligned with CLI/API changes. |
| Architecture guide | `architecture/overview.md`, `backends.md`, `contracts.md` | Partial | Add a dedicated fleet architecture page for conductor/worker dispatch, shared memory, critic panels, and gauntlet gates. |
| REST API reference | `reference/api.md` | Partial | Export and publish the generated OpenAPI schema, then document every `/api/v1/*` route from the schema. |
| gRPC reference | `pyproject.toml` exposes the optional `grpc` extra | Not started | Add protocol definitions or explicitly document that gRPC is roadmap-only. |
| Deployment guide | `operations/deployment.md`, `ansible.md`, `webhooks.md` | Partial | Prove a fresh deploy path in under 30 minutes and add Tailscale-specific security guidance. |
| Configuration reference | `getting-started/configuration.md`, `reference/configuration.md` | Shipped | Add a config drift test when new top-level config sections are introduced. |
| Development guide | `contributing.md`, `architecture/backends.md`, `architecture/contracts.md` | Partial | Add extension/tool authoring docs, MCP status boundaries, and local test harness guidance. |
| Examples | `getting-started/examples.md`, `troubleshooting.md` | Partial | Add resource-aware routing, critic gauntlet, fleet issue queue, and shared memory walkthroughs. |
| Video tutorials | None in repo | Not started | Publish 10 short tutorials or replace this requirement with a written tutorial acceptance gate. |
| Docs publishing | `.github/workflows/docs.yml`, `mkdocs.yml` | Shipped | Keep `mkdocs build --strict` green on every docs PR. |

## Completion Rules

Do not close issue #19 until all of these gates are true:

- `mkdocs build --strict` passes from a clean checkout.
- Every feature named in the issue has a discoverable page in `mkdocs.yml`.
- The REST API reference is generated from, or checked against, the live OpenAPI
  schema.
- Deployment documentation includes a timed fresh-install proof for the target
  home-user path.
- The video tutorial requirement is either satisfied or replaced by an explicit,
  accepted written-docs alternative.

Use `Refs #19` for partial documentation coverage PRs. Use `Closes #19` only
when the full matrix is green.
