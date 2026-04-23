# Beta feedback operations

Use this page to keep the `v1.0.0-beta.1` feedback loop explicit and fast.

## Goals

- Give early adopters one obvious path to report bugs and usability issues.
- Separate release blockers from non-blocking polish feedback.
- Keep known issues visible so duplicate reports do not overwhelm triage.

## Recommended intake channels

- GitHub Issues for reproducible bugs, regressions, install failures, and deployment defects.
- GitHub Discussions or a pinned release thread for usability feedback, workflow questions, and roadmap comments.
- A short release-notes section pointing testers to both paths.

## Required labels

- `beta`: reported during the beta window.
- `feedback`: non-bug product feedback or workflow friction.
- `release-blocker`: issue must be fixed before broadening rollout.
- `known-issue`: acknowledged limitation already reflected in release notes.

## Triage policy

Review new beta reports at least once per business day during the first launch week.

For each report:

1. Reproduce or request the missing evidence.
2. Classify it as `release-blocker`, `known-issue`, or normal follow-up.
3. Link duplicates to a single owner ticket.
4. Reflect user-visible blockers back into the release notes or known-issues list.

## Report template

Ask testers to include:

- Maxwell-Daemon version or commit SHA.
- Platform and Python version.
- Backend or deployment mode in use.
- Exact command, workflow, or UI action that failed.
- Logs, screenshots, or traceback snippets with secrets removed.
- Whether the issue blocks first-run setup, routine use, or only an advanced path.

## Launch-week dashboard

Track these numbers in the beta release issue or milestone:

- Open `release-blocker` count.
- Open `known-issue` count.
- Median first-response time for beta reports.
- Number of testers who completed the zero-to-production path.
