from pathlib import Path

import pytest

from maxwell_daemon.core.memory_annealer import MemoryAnnealer
from maxwell_daemon.core.roles import Job


class DummySummarizer:
    async def execute(self, job: Job, tools=None):
        from maxwell_daemon.backends.base import BackendResponse, TokenUsage

        return BackendResponse(
            content="Annealed architectural state.",
            finish_reason="stop",
            usage=TokenUsage(),
            model="dummy",
            backend="dummy",
            raw={},
        )


@pytest.mark.asyncio
async def test_memory_annealer_cleanup(tmp_path: Path):
    raw_dir = tmp_path / ".maxwell" / "raw_logs"
    raw_dir.mkdir(parents=True)
    (raw_dir / "session_1.log").write_text("lots of useless tokens and debug output")
    (raw_dir / "session_2.log").write_text("more useless debug output")

    assert len(list(raw_dir.iterdir())) == 2

    summarizer = DummySummarizer()
    annealer = MemoryAnnealer(workspace=tmp_path, summarizer_role=summarizer)  # type: ignore

    result = await annealer.anneal()

    assert "reclaimed disk space" in result.lower()

    # DbC Verification: Ensure raw logs were deleted
    assert len(list(raw_dir.iterdir())) == 0

    # Ensure memory was written
    memory_file = tmp_path / ".maxwell" / "memory" / "architectural_state.md"
    assert memory_file.exists()
    assert memory_file.read_text() == "Annealed architectural state."


def test_memory_annealer_status_counts_raw_logs(tmp_path: Path) -> None:
    raw_dir = tmp_path / ".maxwell" / "raw_logs"
    raw_dir.mkdir(parents=True)
    (raw_dir / "session_1.log").write_text("abc", encoding="utf-8")
    (raw_dir / "ignored.txt").write_text("not raw memory", encoding="utf-8")

    annealer = MemoryAnnealer(workspace=tmp_path)
    status = annealer.status()

    assert status.workspace == tmp_path
    assert status.raw_log_count == 1
    assert status.raw_bytes == 3
    assert status.memory_exists is False
