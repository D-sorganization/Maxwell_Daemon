"""Filesystem helpers shared across the daemon.

The canonical durable-write pattern (write to a sibling temp file, ``fsync``,
then ``os.replace``) previously lived only inside ``audit.py``. It is hoisted
here so the scheduler dedup file and the config migration can reuse it instead
of doing non-atomic ``write_text`` / ``open("w")`` that a mid-write crash can
truncate (#979).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    """Atomically write ``data`` to ``path``.

    Guarantees that a reader either sees the previous file contents or the
    fully-written new contents — never a truncated/partial file — even if the
    process crashes mid-write. Implemented as temp-file + ``fsync`` + atomic
    ``os.replace`` on the same directory (so the rename cannot cross devices).

    The parent directory is created if missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        # Only OS-level I/O errors are expected here; surface anything else
        # (e.g. MemoryError) unchanged. Clean up the temp file on failure.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
