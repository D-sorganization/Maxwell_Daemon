"""Deterministic hook system — code-standards gates the LLM cannot bypass.

Hooks fire at well-defined moments in the agent lifecycle:

  * ``pre_tool``   — before a tool invocation; non-zero exit aborts the call
  * ``post_tool``  — after a tool invocation; non-zero exit surfaces as a
                     :class:`~maxwell_daemon.tools.ToolResult` with ``is_error=True``
  * ``pre_commit`` — gate PR-open on linters / types / tests
  * ``on_prompt``  — inject extra context before the first turn
  * ``on_stop``    — housekeeping when a session ends

Design notes (per Maxwell-Daemon principles):

  * **DbC:** constructor enforces workspace-is-a-directory; hook invocations
    that can't be recovered from raise :class:`HookViolationError` so callers
    pattern-match on a named failure kind.
  * **LOD:** the subprocess runner is injected (:class:`RunnerFn` protocol);
    in tests the runner is a recorder, in prod it wraps ``asyncio.subprocess``.
    The runner knows *nothing* about hooks; the hook runner knows *nothing*
    about shells — each layer talks only to its neighbour.
  * **Reversibility:** ``HookConfig`` defaults to empty tuples so a repo with
    no ``maxwell-daemon.yaml`` gets zero hook machinery running.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import shlex
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from maxwell_daemon.contracts import require
from maxwell_daemon.tools.builtins import _build_run_bash_env

__all__ = [
    "HookConfig",
    "HookOutcome",
    "HookRunner",
    "HookSpec",
    "HookViolationError",
    "RunnerFn",
    "load_hook_config",
]


#: Injected subprocess runner — returns ``(rc, output)`` given a command.
RunnerFn = Callable[..., Awaitable[tuple[int, str]]]

_DEFAULT_TIMEOUT_SECONDS = 60.0


class HookViolationError(RuntimeError):
    """Raised by ``raise_if_*`` helpers when a hook refuses to let the agent proceed."""


@dataclass(slots=True, frozen=True)
class HookSpec:
    """One hook declaration.

    ``match`` is a tool name glob (``"*"`` matches every tool) for
    ``pre_tool``/``post_tool`` hooks; unused for lifecycle hooks.
    ``command`` is a shell command; ``{{path}}`` and other ``{{...}}`` tokens
    are substituted from the tool's input dict before execution.
    """

    command: str
    match: str = "*"


@dataclass(slots=True, frozen=True)
class HookConfig:
    """Top-level ``hooks:`` section of ``maxwell-daemon.yaml``."""

    pre_tool: tuple[HookSpec, ...] = field(default_factory=tuple)
    post_tool: tuple[HookSpec, ...] = field(default_factory=tuple)
    pre_commit: tuple[str, ...] = field(default_factory=tuple)
    on_prompt: tuple[str, ...] = field(default_factory=tuple)
    on_stop: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True, frozen=True)
class HookOutcome:
    """Outcome of a hook invocation.

    The three boolean fields are mutually-exclusive semantics:
      * ``blocked`` — pre_tool refused the call
      * ``errored`` — post_tool surfaced a non-zero exit
      * ``passed``  — every hook in the sequence exited zero
    ``detail`` carries the stdout/stderr of the first failing hook;
    ``failing_command`` names it for logs/PR comments.
    """

    blocked: bool = False
    errored: bool = False
    passed: bool = True
    detail: str = ""
    failing_command: str = ""


# ── Loader ──────────────────────────────────────────────────────────────────


def load_hook_config(path: Path) -> HookConfig:
    """Load the ``hooks:`` section of ``maxwell-daemon.yaml`` at ``path``.

    Returns an empty :class:`HookConfig` if the file doesn't exist. Raises
    :class:`HookViolationError` on malformed YAML so the daemon fails fast
    rather than running without the gates the operator configured.
    """
    if not path.is_file():
        return HookConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise HookViolationError(f"hook config at {path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise HookViolationError(f"hook config at {path} must be a mapping")
    section = raw.get("hooks") or {}
    if not isinstance(section, dict):
        raise HookViolationError(
            f"hook config at {path} has non-mapping `hooks:` section"
        )

    return HookConfig(
        pre_tool=tuple(_parse_specs(section.get("pre_tool"))),
        post_tool=tuple(_parse_specs(section.get("post_tool"))),
        pre_commit=tuple(_parse_strings(section.get("pre_commit"))),
        on_prompt=tuple(_parse_strings(section.get("on_prompt"))),
        on_stop=tuple(_parse_strings(section.get("on_stop"))),
    )


def _parse_specs(raw: Any) -> list[HookSpec]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HookViolationError(
            f"expected a list of hook specs, got {type(raw).__name__}"
        )
    out: list[HookSpec] = []
    for item in raw:
        if isinstance(item, str):
            out.append(HookSpec(command=item))
            continue
        if not isinstance(item, dict):
            raise HookViolationError(
                f"hook spec must be a string or mapping, got {item!r}"
            )
        cmd = item.get("command")
        if not isinstance(cmd, str):
            raise HookViolationError(f"hook spec is missing `command:` ({item!r})")
        match = item.get("match", "*")
        if not isinstance(match, str):
            raise HookViolationError(f"hook spec `match:` must be a string ({item!r})")
        out.append(HookSpec(command=cmd, match=match))
    return out


def _parse_strings(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HookViolationError(
            f"expected a list of commands, got {type(raw).__name__}"
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise HookViolationError(f"hook entry must be a string, got {item!r}")
        out.append(item)
    return out


# ── Runner ──────────────────────────────────────────────────────────────────


class HookRunner:
    """Runs configured hooks against an injected subprocess runner."""

    def __init__(
        self,
        config: HookConfig,
        *,
        workspace: Path,
        runner: RunnerFn | None = None,
        default_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        require(
            workspace.is_dir(),
            f"HookRunner: workspace {workspace} must be a directory",
        )
        self._cfg = config
        self._workspace = workspace
        self._run = runner or _default_runner
        self._default_timeout = default_timeout_seconds

    # ── pre_tool ────────────────────────────────────────────────────────────

    async def run_pre_tool(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> HookOutcome:
        """Run every matching pre_tool hook; first non-zero exit blocks the call."""
        for spec in self._cfg.pre_tool:
            if not _matches(spec.match, tool_name):
                continue
            command = _substitute(spec.command, tool_input)
            rc, output = await self._run(
                command,
                cwd=str(self._workspace),
                env=_env(tool_name, tool_input, None),
                timeout=self._default_timeout,
            )
            if rc != 0:
                return HookOutcome(
                    blocked=True,
                    passed=False,
                    detail=output,
                    failing_command=spec.command,
                )
        return HookOutcome(blocked=False, passed=True)

    # ── post_tool ───────────────────────────────────────────────────────────

    async def run_post_tool(
        self, tool_name: str, tool_input: dict[str, Any], *, tool_output: str
    ) -> HookOutcome:
        """Run every matching post_tool hook; first non-zero exit is an agent-visible error."""
        for spec in self._cfg.post_tool:
            if not _matches(spec.match, tool_name):
                continue
            command = _substitute(spec.command, tool_input)
            rc, output = await self._run(
                command,
                cwd=str(self._workspace),
                env=_env(tool_name, tool_input, tool_output),
                timeout=self._default_timeout,
            )
            if rc != 0:
                return HookOutcome(
                    errored=True,
                    passed=False,
                    detail=output,
                    failing_command=spec.command,
                )
        return HookOutcome(errored=False, passed=True)

    # ── pre_commit ──────────────────────────────────────────────────────────

    async def run_pre_commit(self) -> HookOutcome:
        """Run every pre_commit command in order; first failure short-circuits."""
        for command in self._cfg.pre_commit:
            rc, output = await self._run(
                command,
                cwd=str(self._workspace),
                env=_env(None, None, None),
                timeout=self._default_timeout,
            )
            if rc != 0:
                return HookOutcome(passed=False, detail=output, failing_command=command)
        return HookOutcome(passed=True)

    async def raise_if_pre_commit_fails(self) -> None:
        """Convenience — raise :class:`HookViolationError` instead of returning an outcome."""
        outcome = await self.run_pre_commit()
        if not outcome.passed:
            raise HookViolationError(
                f"pre_commit hook failed: {outcome.failing_command}\n{outcome.detail}"
            )

    # ── on_prompt / on_stop ─────────────────────────────────────────────────

    async def run_on_prompt(self) -> list[tuple[int, str]]:
        """Run every on_prompt hook in order. Returns their raw outputs for context injection."""
        outputs: list[tuple[int, str]] = []
        for command in self._cfg.on_prompt:
            rc, output = await self._run(
                command,
                cwd=str(self._workspace),
                env=_env(None, None, None),
                timeout=self._default_timeout,
            )
            outputs.append((rc, output))
        return outputs

    async def run_on_stop(self, *, exit_reason: str) -> None:
        """Run every on_stop hook. Exit codes are ignored — it's housekeeping, not a gate."""
        env = {**_env(None, None, None), "MAXWELL_EXIT_REASON": exit_reason}
        for command in self._cfg.on_stop:
            await self._run(
                command,
                cwd=str(self._workspace),
                env=env,
                timeout=self._default_timeout,
            )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _matches(pattern: str, name: str) -> bool:
    """Tool-name glob using :func:`fnmatch.fnmatch` for full glob support.

    Supports patterns like ``"*"``, ``"run_*"``, ``"*.py"``, etc.
    """
    return fnmatch.fnmatch(name, pattern)


def _substitute(command: str, tool_input: dict[str, Any]) -> str:
    """Replace ``{{key}}`` tokens in ``command`` with a shell-safe rendering.

    Every substituted value is passed through :func:`shlex.quote` so values
    containing shell metacharacters (``;``, ``&``, ``|``, backticks, etc.)
    land as a single shell token — hook commands run under ``bash -c`` and
    this is the thin layer that stops an attacker-controlled tool input from
    breaking out of the intended argument.

    Structured values (``dict`` / ``list``) are serialised via
    :func:`json.dumps` first so hook scripts can still parse them; the result
    is then ``shlex.quote``d exactly like any other value.

    Missing keys are left as-is so hook authors can spot unresolved placeholders
    in logs rather than having them silently replaced with ``""``.
    """
    out = command
    for key, value in tool_input.items():
        rendered = (
            json.dumps(value, default=str)
            if isinstance(value, (dict, list))
            else str(value)
        )
        out = out.replace(f"{{{{{key}}}}}", shlex.quote(rendered))
    return out


def _env(
    tool_name: str | None,
    tool_input: dict[str, Any] | None,
    tool_output: str | None,
) -> dict[str, str]:
    """Build the environment visible to a hook subprocess."""
    env: dict[str, str] = _build_run_bash_env()
    env["MAXWELL_TOOL_NAME"] = tool_name or ""
    env["MAXWELL_TOOL_INPUT"] = json.dumps(tool_input or {}, default=str)
    env["MAXWELL_TOOL_OUTPUT"] = tool_output or ""
    return env


async def _default_runner(
    command: str, *, cwd: str, env: dict[str, str], timeout: float
) -> tuple[int, str]:
    """Real subprocess runner used in production. Combines stdout and stderr."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timeout after {timeout}s"
    return proc.returncode or 0, stdout.decode(errors="replace")
