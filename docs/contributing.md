# Contributing

See [CONTRIBUTING.md](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/CONTRIBUTING.md) in the repo root.

TL;DR for getting set up:

```bash
git clone https://github.com/D-sorganization/Maxwell-Daemon.git
cd Maxwell-Daemon
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
mypy maxwell_daemon
```

Good first issues live at <https://github.com/D-sorganization/Maxwell-Daemon/labels/good-first-issue>.
