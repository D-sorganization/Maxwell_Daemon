"""Tests for the shared atomic-write helper (#979)."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.fsutil import atomic_write_text


def test_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "f.txt"
    atomic_write_text(target, "x")
    assert target.read_text(encoding="utf-8") == "x"


def test_overwrites_atomically_no_temp_leftover(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    atomic_write_text(target, "old")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    # No ``*.tmp`` siblings must remain after a successful replace.
    assert list(tmp_path.glob("*.tmp")) == []
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_previous_content_intact_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.txt"
    atomic_write_text(target, "original")

    import os

    def _boom(_src: str, _dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_text(target, "corrupt")

    # The original file must survive a failed write, and the temp file cleaned up.
    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob("*.tmp")) == []
