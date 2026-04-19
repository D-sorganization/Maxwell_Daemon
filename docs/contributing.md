# Contributing

See [CONTRIBUTING.md](https://github.com/D-sorganization/CONDUCTOR/blob/main/CONTRIBUTING.md) in the repo root.

TL;DR for getting set up:

```bash
git clone https://github.com/D-sorganization/CONDUCTOR.git
cd CONDUCTOR
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
mypy conductor
```

Good first issues live at <https://github.com/D-sorganization/CONDUCTOR/labels/good-first-issue>.
