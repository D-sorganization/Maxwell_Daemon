"""Built-in agent tools — sandboxed to a workspace root.

The goal is every tool uses the *same* handler regardless of which backend
dispatched it. Factories (``make_read_file``, …) bind a workspace ``root`` so
each invocation is limited to that directory. Resolution goes through
``_resolve`` which refuses:

  * absolute paths outside the root
  * ``..`` traversal
  * symlinks whose target escapes the root

These rules apply to every path argument across every tool.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from maxwell_daemon.browser import BrowserAction, BrowserRequest, BrowserResult, BrowserService
from maxwell_daemon.core.actions import ActionKind, ActionRiskLevel
from maxwell_daemon.tools.mcp import (
    HookRunnerProtocol,
    ToolInvocationStore,
    ToolParam,
    ToolPolicy,
    ToolRegistry,
    mcp_tool,
)

if TYPE_CHECKING:
    from maxwell_daemon.core.action_service import ActionService

__all__ = [
    "BashRunner",
    "SandboxViolationError",
    "build_default_registry",
    "make_browser_screenshot",
    "make_edit_file",
    "make_glob_files",
    "make_grep_files",
    "make_open_browser_url",
    "make_read_file",
    "make_run_bash",
    "make_write_file",
]

#: Default timeout on ``run_bash`` when the model doesn't specify one.
DEFAULT_BASH_TIMEOUT_SECONDS = 30.0
#: Max bytes of combined stdout+stderr returned to the model; beyond this we
#: truncate so one runaway command can't poison the context window.
DEFAULT_MAX_OUTPUT_BYTES = 64_000

#: Runner signature — takes argv, cwd, and timeout; returns (rc, stdout, stderr).
#: Injected so tests don't spawn real subprocesses.
BashRunner = Callable[[list[str], str, float], Awaitable[tuple[int, bytes, bytes]]]


class SandboxViolationError(ValueError):
    """Raised when a tool would access a path outside its workspace."""


def _resolve(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and refuse anything that escapes.

    We deliberately resolve with ``strict=False`` so write/create paths that
    don't exist yet still pass — the constraint is the *resolved* path must
    live under ``root``. When a symlink is in play we also check the real
    target via ``Path.resolve(strict=False)``.
    """
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise SandboxViolationError(
            f"path {rel!r} resolves outside the workspace root {root_resolved}"
        ) from exc
    return candidate


def _is_within_root(root_resolved: Path, path: Path) -> bool:
    """Return whether ``path`` resolves inside ``root_resolved``."""
    try:
        path.resolve().relative_to(root_resolved)
    except ValueError:
        return False
    return True


# ── read_file ────────────────────────────────────────────────────────────────
def make_read_file(root: Path) -> Callable[..., str]:
    @mcp_tool(
        name="read_file",
        description="Read a UTF-8 text file from the workspace. Returns the file contents.",
        capabilities=frozenset({"file_read", "repo_read"}),
        risk_level="read_only",
        params=[
            ToolParam(
                name="path",
                type="string",
                description="Path relative to the workspace root",
            )
        ],
    )
    def read_file(path: str) -> str:
        resolved = _resolve(root, path)
        if not resolved.is_file():
            raise FileNotFoundError(f"{path!r} not found or is not a regular file")
        return resolved.read_text(encoding="utf-8", errors="replace")

    return read_file


# ── write_file ───────────────────────────────────────────────────────────────
def make_write_file(
    root: Path,
    *,
    action_service: ActionService | None = None,
    task_id: str | None = None,
) -> Callable[..., str]:
    @mcp_tool(
        name="write_file",
        description=(
            "Write or overwrite a UTF-8 text file in the workspace. Creates parent "
            "directories as needed. Returns a short confirmation."
        ),
        capabilities=frozenset({"file_write", "repo_write"}),
        risk_level="local_write",
        requires_approval=True,
        params=[
            ToolParam(
                name="path",
                type="string",
                description="Path relative to the workspace root",
            ),
            ToolParam(name="content", type="string", description="Full file content to write"),
        ],
    )
    def write_file(path: str, content: str) -> str:
        import os
        import tempfile

        resolved = _resolve(root, path)
        action_id: str | None = None
        if action_service is not None and task_id is not None:
            action, decision = action_service.propose(
                task_id=task_id,
                kind=ActionKind.FILE_WRITE,
                summary=f"write file {path}",
                payload={"path": path, "bytes": len(content)},
                risk_level=ActionRiskLevel.MEDIUM,
            )
            action_id = action.id
            if not decision.allowed:
                action_service.skip(action.id, reason=decision.reason)
                return f"action {action.id} skipped: {decision.reason}"
            if decision.requires_approval:
                return (
                    f"action {action.id} pending approval: {action.summary} "
                    "(approval records the proposal only)"
                )
            action_service.approve(action.id, actor="policy")
            action_service.mark_running(action.id)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=resolved.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, resolved)
        except Exception:
            os.unlink(tmp_path)
            if action_id is not None and action_service is not None:
                action_service.mark_failed(action_id, error="write_file failed")
            raise
        if action_id is not None and action_service is not None:
            action_service.mark_applied(action_id, result={"path": path, "bytes": len(content)})
        return f"wrote {len(content)} bytes to {path}"

    return write_file


# ── edit_file ────────────────────────────────────────────────────────────────
def make_edit_file(
    root: Path,
    *,
    action_service: ActionService | None = None,
    task_id: str | None = None,
) -> Callable[..., str]:
    @mcp_tool(
        name="edit_file",
        description=(
            "Replace a single, unambiguous occurrence of ``old_string`` with "
            "``new_string`` in the named file. Refuses if ``old_string`` is "
            "missing or appears more than once."
        ),
        capabilities=frozenset({"file_read", "file_write", "repo_write"}),
        risk_level="local_write",
        requires_approval=True,
        params=[
            ToolParam(
                name="path",
                type="string",
                description="Path relative to the workspace root",
            ),
            ToolParam(name="old_string", type="string", description="Exact text to replace"),
            ToolParam(name="new_string", type="string", description="Replacement text"),
        ],
    )
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        resolved = _resolve(root, path)
        if not resolved.is_file():
            raise FileNotFoundError(f"{path!r} not found")
        content = resolved.read_text(encoding="utf-8")
        occurrences = content.count(old_string)
        if occurrences == 0:
            raise ValueError(f"old_string not found in {path!r}")
        if occurrences > 1:
            raise ValueError(
                f"old_string appears {occurrences} times in {path!r} — "
                "refuse ambiguous edits; include more surrounding context"
            )
        action_id: str | None = None
        if action_service is not None and task_id is not None:
            action, decision = action_service.propose(
                task_id=task_id,
                kind=ActionKind.FILE_EDIT,
                summary=f"edit file {path}",
                payload={"path": path, "old_bytes": len(old_string), "new_bytes": len(new_string)},
                risk_level=ActionRiskLevel.MEDIUM,
            )
            action_id = action.id
            if not decision.allowed:
                action_service.skip(action.id, reason=decision.reason)
                return f"action {action.id} skipped: {decision.reason}"
            if decision.requires_approval:
                return (
                    f"action {action.id} pending approval: {action.summary} "
                    "(approval records the proposal only)"
                )
            action_service.approve(action.id, actor="policy")
            action_service.mark_running(action.id)
        import os
        import tempfile

        new_content = content.replace(old_string, new_string)
        fd, tmp_path = tempfile.mkstemp(dir=resolved.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, resolved)
        except Exception:
            os.unlink(tmp_path)
            if action_id is not None and action_service is not None:
                action_service.mark_failed(action_id, error="edit_file failed")
            raise
        if action_id is not None and action_service is not None:
            action_service.mark_applied(action_id, result={"path": path})
        return f"edited {path}"

    return edit_file


# ── run_bash ─────────────────────────────────────────────────────────────────

#: Env vars that *always* pass through to the ``run_bash`` child. Anything
#: outside this set is stripped so secrets accidentally exported in the
#: daemon's process (``AWS_*``, ``ANTHROPIC_API_KEY``, …) cannot leak into an
#: LLM-controlled subprocess. Operators can extend the list on an ad-hoc basis
#: with the ``MAXWELL_ALLOW_ENV`` comma-separated allowlist.
_RUN_BASH_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "SHELL"}
)


def _build_run_bash_env() -> dict[str, str]:
    """Build the environment passed to ``run_bash`` subprocesses.

    Inherits only the static allowlist plus any names named in
    ``MAXWELL_ALLOW_ENV`` (comma-separated). Pure function so tests can poke
    it directly.
    """
    allowed: set[str] = set(_RUN_BASH_ENV_ALLOWLIST)
    extra = os.environ.get("MAXWELL_ALLOW_ENV", "")
    for name in extra.split(","):
        trimmed = name.strip()
        if trimmed:
            allowed.add(trimmed)
    return {k: v for k, v in os.environ.items() if k in allowed}


async def _default_runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_build_run_bash_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b"", f"timeout after {timeout}s".encode()
    return proc.returncode or 0, stdout, stderr


def make_run_bash(
    root: Path,
    *,
    runner: BashRunner | None = None,
    default_timeout: float = DEFAULT_BASH_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    action_service: ActionService | None = None,
    task_id: str | None = None,
) -> Callable[..., Awaitable[str]]:
    run = runner or _default_runner

    @mcp_tool(
        name="run_bash",
        description=(
            "Run a bash command inside the workspace root. The command runs via "
            "``bash -c`` with ``cwd`` pinned to the workspace. Environment is "
            "reduced to a safe allowlist (see MAXWELL_ALLOW_ENV). Output is "
            "combined stdout+stderr, truncated to a bounded length."
        ),
        capabilities=frozenset({"shell_read", "shell_write"}),
        risk_level="command_execution",
        requires_approval=True,
        params=[
            ToolParam(name="command", type="string", description="Bash command line"),
            ToolParam(
                name="timeout_seconds",
                type="integer",
                description="Wall-clock limit",
                required=False,
            ),
        ],
    )
    async def run_bash(command: str, timeout_seconds: int | float | None = None) -> str:
        timeout = float(timeout_seconds) if timeout_seconds is not None else default_timeout
        cwd = str(root.resolve())
        action_id: str | None = None
        if action_service is not None and task_id is not None:
            action, decision = action_service.propose(
                task_id=task_id,
                kind=ActionKind.COMMAND,
                summary=f"run command: {command[:80]}",
                payload={"command": command, "timeout_seconds": timeout},
                risk_level=ActionRiskLevel.HIGH,
            )
            action_id = action.id
            if not decision.allowed:
                action_service.skip(action.id, reason=decision.reason)
                return f"action {action.id} skipped: {decision.reason}"
            if decision.requires_approval:
                return (
                    f"action {action.id} pending approval: {action.summary} "
                    "(approval records the proposal only)"
                )
            action_service.approve(action.id, actor="policy")
            action_service.mark_running(action.id)
        # ``-c`` (no ``-l``) so login-profile files don't run and leak state.
        rc, stdout, stderr = await run(["bash", "-c", command], cwd, timeout)
        body = stdout + (b"\n" + stderr if stderr else b"")
        truncated = False
        if len(body) > max_output_bytes:
            body = body[:max_output_bytes]
            truncated = True
        text = body.decode(errors="replace")
        if rc != 0:
            text = f"(exit {rc})\n{text}"
        if truncated:
            text += f"\n[output truncated to {max_output_bytes} bytes]"
        if action_id is not None and action_service is not None:
            if rc == 0:
                action_service.mark_applied(
                    action_id,
                    result={"exit_code": rc, "output_preview": text[:4000]},
                )
            else:
                action_service.mark_failed(action_id, error=f"command exited {rc}")
        return text

    return run_bash


# ── glob_files ───────────────────────────────────────────────────────────────
def make_glob_files(root: Path) -> Callable[..., str]:
    @mcp_tool(
        name="glob_files",
        description=(
            "List files matching a glob pattern, one per line, paths relative to "
            "the workspace root. Use ``**/`` for recursive matching."
        ),
        capabilities=frozenset({"file_read", "repo_read"}),
        risk_level="read_only",
        params=[
            ToolParam(
                name="pattern",
                type="string",
                description="Glob pattern (e.g. '**/*.py')",
            ),
        ],
    )
    def glob_files(pattern: str) -> str:
        root_resolved = root.resolve()
        matches = sorted(
            p.relative_to(root_resolved).as_posix()
            for p in root_resolved.glob(pattern)
            if _is_within_root(root_resolved, p) and p.is_file()
        )
        if not matches:
            return f"no matches for pattern {pattern!r}"
        return "\n".join(matches)

    return glob_files


# ── grep_files ───────────────────────────────────────────────────────────────
def make_grep_files(root: Path) -> Callable[..., str]:
    @mcp_tool(
        name="grep_files",
        description=(
            "Search files under the workspace for a Python-regex pattern. Returns "
            "matching lines as ``path:lineno:line``. Optionally scoped to a glob."
        ),
        capabilities=frozenset({"file_read", "repo_read"}),
        risk_level="read_only",
        params=[
            ToolParam(name="pattern", type="string", description="Python regex pattern"),
            ToolParam(
                name="glob",
                type="string",
                description="Glob filter (e.g. '*.py'); default scans all files",
                required=False,
            ),
        ],
    )
    def grep_files(pattern: str, glob: str | None = None) -> str:
        regex = re.compile(pattern)
        root_resolved = root.resolve()
        scan = root_resolved.rglob(glob) if glob else root_resolved.rglob("*")
        hits: list[str] = []
        for path in scan:
            if not _is_within_root(root_resolved, path) or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = path.relative_to(root_resolved).as_posix()
                    hits.append(f"{rel}:{lineno}:{line}")
        if not hits:
            return f"no match for {pattern!r}"
        return "\n".join(hits)

    return grep_files


# ── open_browser_url ─────────────────────────────────────────────────────────
def _format_browser_result(browser_result: BrowserResult) -> str:
    parts: list[str] = [
        f"url: {browser_result.url}",
        f"action: {browser_result.action.value}",
    ]
    if browser_result.title:
        parts.append(f"title: {browser_result.title}")
    if browser_result.screenshot_artifact_id:
        parts.append(f"screenshot_artifact_id: {browser_result.screenshot_artifact_id}")
    if browser_result.console_artifact_id:
        parts.append(f"console_artifact_id: {browser_result.console_artifact_id}")
    if browser_result.page_error_artifact_id:
        parts.append(f"page_error_artifact_id: {browser_result.page_error_artifact_id}")
    if browser_result.metadata:
        metadata = ", ".join(
            f"{key}={value!r}" for key, value in sorted(browser_result.metadata.items())
        )
        parts.append(f"metadata: {metadata}")
    if browser_result.text:
        parts.append("")
        parts.append(browser_result.text)
    return "\n".join(parts)


def make_open_browser_url(browser_service: BrowserService) -> Callable[..., Awaitable[str]]:
    @mcp_tool(
        name="open_browser_url",
        description=(
            "Open an HTTP(S) URL through the configured browser automation runner and "
            "return a text snapshot. Optional allowed_hosts limits which hosts may be visited."
        ),
        capabilities=frozenset({"network", "artifact_write"}),
        risk_level="network_write",
        requires_approval=True,
        params=[
            ToolParam(name="url", type="string", description="HTTP(S) URL to visit"),
            ToolParam(
                name="allowed_hosts",
                type="array",
                description="Optional host allowlist, including '*.example.com' wildcards",
                required=False,
            ),
            ToolParam(
                name="timeout_seconds",
                type="number",
                description="Wall-clock browser action limit",
                required=False,
            ),
        ],
    )
    async def open_browser_url(
        url: str,
        allowed_hosts: list[str] | None = None,
        timeout_seconds: int | float | None = None,
    ) -> str:
        request = BrowserRequest(
            url=url,
            action=BrowserAction.SNAPSHOT,
            allowed_hosts=tuple(allowed_hosts or ()),
            timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else 30.0,
        )
        result = await browser_service.run(request)
        return _format_browser_result(result)

    return open_browser_url


# ── browser_screenshot ──────────────────────────────────────────────────────
def make_browser_screenshot(browser_service: BrowserService) -> Callable[..., Awaitable[str]]:
    @mcp_tool(
        name="browser_screenshot",
        description=(
            "Open an HTTP(S) URL through the configured browser automation runner, "
            "capture a screenshot, and return the durable screenshot artifact id."
        ),
        capabilities=frozenset({"network", "artifact_write"}),
        risk_level="network_write",
        requires_approval=True,
        params=[
            ToolParam(name="url", type="string", description="HTTP(S) URL to visit"),
            ToolParam(
                name="allowed_hosts",
                type="array",
                description="Optional host allowlist, including '*.example.com' wildcards",
                required=False,
            ),
            ToolParam(
                name="timeout_seconds",
                type="number",
                description="Wall-clock browser action limit",
                required=False,
            ),
        ],
    )
    async def browser_screenshot(
        url: str,
        allowed_hosts: list[str] | None = None,
        timeout_seconds: int | float | None = None,
    ) -> str:
        request = BrowserRequest(
            url=url,
            action=BrowserAction.SCREENSHOT,
            allowed_hosts=tuple(allowed_hosts or ()),
            timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else 30.0,
        )
        result = await browser_service.run(request)
        if result.screenshot_artifact_id is None:
            raise ValueError("browser screenshot did not produce a screenshot artifact")
        return _format_browser_result(result)

    return browser_screenshot


# ── default registry ────────────────────────────────────────────────────────
def build_default_registry(
    root: Path,
    *,
    bash_runner: BashRunner | None = None,
    hook_runner: HookRunnerProtocol | None = None,
    action_service: ActionService | None = None,
    task_id: str | None = None,
    policy: ToolPolicy | None = None,
    invocation_store: ToolInvocationStore | None = None,
    browser_service: BrowserService | None = None,
) -> ToolRegistry:
    """Return a ``ToolRegistry`` with built-in tools bound to ``root``.

    When ``hook_runner`` is supplied, every tool invocation passes through
    ``pre_tool`` and ``post_tool`` gates — deterministic, LLM-bypass-proof
    code-standards enforcement (see :mod:`maxwell_daemon.hooks`).

    Callers who want a different tool set can build their own registry and
    register only what they need — this helper is the agent-loop default.
    """
    reg = ToolRegistry(
        hook_runner=hook_runner,
        policy=policy,
        invocation_store=invocation_store,
    )
    reg.register_from_function(make_read_file(root))
    reg.register_from_function(
        make_write_file(root, action_service=action_service, task_id=task_id)
    )
    reg.register_from_function(make_edit_file(root, action_service=action_service, task_id=task_id))
    reg.register_from_function(make_glob_files(root))
    reg.register_from_function(make_grep_files(root))
    reg.register_from_function(
        make_run_bash(
            root,
            runner=bash_runner,
            action_service=action_service,
            task_id=task_id,
        )
    )
    if browser_service is not None:
        reg.register_from_function(make_open_browser_url(browser_service))
        reg.register_from_function(make_browser_screenshot(browser_service))
    return reg
