"""TestRunner — stream output as it's produced, not just at completion."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from maxwell_daemon.gh.test_runner import TestRunner


class _LineStreamingRunner:
    """Fake runner that emits output one chunk at a time via the on_chunk hook."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    async def __call__(
        self,
        *argv: str,
        cwd: str | None = None,
        stdin: bytes | None = None,
        on_chunk: Any = None,
    ) -> tuple[int, bytes, bytes]:
        body: list[bytes] = []
        for line in self._lines:
            body.append(line)
            if on_chunk is not None:
                await on_chunk(line.decode(errors="replace"), "stdout")
        return 0, b"".join(body), b""


class TestStreaming:
    def test_on_chunk_receives_each_line_in_order(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        received: list[tuple[str, str]] = []

        async def on_chunk(text: str, stream: str) -> None:
            received.append((stream, text))

        runner = _LineStreamingRunner(
            [b"collecting...\n", b"test_a PASSED\n", b"test_b PASSED\n"]
        )
        tr = TestRunner(runner=runner, on_chunk=on_chunk)
        asyncio.run(tr.detect_and_run(tmp_path))
        assert len(received) == 3
        assert [stream for stream, _ in received] == ["stdout"] * 3
        assert "test_a PASSED" in received[1][1]

    def test_no_callback_keeps_legacy_behaviour(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        runner = _LineStreamingRunner([b"ok\n"])
        tr = TestRunner(runner=runner)
        result = asyncio.run(tr.detect_and_run(tmp_path))
        assert result.passed is True
        assert "ok" in result.output_tail
