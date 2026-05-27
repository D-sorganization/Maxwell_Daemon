"""Git Snapshot and Worktree Safety Gates."""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class GitTracker:
    """Git repository snapshot and rollback utility."""

    def __init__(self, repo_path: Path | str) -> None:
        """Initialize the Git tracker for a specific repository path."""
        self.repo_path = Path(repo_path).expanduser().resolve()

    def _run_git(self, args: list[str]) -> str:
        """Run a git command in the repository directory and return the trimmed stdout."""
        cmd = ["git", *args]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                check=True,
                encoding="utf-8",
                errors="replace",
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.error(
                "Git command failed: %s, stderr: %s, stdout: %s",
                " ".join(cmd),
                e.stderr.strip(),
                e.stdout.strip(),
            )
            raise e

    def take_snapshot(self) -> str:
        """Saves current dirty working directory and untracked files.

        Returns:
            str: A snapshot identifier (commit SHA or 'clean:<head_sha>')
        """
        try:
            head_sha = self._run_git(["rev-parse", "HEAD"])
        except subprocess.CalledProcessError:
            head_sha = ""

        # Check if there are any changes (staged, unstaged, or untracked)
        status_out = self._run_git(["status", "--porcelain"])
        if not status_out.strip():
            logger.info("No dirty changes to snapshot. Returning clean HEAD.")
            return f"clean:{head_sha}"

        # Write current index tree
        original_index_tree = self._run_git(["write-tree"])

        # Stage everything including untracked files
        self._run_git(["add", "-A"])

        # Write snapshot tree
        snapshot_tree = self._run_git(["write-tree"])

        # Create commit object representing the snapshot
        commit_msg = f"maxwell_snapshot_{uuid.uuid4()}"
        commit_args = ["commit-tree", snapshot_tree, "-m", commit_msg]
        if head_sha:
            commit_args.extend(["-p", head_sha])

        snapshot_commit_sha = self._run_git(commit_args)

        # Restore original index tree to keep workspace index state intact
        self._run_git(["read-tree", original_index_tree])

        logger.info("Created git snapshot: %s", snapshot_commit_sha)
        return snapshot_commit_sha

    def restore_snapshot(self, snapshot_id: str) -> None:
        """Restores repository state to a previously saved snapshot.

        Args:
            snapshot_id (str): The snapshot identifier returned by take_snapshot().
        """
        if snapshot_id.startswith("clean:"):
            head_sha = snapshot_id.split(":", 1)[1]
            if head_sha:
                self._run_git(["reset", "--hard", head_sha])
            else:
                self._run_git(["reset", "--hard"])
            self._run_git(["clean", "-fd"])
            logger.info("Restored clean snapshot to HEAD: %s", head_sha)
            return

        # Commit-based snapshot:
        # Find parent commit SHA of the snapshot commit
        try:
            parents = self._run_git(["log", "-1", "--format=%P", snapshot_id]).strip()
            parent_sha = parents.split()[0] if parents else ""
        except subprocess.CalledProcessError:
            parent_sha = ""

        # Hard reset and clean working directory to base commit (or HEAD if no parent)
        if parent_sha:
            self._run_git(["reset", "--hard", parent_sha])
        else:
            self._run_git(["reset", "--hard"])
        self._run_git(["clean", "-fd"])

        # Apply snapshot changes back to working tree via read-tree -u
        try:
            self._run_git(["read-tree", "--reset", "-u", snapshot_id])
            self._run_git(["reset"])  # Unstage changes so they appear as dirty/uncommitted
            logger.info("Restored git snapshot: %s", snapshot_id)
        except Exception as e:
            logger.error("Failed to apply snapshot changes via read-tree: %s", str(e))
            raise e


class GitWorktree:
    """Context manager for running sandbox commands in an isolated Git worktree."""

    def __init__(self, repo_path: Path | str, commit_ish: str = "HEAD") -> None:
        """Initialize GitWorktree with repository path and the commit/branch to base on."""
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.commit_ish = commit_ish
        self.worktree_path: Path | None = None
        self._temp_dir: str | None = None

    def __enter__(self) -> Path:
        self._temp_dir = tempfile.mkdtemp(prefix="maxwell_wt_")
        self.worktree_path = Path(self._temp_dir)

        # Create detached worktree at worktree_path
        cmd = ["git", "worktree", "add", "--detach", str(self.worktree_path), self.commit_ish]
        try:
            subprocess.run(
                cmd,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                check=True,
                encoding="utf-8",
                errors="replace",
            )
            logger.info(
                "Created temporary Git worktree at %s (base: %s)",
                self.worktree_path,
                self.commit_ish,
            )
            return self.worktree_path
        except (subprocess.SubprocessError, OSError) as e:
            self._cleanup_dir()
            logger.error("Failed to create Git worktree: %s", str(e))
            raise e

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.worktree_path:
            # Force remove the worktree from Git tracking
            cmd = ["git", "worktree", "remove", "--force", str(self.worktree_path)]
            try:
                subprocess.run(
                    cmd,
                    cwd=str(self.repo_path),
                    capture_output=True,
                    text=True,
                    check=True,
                    encoding="utf-8",
                    errors="replace",
                )
                logger.info("Removed Git worktree tracking for %s", self.worktree_path)
            except (subprocess.SubprocessError, OSError) as e:
                logger.warning("Failed to remove Git worktree via git worktree remove: %s", str(e))

            # Prune to clean up any metadata leftovers
            with contextlib.suppress(subprocess.SubprocessError, OSError):
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(self.repo_path),
                    capture_output=True,
                    check=True,
                )

            # Manually clean up directories and lock files
            self._cleanup_dir()

    def _cleanup_dir(self) -> None:
        if self.worktree_path and self.worktree_path.exists():
            try:
                shutil.rmtree(self.worktree_path, ignore_errors=True)
            except OSError as e:
                logger.warning("Failed to remove worktree path %s: %s", self.worktree_path, str(e))

    async def __aenter__(self) -> Path:
        import asyncio

        return await asyncio.to_thread(self.__enter__)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        import asyncio

        await asyncio.to_thread(self.__exit__, exc_type, exc_val, exc_tb)
