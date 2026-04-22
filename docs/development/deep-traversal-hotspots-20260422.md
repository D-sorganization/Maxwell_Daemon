# Deep Traversal Hotspot Review

Issue #316 was generated from an A-N assessment that referenced the old
`conductor/` package name. The current package is `maxwell_daemon/`; the matching
areas were reviewed against current `main` on 2026-04-22.

## Reviewed Hotspots

| Reported area | Current area | Disposition |
| --- | --- | --- |
| `conductor/backends/claude.py` | `maxwell_daemon/backends/claude.py` | External SDK boundary. `self._client.messages.create`, `self._client.messages.stream`, and `resp.usage.*` are direct Anthropic SDK surfaces. Wrapping them further would not reduce internal coupling. Usage fields are already copied into local variables before building `TokenUsage`. |
| `conductor/backends/openai.py` | `maxwell_daemon/backends/openai.py` | External SDK boundary. `self._client.chat.completions.create`, `chunk.choices[0].delta`, and `self._client.models.list` are OpenAI SDK surfaces. The adapter is the intended anti-corruption boundary. |
| `conductor/core/router.py` | `maxwell_daemon/core/router.py` | Already simplified. Config graph access is centralized through `_default_backend_name`, `_backend_config`, and `_all_backend_configs`; callers do not traverse `config.agent` or `config.backends` directly. |
| `conductor/gh/workspace.py` | `maxwell_daemon/gh/workspace.py` | Intentional path and process API usage. `target.parent.mkdir`, `asyncio.subprocess.PIPE`, and resolved parent checks are standard library boundary calls with local validation around them. |
| `conductor/config/models.py` | `maxwell_daemon/config/models.py` | Intentional Pydantic settings model. The only reviewed secret chain is `self.github.webhook_secret.get_secret_value()`, which is the local accessor that prevents callers from reaching into the secret field directly. |
| `conductor/cli/issues.py` and `conductor/cli/main.py` | `maxwell_daemon/cli/issues.py` and `maxwell_daemon/cli/main.py` | No API boundary change needed for the reported default URLs and Typer option declarations. Future CLI cleanup should prefer helper functions when multiple commands share response parsing. |
| `conductor/core/ledger.py` | `maxwell_daemon/core/ledger.py` | Current hits are filesystem setup calls such as `self._path.parent.mkdir(...)`, which are acceptable standard library boundary usage. |

## Follow-Up Guidance

- Do not add wrappers around third-party SDK clients solely to satisfy member-chain
  regexes; the backend adapters already provide that boundary for callers.
- Prefer narrow follow-up issues for real internal graph traversal, especially
  daemon or API code that repeatedly reads `daemon._config.*` from request
  handlers.
- When changing public API boundaries, add focused tests around the caller-facing
  behavior rather than asserting implementation details.
