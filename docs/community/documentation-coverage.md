# Documentation Coverage

Issue #19 tracks the comprehensive documentation site. The site is no longer a
blank epic: MkDocs is configured, GitHub Pages deployment exists, and several
core user paths are documented. Keep this page as the reviewable coverage map so
future agents can advance the remaining gaps without duplicating shipped work.

## Coverage Matrix

| Area | Current repo evidence | Status | Remaining gate |
| --- | --- | --- | --- |
| Getting started | `getting-started/quickstart.md`, `configuration.md`, `autonomous-workflow.md`, `examples.md`, `fleet-issue-queue.md`, `troubleshooting.md` | Shipped | Keep examples aligned with CLI/API changes. |
| Architecture guide | `architecture/overview.md`, `backends.md`, `contracts.md`, `gate-runtime.md`, `fleet-architecture.md` | Shipped | Keep the fleet architecture page aligned with worker capability, heartbeat, and gauntlet control-plane changes. |
| REST API reference | `reference/api.md`, `reference/openapi.md`, `tests/unit/test_docs_site_contract.py` | Shipped | Keep the OpenAPI route inventory test green whenever HTTP routes change. |
| gRPC reference | `reference/grpc.md`, `pyproject.toml` exposes the optional `grpc` extra, `tests/unit/test_docs_site_contract.py`, `tests/unit/test_grpc_status_docs.py` | Shipped | Keep the roadmap-only boundary explicit until versioned proto files, generated stubs, and a supported server contract exist. |
| Deployment guide | `operations/deployment.md`, `ansible.md`, `webhooks.md`, `tailscale.md`, `tests/unit/test_deployment_docs.py` | Shipped | Keep the launcher-based timed deploy proof current when bootstrap steps change. |
| Configuration reference | `getting-started/configuration.md`, `reference/configuration.md` | Shipped | Add a config drift test when new top-level config sections are introduced. |
| Development guide | `contributing.md`, `architecture/backends.md`, `architecture/contracts.md`, `development/backend-extension-guide.md`, `development/tool-authoring-guide.md`, `development/external-agent-adapters.md` | Shipped | Keep extension docs aligned with backend, external-agent, tool, and MCP transport changes. |
| Examples | `getting-started/examples.md`, `troubleshooting.md`, `fleet-gauntlet-walkthrough.md`, `resource-aware-routing.md`, `fleet-issue-queue.md` | Shipped | Fleet/shared-memory/critic-gauntlet walkthrough is shipped; resource-aware routing walkthrough is shipped; fleet issue queue walkthrough is shipped. Keep examples aligned with CLI/API changes. |
| Video tutorials | None in repo | Not started | Publish 10 short tutorials or replace this requirement with a written tutorial acceptance gate. |
| Docs publishing | `.github/workflows/docs.yml`, `mkdocs.yml` | Shipped | Keep `mkdocs build --strict` green on every docs PR. |

## Completion Rules

Do not close issue #19 until all of these gates are true:

- `mkdocs build --strict` passes from a clean checkout.
- Every feature named in the issue has a discoverable page in `mkdocs.yml`.
- The REST API reference is generated from, or checked against, the live OpenAPI
  schema.
- The gRPC reference either publishes supported proto definitions or explicitly
  documents that gRPC is roadmap-only.
- Deployment documentation includes a timed fresh-install proof for the target
  home-user path.
- Tailscale fleet documentation includes least-privilege policy guidance,
  application-auth requirements, and validation commands.
- The fleet/shared-memory/critic-gauntlet walkthrough is discoverable from
  `mkdocs.yml`, links memory, artifacts, control-plane gauntlet actions, and
  critic review into one operator flow.
- The architecture section includes a dedicated fleet architecture page that
  separates queue intake, conductor state, worker execution, capability
  discovery, and gate decisions.
- The resource-aware routing walkthrough is discoverable from `mkdocs.yml`,
  covers repo overrides, explicit task overrides, budget gates, fallback
  boundaries, and the `ResourceBroker` contract without claiming full automated
  subscription juggling is already integrated.
- The fleet issue queue walkthrough is discoverable from `mkdocs.yml`, covers
  dry-run batch dispatch, fleet manifest expansion, label filters, per-repo
  caps, task monitoring, scheduler dedup boundaries, and the current no
  auto-merge safety boundary.
- The development guide is discoverable from `mkdocs.yml`, covers backend
  extensions, external-agent adapters, deterministic tool authoring, current MCP
  status boundaries, and the local test harness expected for new extension
  surfaces.
- The video tutorial requirement is either satisfied or replaced by an explicit,
  accepted written-docs alternative.

Use `Refs #19` for partial documentation coverage PRs. Use `Closes #19` only
when the full matrix is green.
