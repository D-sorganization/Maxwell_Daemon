"""Memory Annealer for contextual consolidation.

The Annealer runs as a background maintenance cycle. It reads verbose,
token-heavy raw execution logs, uses a summarizer role to compress them
into dense architectural markdown, and then aggressively deletes the raw logs
to conserve disk space and adhere to the "Keep Knowledge in Plain Text" principle.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from maxwell_daemon.core.roles import Job, RolePlayer


class MemoryAnnealer:
    """Consolidates raw execution logs into dense architectural memory."""

    def __init__(self, workspace: Path, summarizer_role: RolePlayer) -> None:
        self.workspace = workspace
        self.memory_dir = workspace / ".maxwell" / "memory"
        self.raw_logs_dir = workspace / ".maxwell" / "raw_logs"
        self.summarizer = summarizer_role

    async def anneal(self) -> str:
        """Reads raw logs, generates a summary, and purges raw logs."""
        if not self.raw_logs_dir.exists() or not any(self.raw_logs_dir.iterdir()):
            return "No raw memory to anneal."

        raw_content = ""
        for log_file in self.raw_logs_dir.glob("*.log"):
            raw_content += (
                log_file.read_text(encoding="utf-8", errors="replace") + "\n\n"
            )

        job = Job(
            instructions=(
                "Compress this execution history into dense architectural knowledge. "
                "Discard all fluffy dialogue, keep only technical decisions and patterns.\n\n"
                f"{raw_content}"
            )
        )

        response = await self.summarizer.execute(job)
        compressed_memory = response.content

        self.memory_dir.mkdir(parents=True, exist_ok=True)
        annealed_file = self.memory_dir / "architectural_state.md"

        # Overwrite the state file with the latest truth
        annealed_file.write_text(compressed_memory, encoding="utf-8")

        # Responsible Cleanup: Delete the raw logs to aggressively save disk space
        shutil.rmtree(self.raw_logs_dir)
        self.raw_logs_dir.mkdir(parents=True, exist_ok=True)

        return "Memory successfully annealed. Reclaimed disk space by purging raw logs."
