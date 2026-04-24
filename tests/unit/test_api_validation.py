"""Tests for API input validation utilities."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from maxwell_daemon.api.validation import (
    ModelField,
    PriorityField,
    PromptField,
    RepoField,
    TaskIdField,
)


class SampleAPIModel(BaseModel):
    """Sample model using validation fields."""

    repo: RepoField | None = None
    prompt: PromptField | None = None
    priority: PriorityField = 100
    task_id: TaskIdField | None = None
    model: ModelField | None = None


def test_valid_repo_field() -> None:
    """Test valid repository format."""
    model = SampleAPIModel(repo="my-org/my-repo")
    assert model.repo == "my-org/my-repo"

    model = SampleAPIModel(repo="owner/repo-name.ext")
    assert model.repo == "owner/repo-name.ext"


def test_invalid_repo_field() -> None:
    """Test invalid repository format."""
    with pytest.raises(ValidationError):
        SampleAPIModel(repo="invalid-repo")

    with pytest.raises(ValidationError):
        SampleAPIModel(repo="owner/")

    with pytest.raises(ValidationError):
        SampleAPIModel(repo="/repo")


def test_valid_prompt_field() -> None:
    """Test valid prompt."""
    model = SampleAPIModel(prompt="a")  # minimum length
    assert model.prompt == "a"

    model = SampleAPIModel(prompt="a" * 25000)  # within range
    assert len(model.prompt) == 25000


def test_invalid_prompt_field() -> None:
    """Test invalid prompt."""
    with pytest.raises(ValidationError):
        SampleAPIModel(prompt="")  # empty string (min length is 1)

    with pytest.raises(ValidationError):
        SampleAPIModel(prompt="a" * 50001)  # too long


def test_valid_priority_field() -> None:
    """Test valid priority."""
    model = SampleAPIModel(priority=0)  # emergency
    assert model.priority == 0

    model = SampleAPIModel(priority=50)  # high
    assert model.priority == 50

    model = SampleAPIModel(priority=100)  # normal
    assert model.priority == 100

    model = SampleAPIModel(priority=200)  # batch
    assert model.priority == 200


def test_invalid_priority_field() -> None:
    """Test invalid priority."""
    with pytest.raises(ValidationError):
        SampleAPIModel(priority=-1)

    with pytest.raises(ValidationError):
        SampleAPIModel(priority=201)


def test_valid_task_id_field() -> None:
    """Test valid task ID."""
    model = SampleAPIModel(task_id="a")  # single char
    assert model.task_id == "a"

    model = SampleAPIModel(task_id="task-123-abc")
    assert model.task_id == "task-123-abc"

    model = SampleAPIModel(task_id="a" * 256)  # max length
    assert len(model.task_id) == 256


def test_invalid_task_id_field() -> None:
    """Test invalid task ID."""
    with pytest.raises(ValidationError):
        SampleAPIModel(task_id="task_id")  # underscore not allowed

    with pytest.raises(ValidationError):
        SampleAPIModel(task_id="task id")  # space not allowed

    with pytest.raises(ValidationError):
        SampleAPIModel(task_id="a" * 257)  # too long


def test_valid_model_field() -> None:
    """Test valid model names."""
    model = SampleAPIModel(model="claude-opus-4-7")
    assert model.model == "claude-opus-4-7"

    model = SampleAPIModel(model="gpt-4o")
    assert model.model == "gpt-4o"

    model = SampleAPIModel(model="ollama:llama2")
    assert model.model == "ollama:llama2"

    model = SampleAPIModel(model="custom_model-1.5")
    assert model.model == "custom_model-1.5"


def test_invalid_model_field() -> None:
    """Test invalid model names."""
    with pytest.raises(ValidationError):
        SampleAPIModel(model="model with spaces")

    with pytest.raises(ValidationError):
        SampleAPIModel(model="a" * 129)  # too long
