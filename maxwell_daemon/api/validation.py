"""Input validation utilities for REST API endpoints.

Provides reusable Pydantic field types for common input patterns:
- Repository format (owner/repo)
- Task IDs
- Priorities
- Model names
- Prompts

These ensure consistent validation across all API endpoints.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

__all__ = [
    "MODEL_NAME_PATTERN",
    "REPO_PATTERN",
    "TASK_ID_PATTERN",
    "ModelField",
    "PriorityField",
    "PromptField",
    "RepoField",
    "TaskIdField",
]

# Reusable regex patterns for validation
REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
TASK_ID_PATTERN = r"^[A-Za-z0-9-]{1,256}$"
MODEL_NAME_PATTERN = r"^[A-Za-z0-9_:.-]+$"

# Reusable Pydantic Field configurations for consistent API input validation
RepoField = Annotated[
    str,
    StringConstraints(
        pattern=REPO_PATTERN,
        max_length=100,
    ),
    Field(description="Repository in owner/repo format (e.g., 'my-org/my-repo')"),
]

RoutingKeyField = Annotated[
    str,
    StringConstraints(max_length=100),
    Field(description="Generic routing key or repository identifier"),
]

PromptField = Annotated[
    str,
    StringConstraints(min_length=1, max_length=500000),
    Field(description="Prompt text must be between 1 and 500,000 characters"),
]

PriorityField = Annotated[
    int,
    Field(
        ge=0,
        le=200,
        description="Priority (0=emergency, 50=high, 100=normal, 200=batch)",
    ),
]

TaskIdField = Annotated[
    str,
    StringConstraints(pattern=TASK_ID_PATTERN),
    Field(description="Task ID: alphanumeric and dashes, max 256 chars"),
]

ModelField = Annotated[
    str,
    StringConstraints(pattern=MODEL_NAME_PATTERN, max_length=128),
    Field(
        description="Model name (e.g., 'claude-opus-4-7', 'gpt-4o', 'ollama:llama2')"
    ),
]
