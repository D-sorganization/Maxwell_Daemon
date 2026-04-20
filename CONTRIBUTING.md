# Contributing to Maxwell-Daemon

Thanks for your interest — Maxwell-Daemon is built in the open and welcomes contributions of every size, from typo fixes to whole new LLM backends.

## Getting set up

```bash
git clone https://github.com/D-sorganization/Maxwell-Daemon.git
cd Maxwell-Daemon
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

## What we're looking for

The [open issues](https://github.com/D-sorganization/Maxwell-Daemon/issues) are the authoritative list. Good first issues are labelled `good-first-issue`. If you want to tackle something bigger (a new backend, the GUI, the gRPC API), comment on the tracking issue first so we can align on design.

## How to add a new LLM backend

1. Create `maxwell_daemon/backends/<name>.py` implementing `ILLMBackend`.
2. Register it at the bottom: `registry.register("<name>", YourBackend)`.
3. Import it in `maxwell_daemon/backends/registry.py::_autoload()`.
4. Add tests in `tests/unit/test_backends.py` (don't hit the real API — mock it).
5. Document pricing and context windows so cost estimation stays accurate.

The `ClaudeBackend` and `OllamaBackend` adapters are good reference implementations — one remote, one local.

## Pull request checklist

- [ ] `pytest` passes locally
- [ ] `ruff check .` and `ruff format --check .` both pass
- [ ] New code has tests
- [ ] Public APIs have type hints
- [ ] Commits are squashed or logically grouped (one concept per commit)
- [ ] PR description explains the *why*, not just the *what*

## Code style

- Python 3.10+, type-hinted, async where it touches I/O.
- Line length 100. Ruff handles formatting.
- No comments explaining *what* the code does — only *why* when it's non-obvious.
- Fail fast on misconfiguration; never silently fall back to insecure defaults.

## Reporting bugs

Use the bug-report issue template. Include: Maxwell-Daemon version, Python version, OS, config (redact secrets), and a minimal reproduction.

## Security

Please do **not** open public issues for security vulnerabilities. Email dieterolson@gmail.com or use GitHub's private reporting instead.

## License

By contributing you agree your work is released under the MIT License.
