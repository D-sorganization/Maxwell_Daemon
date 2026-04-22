# Production release readiness

Use this checklist before tagging `v1.0.0`. Production release is a commitment that Maxwell-Daemon can be operated, supported, audited, and upgraded by users outside the core development loop.

## Required gates

- Beta feedback has been triaged and no critical beta bug remains open.
- CI is green on `main`, including lint, typecheck, security scans, distribution build, and all supported Python versions.
- Release candidate has completed a multi-day soak test with daemon, web UI, fleet workers, memory, and GitHub issue dispatch enabled.
- Uptime target and support response expectations are documented.
- Security review covers authentication, authorization, audit logs, secret handling, desktop app surfaces, VS Code extension surfaces, webhooks, and fleet communication.
- Upgrade and rollback paths are documented for Python package, Docker, Helm, and desktop installers.

## Enterprise readiness

Production release requires an explicit posture for:

- SAML or external identity provider integration, or a documented reason it is deferred.
- Role-based access control and audit log export.
- Secret rotation and incident response.
- Data retention and deletion controls.
- Fleet-node trust boundaries, including Tailscale or private-network assumptions.
- Commercial support and escalation channels, even if the project remains community-first.

## Required artifacts

- Versioned Python package on PyPI.
- Versioned Docker image with provenance metadata.
- Versioned Helm chart with upgrade notes.
- Signed macOS and Windows desktop installers.
- Linux desktop installer artifacts.
- Published documentation site.
- GitHub release notes with migration notes, known limitations, and security posture.

## Launch readiness

Before announcing `v1.0.0`, confirm:

- Pricing and licensing language is final or explicitly states the project remains OSS-only.
- Support channels and issue templates are ready.
- Production deployment guide has been exercised by a fresh operator.
- Monitoring dashboards and alerts have documented defaults.
- Public roadmap distinguishes production guarantees from future features.

## Deferred from production

Any deferred item must have:

1. A linked GitHub issue.
2. A clear risk statement.
3. A mitigation or fallback.
4. A target milestone.
