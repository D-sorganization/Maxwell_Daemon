"""Three-tier agent memory.

Layered on top of SQLite (same DB file as the cost ledger by convention) plus
in-memory state. No external services, no vector DB in v1 — SQLite FTS5 gets
us searchable episodic memory at a tenth of the complexity.

* :class:`ScratchPad` — ephemeral, per-task working context across retries
* :class:`RepoProfile` — durable per-repo facts (language, style, conventions)
* :class:`EpisodicStore` — FTS5-indexed history of past successful issue→PR runs
* :class:`MemoryManager` — composite; what the IssueExecutor actually talks to
"""

from maxwell_daemon.memory.episodic import Episode, EpisodicStore
from maxwell_daemon.memory.manager import MemoryManager
from maxwell_daemon.memory.profile import RepoProfile
from maxwell_daemon.memory.scratchpad import ScratchEntry, ScratchPad

__all__ = [
    "Episode",
    "EpisodicStore",
    "MemoryManager",
    "RepoProfile",
    "ScratchEntry",
    "ScratchPad",
]
