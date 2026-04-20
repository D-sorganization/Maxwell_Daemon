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
import re
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

from maxwell_daemon.tools.mcp import (
    ToolParam,
    ToolRegistry,
    mcp_tool,
)

__all__ = [
    "BashRunner",
    "SandboxViolationError",
    "build_default_registry",
    "make_edit_file",
    "make_glob_files",
    "make_grep_files",
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


# ── read_file ────────────────────────────────────────────────────────────────
def make_read_file(root: Path) -> Callable[..., str]:
    @mcp_tool(
        name="read_file",
        description="Read a UTF-8 text file from the workspace. Returns the file contents.",
        params=[
            ToolParam(name="path", type="string", description="Path relative to the workspace root")
        ],
    )
    def read_file(path: str) -> str:
        resolved = _resolve(root, path)
        if not resolved.is_file():
            raise FileNotFoundError(f"{path!r} not found or is not a regular file")
        return resolved.read_text(encoding="utf-8", errors="replace")

    return read_file


# ── write_file ───────────────────────────────────────────────────────────────
def make_write_file(root: Path) -> Callable[..., str]:
    @mcp_tool(
        name="write_file",
        description=(
            "Write or overwrite a UTF-8 text file in the workspace. Creates parent "
            "directories as needed. Returns a short confirmation."
        ),
        params=[
            ToolParam(
                name="path", type="string", description="Path relative to the workspace root"
            ),
            ToolParam(name="content", type="string", description="Full file content to write"),
        ],
    )
    def write_file(path: str, content: str) -> str:
        resolved = _resolve(root, path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} bytes to {path}"

    return write_file


# ── edit_file ────────────────────────────────────────────────────────────────
def make_edit_file(root: Path) -> Callable[..., str]:
    @mcp_tool(
        name="edit_file",
        description=(
            "Replace a single, unambiguous occurrence of ``old_string`` with "
            "``new_string`` in the named file. Refuses if ``old_string`` is "
            "missing or appears more than once."
        ),
        params=[
            ToolParam(
                name="path", type="string", description="Path relative to the workspace root"
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
        resolved.write_text(content.replace(old_string, new_string), encoding="utf-8")
        return f"edited {path}"

    return edit_file


# ── run_bash ─────────────────────────────────────────────────────────────────
async def _default_runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
) -> Callable[..., Awaitable[str]]:
    run = runner or _default_runner

    @mcp_tool(
        name="run_bash",
        description=(
            "Run a bash command inside the workspace root. The command runs via "
            "``bash -lc`` with ``cwd`` pinned to the workspace. Output is combined "
            "stdout+stderr, truncated to a bounded length."
        ),
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
        rc, stdout, stderr = await run(["bash", "-lc", command], cwd, timeout)
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
        params=[
            ToolParam(name="pattern", type="string", description="Glob pattern (e.g. '**/*.py')"),
        ],
    )
    def glob_files(pattern: str) -> str:
        root_resolved = root.resolve()
        matches = sorted(
            str(p.relative_to(root_resolved)) for p in root_resolved.glob(pattern) if p.is_file()
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
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = path.relative_to(root_resolved)
                    hits.append(f"{rel}:{lineno}:{line}")
        if not hits:
            return f"no match for {pattern!r}"
        return "\n".join(hits)

    return grep_files


# ── default registry ────────────────────────────────────────────────────────
def build_default_registry(root: Path, *, bash_runner: BashRunner | None = None) -> ToolRegistry:
    """Return a ``ToolRegistry`` with all six built-in tools bound to ``root``.

    Callers who want a different tool set can build their own registry and
    register only what they need — this helper is the agent-loop default.
    """
    reg = ToolRegistry()
    reg.register_from_function(make_read_file(root))
    reg.register_from_function(make_write_file(root))
    reg.register_from_function(make_edit_file(root))
    reg.register_from_function(make_glob_files(root))
    reg.register_from_function(make_grep_files(root))
    reg.register_from_function(make_run_bash(root, runner=bash_runner))
    return reg
