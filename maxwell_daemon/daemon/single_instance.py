"""Single-instance guard for the daemon storage root (#975).

Two daemons sharing one storage root corrupt each other's state: instance B's
crash-recovery marks instance A's in-flight tasks FAILED and re-enqueues every
QUEUED row, so both instances execute every task — duplicate LLM spend,
duplicate PRs. This module provides a cross-platform exclusive lock file so a
second daemon against the same storage root refuses to start.

The lock is advisory (``flock`` on POSIX, ``msvcrt.locking`` on Windows) and is
held for the process lifetime. A stale lock left by a crashed process is
detected via the recorded PID and reclaimed, so a clean restart is never
blocked by a previous crash.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import TracebackType

from maxwell_daemon.logging import get_logger

log = get_logger(__name__)


class InstanceLockError(RuntimeError):
    """Raised when the storage root is already locked by a live daemon."""


def _pid_is_alive(pid: int) -> bool:
    """Best-effort check whether ``pid`` names a running process."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import ctypes

        # ``windll`` only exists on the Windows ctypes build; reach it via
        # getattr so static analysis on POSIX doesn't flag a missing attribute.
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — treat as alive.
        return True
    return True


class InstanceLock:
    """Exclusive, PID-stamped lock file for one storage root.

    Usage::

        lock = InstanceLock(storage_root)
        lock.acquire()   # raises InstanceLockError if a live daemon holds it
        ...
        lock.release()
    """

    def __init__(self, storage_root: Path, *, name: str = "daemon.lock") -> None:
        self._path = Path(storage_root).expanduser() / name
        self._fh: object | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _read_recorded_pid(self) -> int | None:
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def acquire(self) -> None:
        if self._fh is not None:
            return  # already held by this instance
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # If a lock file exists and names a live PID, refuse. A stale lock from a
        # crashed process is reclaimed below by simply taking the OS-level lock.
        recorded = self._read_recorded_pid()
        if recorded is not None and recorded != os.getpid() and _pid_is_alive(recorded):
            raise InstanceLockError(
                f"another daemon (pid {recorded}) already holds {self._path}. "
                "Refusing to start a second instance against the same storage root."
            )

        fh = open(self._path, "a+", encoding="utf-8")  # noqa: SIM115 - held for lifetime
        try:
            self._os_lock(fh)
        except OSError as exc:
            fh.close()
            raise InstanceLockError(
                f"storage root {self._path.parent} is locked by another daemon"
            ) from exc

        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        os.fsync(fh.fileno())
        self._fh = fh
        log.info("acquired single-instance lock", path=str(self._path), pid=os.getpid())

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            self._os_unlock(fh)
        except OSError:
            log.warning("failed to release instance lock cleanly", exc_info=True)
        finally:
            with contextlib.suppress(OSError):
                fh.close()  # type: ignore[attr-defined]
            self._fh = None

    # -- platform-specific locking ------------------------------------------- #

    @staticmethod
    def _os_lock(fh: object) -> None:
        if os.name == "nt":  # pragma: no cover - Windows path
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]

    @staticmethod
    def _os_unlock(fh: object) -> None:
        if os.name == "nt":  # pragma: no cover - Windows path
            import msvcrt

            fh.seek(0)  # type: ignore[attr-defined]
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
