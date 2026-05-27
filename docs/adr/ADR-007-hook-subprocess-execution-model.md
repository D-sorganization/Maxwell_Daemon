# ADR-007: Hook subprocess execution model

## Status
Accepted

## Context
`maxwell_daemon/hooks.py` previously used `asyncio.create_subprocess_shell` for every
hook invocation — including simple commands like `ruff check .` and `mypy maxwell_daemon`
that have no need for shell interpretation.  Using a shell for every execution carries
several risks:

1. **Injection surface** — an attacker who controls hook command text (e.g. via a
   maliciously crafted `maxwell-daemon.yaml`) can embed shell metacharacters to escape
   the intended command boundary.
2. **Portability** — `create_subprocess_shell` invokes `/bin/sh -c` on POSIX but
   `cmd.exe /c` on Windows, making behaviour platform-dependent for commands that
   don't need it.
3. **Security linters** — Bandit flags every `shell=True` call as a B602/B605 warning,
   creating noise that obscures real findings.

At the same time, some hooks legitimately need shell semantics: operators configure
pipeline commands (`cat log | grep ERROR`), redirections (`ruff . > report.txt`), and
compound commands (`make lint && make test`).  Removing shell support entirely would
break those use-cases.

## Decision
**Option 1: split runner.**

- `HookSpec` gains a `shell: bool = False` field.  Operators who need pipelines or
  shell built-ins set `shell: true` in YAML; all other specs default to the safe path.
- Two production runners are provided:
  - `_exec_default_runner` — uses `asyncio.create_subprocess_exec` with
    `shlex.split()`.  This is the default for all HookSpec hooks where
    `shell=False`.
  - `_shell_default_runner` — uses `asyncio.create_subprocess_shell`.  Used
    only when a spec carries `shell=True`.
- Lifecycle hooks (`pre_commit`, `on_prompt`, `on_stop`) are plain strings with no
  `shell:` field.  A `_needs_shell(cmd)` helper auto-detects shell metacharacters
  (`|`, `&`, `;`, `<`, `>`, `` ` ``, `$(`, `${`, `{`, `}`, `(`, `)`) and routes to
  `_shell_default_runner` when they are present, `_exec_default_runner` otherwise.
- `_default_runner` is kept unchanged for backward compatibility with existing tests
  that monkeypatch it.
- `HookRunner.__init__` gains `exec_runner=` and `shell_runner=` kwargs for injection
  in tests.  The existing `runner=` kwarg is mapped to `exec_runner` for backward
  compatibility.

## Consequences

**Positive**
- The security attack surface shrinks: the vast majority of hooks now run without a
  shell, limiting the impact of a crafted hook command.
- Bandit findings are reduced to the single, intentional `_shell_default_runner`
  function; a CI guard script (`check_subprocess_shell.py`) ensures no new
  `create_subprocess_shell` calls are introduced outside that function.
- Existing tests and callers require no changes — the `runner=` kwarg and
  `_default_runner` symbol continue to work exactly as before.
- Operators who need shell semantics have a clear, explicit opt-in path (`shell: true`).

**Negative**
- `_exec_default_runner` calls `shlex.split()` on the command string; this adds minor
  overhead and means commands with unmatched quotes will raise `ValueError` at runtime
  rather than silently misbehaving under the shell.
- Lifecycle hooks rely on auto-detection heuristics.  A command string that contains
  `$` in a file path (unusual but valid) will be incorrectly routed to the shell
  runner.  Operators needing deterministic routing should migrate lifecycle commands to
  HookSpec format and set `shell:` explicitly (a future improvement).
- One additional dependency on `shlex` (already imported) and a new regex constant.
