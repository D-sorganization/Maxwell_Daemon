# Roadmap and Governance

Maxwell-Daemon is maintained in public through GitHub issues, pull requests, and
release milestones. The roadmap is intentionally issue-driven so implementation
work, design discussion, and release readiness stay linked.

## Decision Model

Maintainers make final calls on scope, security posture, release timing, and
compatibility. Contributors influence those decisions through:

- Feature requests with clear use cases and proposed interfaces.
- Pull requests with tests, documentation, and migration notes.
- Design comments on roadmap issues before implementation starts.
- User reports that describe real operational constraints.

For small changes, a passing pull request and maintainer approval are enough.
For changes that affect public APIs, backend contracts, security, configuration,
or deployment topology, open or update a tracking issue first.

## Roadmap Process

Roadmap issues are organized by phase:

| Phase | Focus |
| --- | --- |
| 1 | Foundation and architecture |
| 2 | Multi-backend LLM support |
| 3 | VS Code-like GUI |
| 4 | Remote access and fleet management |
| 5 | Deployment automation |
| 6 | Extensions and desktop packaging |
| 7 | Observability and cost analytics |
| 8 | Security, RBAC, and audit logging |
| 9 | Documentation and community |
| 10 | Beta and production releases |

Each phase issue should define acceptance criteria, expected deliverables, and
verification. Large phase issues can be split into smaller implementation
issues when the scope becomes too broad for a single pull request.

## Feature Voting

Community members can signal demand by:

- Reacting with thumbs-up on feature requests.
- Commenting with concrete workflows, constraints, or failure modes.
- Linking production examples or integrations that would benefit from the work.

Maintainers prioritize work by user impact, implementation risk, maintenance
cost, security implications, and alignment with the current release phase.

## Community Channels

The canonical project channel is GitHub:

- Bugs and feature requests: GitHub Issues.
- Design discussion: the relevant tracking issue.
- Code review: GitHub Pull Requests.
- Security reports: private email to the maintainer address in the security
  section of `CONTRIBUTING.md`.

If GitHub Discussions, Discord, or Slack are added later, this page should be
updated with links and moderation expectations before the channel is announced.

## Maintainer Responsibilities

Maintainers are expected to:

- Keep roadmap issues accurate enough for contributors to choose work.
- Label approachable issues for new contributors.
- Explain rejection or deferral decisions with actionable context.
- Enforce the Code of Conduct consistently.
- Avoid merging user-facing changes without matching documentation.
