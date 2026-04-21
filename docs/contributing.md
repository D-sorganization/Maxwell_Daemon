# Contributing

Maxwell-Daemon accepts bug fixes, documentation improvements, backend adapters,
deployment examples, and feature work that is linked to a public issue.

## Development Setup

```bash
git clone https://github.com/D-sorganization/Maxwell-Daemon.git
cd Maxwell-Daemon
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy maxwell_daemon
pytest
```

On Windows PowerShell, activate the environment with:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Contribution Flow

1. Pick an issue or open one for substantial changes.
2. Keep each pull request focused on one behavior or documentation goal.
3. Add tests for runtime behavior and docs for user-facing changes.
4. Run the local gate before requesting review.
5. Use the pull request template checklist to call out verification and risk.

Good first issues live at
<https://github.com/D-sorganization/Maxwell-Daemon/labels/good-first-issue>.

## Community Standards

All contributors must follow the
[Code of Conduct](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/CODE_OF_CONDUCT.md).
Security issues should be reported privately to the maintainer address listed in
the root contribution guide.

Roadmap and governance details live in
[Roadmap and Governance](community/roadmap-governance.md).
