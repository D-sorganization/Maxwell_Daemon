# Beta release readiness

Use this checklist before tagging `v1.0.0-beta.1`. The beta should not be cut while any required artifact is missing or any release gate is red.

## Required gates

- GitHub Actions CI is green on `main`, including lint, typecheck, security scans, and the Python test matrix.
- Coverage is at or above the current ratchet floor and no release-critical module is excluded without a tracked issue.
- The MkDocs site builds with `mkdocs build --strict`.
- Security review is complete, including auth defaults, token handling, webhooks, audit logs, and desktop extension surfaces.
- The current changelog or generated GitHub release notes explain user-visible changes and known limitations.

## Required artifacts

- Python package distributions are built, checked with `twine check`, uploaded as a GitHub release artifact, and published to PyPI.
- Docker image is built, smoke-tested, and published with an immutable version tag.
- Helm chart is linted, packaged, and published with values for daemon, worker, ingress, secrets, and persistence.
- Desktop app installers are produced for macOS DMG, Windows MSI, and Linux AppImage/Snap.
- Desktop app signing/notarization status is documented for macOS and Windows.
- Documentation site is published from the release and includes getting started, configuration, deployment, API/OpenAPI, examples, and troubleshooting.

## Required release notes

Include:

- Installation and upgrade instructions.
- Supported Python versions and platform assumptions.
- Known alpha-to-beta limitations, especially around fleet orchestration, desktop packaging, and signed installers.
- Security posture and how to report vulnerabilities.
- Feedback channels for beta testers.

## Zero-to-production smoke path

Before the beta tag, a fresh machine should be able to complete this path in under 30 minutes:

1. Install Maxwell-Daemon.
2. Configure at least one backend.
3. Start the daemon.
4. Open the web UI.
5. Dispatch a GitHub issue in `plan` mode.
6. Inspect task status, logs, cost, and generated artifacts.
7. Deploy with the documented Docker or Helm path.

## Deferred from beta

These items can remain open for later production hardening if they are called out in release notes:

- Commercial support or SLA language.
- Enterprise SAML/SSO.
- Final pricing model.
- Broad marketing launch content.
