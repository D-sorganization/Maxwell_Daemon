"""Remote memory manager for worker nodes.

Routes memory operations to the coordinator's HTTP API.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import httpx

from maxwell_daemon.memory import MemoryManager, ScratchPad

T = TypeVar("T")


def _run_from_sync(factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    raise RuntimeError(
        "RemoteMemoryManager synchronous methods cannot run inside an active "
        "event loop; use the async methods instead."
    )


class RemoteMemoryManager(MemoryManager):
    def __init__(self, coordinator_url: str, auth_token: str | None = None) -> None:
        self._url = coordinator_url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if auth_token:
            self._headers["Authorization"] = f"Bearer {auth_token}"
        self.scratchpad = ScratchPad()

    def assemble_context(
        self,
        *,
        repo: str,
        issue_title: str,
        issue_body: str,
        task_id: str,
        max_chars: int = 8000,
    ) -> str:
        return _run_from_sync(
            lambda: self.assemble_context_async(
                repo=repo,
                issue_title=issue_title,
                issue_body=issue_body,
                task_id=task_id,
                max_chars=max_chars,
            )
        )

    async def assemble_context_async(
        self,
        *,
        repo: str,
        issue_title: str,
        issue_body: str,
        task_id: str,
        max_chars: int = 8000,
    ) -> str:
        base_context = ""
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self._url}/api/v1/memory/assemble",
                    json={
                        "repo": repo,
                        "issue_title": issue_title,
                        "issue_body": issue_body,
                        "task_id": task_id,
                        "max_chars": max_chars,
                    },
                    headers=self._headers,
                    timeout=10.0,
                )
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict):
                    context = payload.get("context")
                    if isinstance(context, str):
                        base_context = context
            except (httpx.HTTPError, ValueError, TypeError):
                base_context = ""

        # Merge local scratchpad
        scratch_text = self.scratchpad.render(task_id, max_chars=max_chars // 4)
        if scratch_text:
            return f"{base_context}\n\n## Scratchpad (this task's history)\n\n{scratch_text}"
        return base_context

    def record_outcome(
        self,
        *,
        task_id: str,
        repo: str,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        plan: str,
        applied_diff: bool,
        pr_url: str,
        outcome: str,
    ) -> None:
        _run_from_sync(
            lambda: self.record_outcome_async(
                task_id=task_id,
                repo=repo,
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                plan=plan,
                applied_diff=applied_diff,
                pr_url=pr_url,
                outcome=outcome,
            )
        )

    async def record_outcome_async(
        self,
        *,
        task_id: str,
        repo: str,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        plan: str,
        applied_diff: bool,
        pr_url: str,
        outcome: str,
    ) -> None:
        async with httpx.AsyncClient() as client:
            with contextlib.suppress(httpx.HTTPError):
                resp = await client.post(
                    f"{self._url}/api/v1/memory/record",
                    json={
                        "task_id": task_id,
                        "repo": repo,
                        "issue_number": issue_number,
                        "issue_title": issue_title,
                        "issue_body": issue_body,
                        "plan": plan,
                        "applied_diff": applied_diff,
                        "pr_url": pr_url,
                        "outcome": outcome,
                    },
                    headers=self._headers,
                    timeout=10.0,
                )
                resp.raise_for_status()
        self.scratchpad.clear(task_id)
