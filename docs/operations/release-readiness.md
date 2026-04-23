# Beta release readiness

Use this checklist before tagging `v1.0.0-beta.1`. The beta should not be cut while any required artifact is missing or any release gate is red.

## Exit criteria

The beta release is ready only when all of the following are true:

- Core product work through PHASE 7 is merged or explicitly deferred with a linked issue and release-note callout.
- The current release candidate passes CI on `main` with no blocking flaky jobs, skipped required jobs, or unresolved red checks.
- Coverage remains at or above the ratchet floor and no temporary suppression was added without a tracked follow-up.
- User-facing docs cover install, configuration, backends, issue dispatch, fleet orchestration, troubleshooting, and deployment.
- A named feedback channel is open and staffed for the beta window.
- The zero-to-production rehearsal has been run on a fresh machine and its evidence is attached to the release issue or milestone.

## Required gates

- GitHub Actions CI is green on `main`, including lint, typecheck, security scans, and the Python test matrix.
- Coverage is at or above the current ratchet floor and no release-critical module is excluded without a tracked issue.
- The MkDocs site builds with `mkdocs build --strict`.
- Security review is complete, including auth defaults, token handling, webhooks, audit logs, and desktop extension surfaces.
- The current changelog or generated GitHub release notes explain user-visible changes and known limitations.

## Evidence to capture

Record release evidence in a single tracking issue, milestone note, or release project item. At minimum capture:

- Commit SHA or tag candidate under evaluation.
- Link to the passing `main` CI run.
- Coverage floor value and the command/output or report proving it.
- Link to the docs build artifact or `mkdocs build --strict` log.
- Link to the security review or threat-model note used for signoff.
- Screenshot or transcript of the zero-to-production smoke path.
- Release-notes draft and beta feedback channel location.

## Required artifacts

- Python package distributions are built, checked with `twine check`, uploaded as a GitHub release artifact, and published to PyPI.
- Docker image is built, smoke-tested, and published with an immutable version tag.
- Helm chart is linted, packaged, and published with values for daemon, worker, ingress, secrets, and persistence.
- Desktop app installers are produced for macOS DMG, Windows MSI, and Linux AppImage/Snap.
- Desktop app signing/notarization status is documented for macOS and Windows.
- Documentation site is published from the release and includes getting started, configuration, deployment, API/OpenAPI, examples, and troubleshooting.

## Signoff matrix

Use this table to make remaining gaps obvious before the tag:

| Area | What must be green | Evidence |
| --- | --- | --- |
| CI and tests | Required checks green on `main`; no untriaged flaky blockers | GitHub Actions run URL |
| Coverage | Ratchet floor met or exceeded | Coverage report or gate log |
| Packaging | Python package, Docker image, Helm chart, and desktop artifacts built | Release issue checklist |
| Security | Auth, secrets, audit logs, and webhook handling reviewed | Security review link |
| Docs | MkDocs strict build passes and beta guidance is published | Docs build log |
| Feedback | Beta intake path announced and monitored | [Beta feedback operations](../community/beta-feedback.md) |

## Required release notes

Include:

- Installation and upgrade instructions.
- Supported Python versions and platform assumptions.
- Known alpha-to-beta limitations, especially around fleet orchestration, desktop packaging, and signed installers.
- Security posture and how to report vulnerabilities.
- Feedback channels for beta testers.

## Beta communication checklist

Before publishing the release, prepare:

- GitHub release notes with installation, upgrade, rollback, and known-limitations sections.
- A short beta announcement for GitHub Discussions or the repository README/news surface.
- A bug-report intake link plus a discussion or feedback thread for non-bug observations.
- A list of known not-in-beta items so early adopters do not infer production guarantees.

## Zero-to-production smoke path

Before the beta tag, a fresh machine should be able to complete this path in under 30 minutes:

1. Install Maxwell-Daemon.
2. Configure at least one backend.
3. Start the daemon.
4. Open the web UI.
5. Dispatch a GitHub issue in `plan` mode.
6. Inspect task status, logs, cost, and generated artifacts.
7. Deploy with the documented Docker or Helm path.

## Rehearsal commands

Use a release-candidate branch or local checkout and preserve the command/output as evidence:

```bash
python -m pytest
python -m mypy maxwell_daemon
python -m ruff check .
python -m mkdocs build --strict
python -m build
twine check dist/*
```

Run the container and chart verification commands that match the current deployment docs before tagging the beta.

## Feedback-channel readiness

The beta should not ship until all of these are assigned:

- Bug intake owner for launch week.
- Triage cadence for incoming defects and usability findings.
- Label taxonomy for `beta`, `feedback`, `release-blocker`, and `known-issue`.
- Public place where testers can see known issues and reporting instructions.

## Deferred from beta

These items can remain open for later production hardening if they are called out in release notes:

- Commercial support or SLA language.
- Enterprise SAML/SSO.
- Final pricing model.
- Broad marketing launch content.
