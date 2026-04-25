"""Orthogonal Role Orchestration.

This module decouples the "Job" (Role) from the "Agent" (ILLMBackend).
A Role defines what needs to be done and the constraints (DbC) required to do it.
A RolePlayer is the dynamic binding of a Role to an available backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from maxwell_daemon.backends.base import (
    BackendResponse,
    ILLMBackend,
    Message,
    MessageRole,
)


@dataclass(slots=True)
class Role:
    """Defines the job requirements orthogonally from the backend."""

    name: str
    system_prompt: str
    requires_tool_use: bool = False


@dataclass(slots=True)
class Job:
    """A specific task to be executed by a RolePlayer."""

    instructions: str
    context_data: dict[str, Any] = field(default_factory=dict)


class RolePlayer:
    """An orthogonal wrapper tying a specific Role to any ILLMBackend.

    It enforces Design by Contract (DbC) by ensuring the backend
    assigned to the role actually possesses the required capabilities.
    """

    def __init__(self, role: Role, backend: ILLMBackend, model: str) -> None:
        self.role = role
        self.backend = backend
        self.model = model
        self.last_messages: list[Message] = []

        # Enforce Design by Contract (DbC): Validate capabilities
        caps = self.backend.capabilities(model)
        if self.role.requires_tool_use and not caps.supports_tool_use:
            raise ValueError(
                f"Backend {self.backend.name} does not support required tool use for role {self.role.name}"
            )

    async def execute(self, job: Job, tools: list[dict[str, Any]] | None = None) -> BackendResponse:
        """Executes the job using the assigned backend, adhering to the role's constraints."""
        messages = [
            Message(role=MessageRole.SYSTEM, content=self.role.system_prompt),
            Message(role=MessageRole.USER, content=job.instructions),
        ]
        self.last_messages = messages

        return await self.backend.complete(
            messages=messages,
            model=self.model,
            tools=tools,
        )


class RoleOrchestrator:
    """Orchestrates jobs by finding the right backend for the role.

    This adheres to 'You don't have to choose'. The orchestrator
    examines the Role's requirements (DbC) and provisions a RolePlayer
    with a backend capable of fulfilling those requirements.
    """

    def __init__(self, router: Any) -> None:
        # We inject the BackendRouter to adhere to LOD.
        # The Orchestrator doesn't build backends, it just asks the router for them.
        self._router = router

    def assign_player(self, role: Role, repo: str | None = None) -> RolePlayer:
        """Dynamically binds a Role to the best available backend."""
        # Query the router for an available backend
        decision = self._router.route(repo=repo)

        # Enforce DbC: If the default backend can't fulfill the role,
        # we would ideally iterate through available backends. For now,
        # we fail fast if the contract is violated.
        try:
            return RolePlayer(role=role, backend=decision.backend, model=decision.model)
        except ValueError as e:
            raise RuntimeError(
                f"Orchestration failed: {e}. Please configure a compatible backend for this role."
            ) from e
