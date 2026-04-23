# Documentation Coverage

Issue #19 tracks the comprehensive documentation site. The site is no longer a
blank epic: MkDocs is configured, GitHub Pages deployment exists, and several
core user paths are documented. Keep this page as the reviewable coverage map so
future agents can advance the remaining gaps without duplicating shipped work.

## Coverage Matrix

| Area | Current repo evidence | Status | Remaining gate |
| --- | --- | --- | --- |
| Getting started | `getting-started/quickstart.md`, `configuration.md`, `autonomous-workflow.md`, `examples.md`, `troubleshooting.md` | Shipped | Keep examples aligned with CLI/API changes. |
| Architecture guide | `architecture/overview.md`, `backends.md`, `contracts.md`, `gate-runtime.md` | Partial | Add a dedicated fleet architecture page for conductor/worker dispatch. The gate runtime and critic panel foundation is documented, and the fleet gauntlet walkthrough links it to operator workflows. |
| REST API reference | `reference/api.md`, `reference/openapi.md`, `tests/unit/test_docs_site_contract.py` | Shipped | Keep the OpenAPI route inventory test green whenever HTTP routes change. |
| gRPC reference | `reference/grpc.md`, `pyproject.toml` exposes the optional `grpc` extra | Partial | Add protocol definitions and generated-client guidance before claiming supported gRPC. |
| Deployment guide | `operations/deployment.md`, `ansible.md`, `webhooks.md`, `tailscale.md` | Partial | Tailscale-specific security guidance is shipped; still prove a fresh deploy path in under 30 minutes. |
| Configuration reference | `getting-started/configuration.md`, `reference/configuration.md` | Shipped | Add a config drift test when new top-level config sections are introduced. |
| Development guide | `contributing.md`, `architecture/backends.md`, `architecture/contracts.md` | Partial | Add extension/tool authoring docs, MCP status boundaries, and local test harness guidance. |
| Examples | `getting-started/examples.md`, `troubleshooting.md`, `fleet-gauntlet-walkthrough.md`, `resource-aware-routing.md` | Partial | Fleet/shared-memory/critic-gauntlet walkthrough is shipped; resource-aware routing walkthrough is shipped; add fleet issue queue walkthroughs. |
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
- The resource-aware routing walkthrough is discoverable from `mkdocs.yml`,
  covers repo overrides, explicit task overrides, budget gates, fallback
  boundaries, and the `ResourceBroker` contract without claiming full automated
  subscription juggling is already integrated.
- The video tutorial requirement is either satisfied or replaced by an explicit,
  accepted written-docs alternative.

Use `Refs #19` for partial documentation coverage PRs. Use `Closes #19` only
when the full matrix is green.
