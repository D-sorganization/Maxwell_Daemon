from typing import Annotated

from pydantic import Field

# Reusable regex patterns
REPO_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
TASK_ID_PATTERN = r"^[A-Za-z0-9-]+$"

# Reusable Pydantic Field configurations
RepoField = Annotated[
    str,
    Field(pattern=REPO_PATTERN, max_length=100, description="Repository in owner/repo format")
]

PromptField = Annotated[
    str,
    Field(min_length=10, max_length=50000, description="Prompt text must be between 10 and 50,000 characters")
]

PriorityField = Annotated[
    int,
    Field(ge=0, le=200, description="Priority must be between 0 and 200")
]

TaskIdField = Annotated[
    str,
    Field(pattern=TASK_ID_PATTERN, max_length=256, description="Task ID must be alphanumeric and dashes only")
]
