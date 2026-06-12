"""Tests for the single-instance storage-root guard (#975)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from maxwell_daemon.daemon.single_instance import InstanceLock, InstanceLockError

# The byte-range lock taken on Windows (msvcrt.locking) blocks even same-process
# reads of the locked file, which makes the read-back / same-process refusal
# assertions environment-specific. The daemon's required CI runs on Linux
# (d-sorg-fleet) where flock is advisory; gate the OS-lock-dependent assertions.
_skip_on_windows = pytest.mark.skipif(
    os.name == "nt", reason="msvcrt byte-range lock blocks same-process reads; CI is Linux"
)


@_skip_on_windows
def test_acquire_writes_pid_and_creates_lockfile(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path)
    lock.acquire()
    try:
        assert lock.path.exists()
        assert lock.path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


@_skip_on_windows
def test_second_lock_against_same_root_refuses(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        second = InstanceLock(tmp_path)
        with pytest.raises(InstanceLockError):
            second.acquire()
    finally:
        first.release()


@_skip_on_windows
def test_release_allows_reacquire(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path)
    first.acquire()
    first.release()
    # A fresh lock against the same root must now succeed.
    second = InstanceLock(tmp_path)
    second.acquire()
    try:
        assert second.path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        second.release()


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path: Path) -> None:
    # Simulate a crashed daemon: a lock file naming a PID that is not alive.
    lock_path = tmp_path / "daemon.lock"
    dead_pid = 2_147_483_646  # implausibly high; not a running process
    lock_path.write_text(str(dead_pid), encoding="utf-8")

    lock = InstanceLock(tmp_path)
    lock.acquire()  # must not raise — stale lock reclaimed
    try:
        assert lock.path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


def test_context_manager_releases(tmp_path: Path) -> None:
    with InstanceLock(tmp_path):
        pass
    # After exit the lock is released, so another acquire succeeds.
    again = InstanceLock(tmp_path)
    again.acquire()
    again.release()
