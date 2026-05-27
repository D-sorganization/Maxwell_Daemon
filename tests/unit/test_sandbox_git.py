"""Unit tests for Git tracker and worktree safety gates."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from maxwell_daemon.sandbox.git import GitTracker, GitWorktree


@pytest.fixture
def temp_git_repo() -> Generator[Path, None, None]:
    """Fixture that initializes a temporary Git repository with an initial commit."""
    temp_dir = tempfile.mkdtemp(prefix="maxwell_test_repo_")
    repo_path = Path(temp_dir)

    def run_git(args: list[str]) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=True,
        )

    try:
        run_git(["init", "--template="])
        run_git(["config", "user.name", "Maxwell Test"])
        run_git(["config", "user.email", "test@maxwell.local"])
        run_git(["config", "commit.gpgsign", "false"])

        # Create an initial commit
        initial_file = repo_path / "initial.txt"
        initial_file.write_text("initial content", encoding="utf-8")
        run_git(["add", "initial.txt"])
        run_git(["commit", "--no-verify", "-m", "Initial commit"])

        yield repo_path
    finally:
        # Clean up
        shutil.rmtree(repo_path, ignore_errors=True)


def test_git_tracker_snapshot_clean(temp_git_repo: Path) -> None:
    """Test that taking a snapshot of a clean workspace returns a clean snapshot ID."""
    tracker = GitTracker(temp_git_repo)
    snapshot_id = tracker.take_snapshot()
    assert snapshot_id.startswith("clean:")


def test_git_tracker_snapshot_dirty_and_restore(temp_git_repo: Path) -> None:
    """Test taking a snapshot with dirty/untracked changes, modifying the workspace, and restoring."""
    tracker = GitTracker(temp_git_repo)

    # 1. Setup dirty working directory state
    # Unstaged modification
    initial_file = temp_git_repo / "initial.txt"
    initial_file.write_text("modified initial content", encoding="utf-8")

    # Staged change
    staged_file = temp_git_repo / "staged.txt"
    staged_file.write_text("staged content", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=str(temp_git_repo), check=True)

    # Untracked file
    untracked_file = temp_git_repo / "untracked.txt"
    untracked_file.write_text("untracked content", encoding="utf-8")

    # 2. Take snapshot
    snapshot_id = tracker.take_snapshot()
    assert not snapshot_id.startswith("clean:")

    # Verify that files are still in the workspace after taking snapshot
    assert initial_file.read_text(encoding="utf-8") == "modified initial content"
    assert staged_file.read_text(encoding="utf-8") == "staged content"
    assert untracked_file.read_text(encoding="utf-8") == "untracked content"

    # 3. Mess up the workspace
    initial_file.write_text("different content altogether", encoding="utf-8")
    staged_file.write_text("overwritten staged content", encoding="utf-8")
    untracked_file.unlink()

    new_junk_file = temp_git_repo / "junk.txt"
    new_junk_file.write_text("junk content", encoding="utf-8")

    # 4. Restore the snapshot
    tracker.restore_snapshot(snapshot_id)

    # Verify everything rolled back to the snapshot state
    assert initial_file.read_text(encoding="utf-8") == "modified initial content"
    assert staged_file.read_text(encoding="utf-8") == "staged content"
    assert untracked_file.read_text(encoding="utf-8") == "untracked content"
    assert not new_junk_file.exists()


def test_git_worktree_sync(temp_git_repo: Path) -> None:
    """Test the synchronous context manager interface for GitWorktree."""
    with GitWorktree(temp_git_repo) as worktree_path:
        assert worktree_path.exists()
        assert (worktree_path / "initial.txt").exists()
        assert (worktree_path / "initial.txt").read_text(encoding="utf-8") == "initial content"

        # Make changes inside the worktree
        wt_file = worktree_path / "wt_only.txt"
        wt_file.write_text("worktree isolated file", encoding="utf-8")
        assert wt_file.exists()

        # Verify they don't affect original repo path
        assert not (temp_git_repo / "wt_only.txt").exists()

    # Verify cleanup on exit
    assert not worktree_path.exists()


@pytest.mark.asyncio
async def test_git_worktree_async(temp_git_repo: Path) -> None:
    """Test the asynchronous context manager interface for GitWorktree."""
    async with GitWorktree(temp_git_repo) as worktree_path:
        assert worktree_path.exists()
        assert (worktree_path / "initial.txt").exists()
        assert (worktree_path / "initial.txt").read_text(encoding="utf-8") == "initial content"

    # Verify cleanup on exit
    assert not worktree_path.exists()
