"""Interactive chat endpoints.

The dashboard uses this surface for lightweight codebase Q&A.  When a
workspace root is supplied, the request is routed through the normal backend
router while binding agent-loop tools to that workspace and seeding a small
codebase context block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from maxwell_daemon.backends.base import Message, MessageRole
from maxwell_daemon.context.packs import ContextPackPolicy, build_context_pack
from maxwell_daemon.daemon import Daemon

__all__ = ["ChatRequest", "ChatResponse", "register"]


_CODEBASE_SYSTEM_PROMPT = (
    "You are Maxwell's codebase Q&A assistant. Answer questions using the "
    "selected workspace as the source of truth. Prefer concrete file paths, "
    "functions, commands, and caveats over broad summaries. Use the available "
    "read-only repository tools before making claims that depend on current code."
)


class ChatMessage(BaseModel):
    role: str = Field(..., pattern=r"^(system|user|assistant|tool)$")
    content: str = Field(..., min_length=1, max_length=100_000)


class ChatRequest(BaseModel):
    # ``extra="forbid"`` so unsupported fields are rejected with a 422 instead
    # of silently stripped. Conversation history is carried in ``messages[]``
    # (honored end-to-end via ``_messages_for``); a consumer sending a bare
    # ``history`` or ``stream`` field now fails loudly and must migrate to
    # ``messages[]`` (#995).
    model_config = ConfigDict(extra="forbid")

    prompt: str | None = Field(default=None, min_length=1, max_length=100_000)
    message: str | None = Field(default=None, min_length=1, max_length=100_000)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=50)
    workspace: str | None = Field(default=None, min_length=1, max_length=1000)
    repo_root: str | None = Field(default=None, min_length=1, max_length=1000)
    repo: str | None = Field(default=None, min_length=1, max_length=200)
    backend: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    max_tokens: int | None = Field(default=None, ge=1, le=65_536)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    codebase: bool = True

    @field_validator("messages")
    @classmethod
    def _require_user_content(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        if messages and not any(message.role == "user" for message in messages):
            raise ValueError("messages must include at least one user message")
        return messages


class ChatResponse(BaseModel):
    content: str
    backend: str
    backend_name: str
    model: str
    finish_reason: str
    route_reason: str
    workspace: str | None = None
    usage: dict[str, int]


def register(
    app: FastAPI,
    daemon: Daemon,
    require_operator: Any,
) -> None:
    """Attach chat endpoints to ``app``."""

    async def _handle_chat(payload: ChatRequest) -> ChatResponse:
        workspace = _resolve_workspace(payload.workspace or payload.repo_root)
        messages = await _messages_for(payload, workspace=workspace)
        decision = daemon._router.route(
            repo=payload.repo,
            backend_override=payload.backend,
            model_override=payload.model,
        )
        response = await decision.backend.complete(
            messages,
            model=decision.model,
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
            workspace_dir=str(workspace) if workspace is not None else None,
            repo=payload.repo,
        )
        return ChatResponse(
            content=response.content,
            backend=response.backend,
            backend_name=decision.backend_name,
            model=response.model,
            finish_reason=response.finish_reason,
            route_reason=decision.reason,
            workspace=str(workspace) if workspace is not None else None,
            usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "cached_tokens": response.usage.cached_tokens,
            },
        )

    @app.post("/api/chat", dependencies=[Depends(require_operator)])
    async def chat(payload: ChatRequest) -> ChatResponse:
        return await _handle_chat(payload)

    @app.post("/api/chat/codebase", dependencies=[Depends(require_operator)])
    async def codebase_chat(payload: ChatRequest) -> ChatResponse:
        payload.codebase = True
        return await _handle_chat(payload)


def _resolve_workspace(raw: str | None) -> Path | None:
    if raw is None:
        return None
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"workspace root does not exist: {raw}")
    return path


async def _messages_for(payload: ChatRequest, *, workspace: Path | None) -> list[Message]:
    messages = [
        Message(role=MessageRole(message.role), content=message.content)
        for message in payload.messages
    ]
    text = payload.prompt or payload.message
    if text is not None:
        messages.append(Message(role=MessageRole.USER, content=text))
    if not messages:
        raise HTTPException(status_code=422, detail="prompt, message, or messages is required")

    if payload.codebase and workspace is not None:
        context = await _codebase_context(workspace, query=text or messages[-1].content)
        messages.insert(
            0,
            Message(
                role=MessageRole.SYSTEM,
                content=f"{_CODEBASE_SYSTEM_PROMPT}\n\n{context}",
            ),
        )
    return messages


async def _codebase_context(workspace: Path, *, query: str) -> str:
    pack = await build_context_pack(
        workspace,
        query=query,
        policy=ContextPackPolicy(
            max_file_bytes=16 * 1024,
            max_total_bytes=64 * 1024,
            provider_budget_chars=6_000,
        ),
    )
    return (
        "Selected workspace:\n"
        f"{workspace}\n\n"
        "Context pack manifest:\n"
        f"{pack.stable_manifest_json(include_text=False)}"
    )
