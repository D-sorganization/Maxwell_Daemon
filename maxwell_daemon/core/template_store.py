"""Task Template Store for Maxwell Daemon.

Provides parameterised, repeatable task workflows (Issue #492).
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from maxwell_daemon.logging import get_logger

log = get_logger(__name__)


class ParameterType(str, Enum):
    STRING = "string"
    REPO = "repo"
    FILE = "file"
    ENUM = "enum"
    DATE = "date"


class TemplateParameter(BaseModel):
    name: str
    type: ParameterType = ParameterType.STRING
    description: str | None = None
    required: bool = True
    default: Any | None = None
    options: list[str] | None = None  # For ENUM type


class TaskTemplate(BaseModel):
    id: str
    name: str
    description: str
    prompt_template: str
    parameters: list[TemplateParameter] = Field(default_factory=list)
    default_backend: str | None = None
    default_policy: str | None = None
    tags: list[str] = Field(default_factory=list)

    def render(self, kwargs: dict[str, Any]) -> str:
        """Render the prompt_template using Jinja2."""
        # Optional jinja2 import to avoid hard dependency if not needed everywhere
        try:
            from jinja2 import Template
        except ImportError:
            # Fallback to simple format if jinja2 is missing
            return self.prompt_template.format(**kwargs)

        t = Template(self.prompt_template)
        rendered: str = t.render(**kwargs)
        return rendered


class TemplateStore:
    """Store for predefined and user-customised task templates."""

    def __init__(self, templates_path: Path | None = None) -> None:
        self._path = templates_path
        self._templates: dict[str, TaskTemplate] = {}
        self._load_builtins()
        if self._path and self._path.exists():
            self._load_from_disk()

    def _load_builtins(self) -> None:
        """Load opinionated built-in templates."""
        builtins = [
            # 1. Audits
            TaskTemplate(
                id="audit-repo-todos",
                name="Audit Repo TODOs",
                description="Scan a repository for TODO comments and summarize them.",
                prompt_template="Scan the repository {{ repo }} for TODOs and FIXMEs. Group them by file and suggest a priority order for addressing them.",
                parameters=[TemplateParameter(name="repo", type=ParameterType.REPO, required=True)],
                tags=["audit", "cleanup"],
            ),
            TaskTemplate(
                id="audit-code-duplication",
                name="Audit Code Duplication",
                description="Find redundant code or copy-paste duplication.",
                prompt_template="Analyze {{ target_dir }} for duplicated code blocks. Provide a report of the top 5 largest duplicated sections and suggest refactoring strategies to apply DRY principles.",
                parameters=[
                    TemplateParameter(
                        name="target_dir",
                        type=ParameterType.STRING,
                        required=True,
                        description="Directory to scan",
                    )
                ],
                tags=["audit", "refactor"],
            ),
            TaskTemplate(
                id="audit-dead-code",
                name="Audit Dead Code",
                description="Identify unused functions, classes, and variables.",
                prompt_template="Scan the Python files in {{ target_dir }} for dead code (unused imports, uncalled functions, inaccessible classes). Produce a summary of what can be safely deleted.",
                parameters=[
                    TemplateParameter(name="target_dir", type=ParameterType.STRING, required=True)
                ],
                tags=["audit", "cleanup"],
            ),
            # 2. Bug Triage
            TaskTemplate(
                id="triage-bug-report",
                name="Triage Bug Report",
                description="Investigate a specific bug report and locate the root cause.",
                prompt_template="Investigate bug report #{{ issue_number }}. Review the traceback and the code in {{ repo }}. Identify the root cause and propose a step-by-step fix.",
                parameters=[
                    TemplateParameter(
                        name="issue_number", type=ParameterType.STRING, required=True
                    ),
                    TemplateParameter(name="repo", type=ParameterType.REPO, required=True),
                ],
                tags=["bug", "triage"],
            ),
            TaskTemplate(
                id="reproduce-bug",
                name="Write Regression Test",
                description="Write a test case to reproduce a reported bug.",
                prompt_template="Write a failing test case in {{ test_file }} that reproduces the behavior described in bug report #{{ issue_number }}.",
                parameters=[
                    TemplateParameter(
                        name="issue_number", type=ParameterType.STRING, required=True
                    ),
                    TemplateParameter(name="test_file", type=ParameterType.FILE, required=True),
                ],
                tags=["bug", "testing"],
            ),
            # 3. Documentation & Release
            TaskTemplate(
                id="write-release-notes",
                name="Write Release Notes",
                description="Generate release notes from recent git commits.",
                prompt_template="Write release notes for {{ repo }} comparing the current state against {{ previous_tag }}. Focus on user-facing features, bug fixes, and breaking changes. Format in markdown.",
                parameters=[
                    TemplateParameter(name="repo", type=ParameterType.REPO, required=True),
                    TemplateParameter(
                        name="previous_tag",
                        type=ParameterType.STRING,
                        required=True,
                        description="e.g. v1.2.0",
                    ),
                ],
                tags=["release", "docs"],
            ),
            TaskTemplate(
                id="update-changelog",
                name="Update CHANGELOG.md",
                description="Append recent changes to the repository's CHANGELOG.md.",
                prompt_template="Read the git log of {{ repo }} since {{ previous_tag }} and append a new section to CHANGELOG.md following the Keep a Changelog format.",
                parameters=[
                    TemplateParameter(name="repo", type=ParameterType.REPO, required=True),
                    TemplateParameter(
                        name="previous_tag", type=ParameterType.STRING, required=True
                    ),
                ],
                tags=["release", "docs"],
            ),
            TaskTemplate(
                id="docstring-backfill",
                name="Backfill Docstrings",
                description="Add missing docstrings to functions and classes in a file.",
                prompt_template="Review {{ file_path }}. Add PEP 257 compliant docstrings to all functions and classes that are missing them. Do not change any logic.",
                parameters=[
                    TemplateParameter(name="file_path", type=ParameterType.FILE, required=True)
                ],
                tags=["docs", "refactor"],
            ),
            TaskTemplate(
                id="readme-refresh",
                name="Refresh README",
                description="Update the README to reflect current project features.",
                prompt_template="Review the codebase in {{ repo }} and update the README.md to ensure the feature list, setup instructions, and usage examples are accurate.",
                parameters=[TemplateParameter(name="repo", type=ParameterType.REPO, required=True)],
                tags=["docs"],
            ),
            # 4. Code Review & PRs
            TaskTemplate(
                id="pr-review",
                name="Review Pull Request",
                description="Perform a comprehensive code review on a PR.",
                prompt_template="Review the changes in PR #{{ pr_number }} for {{ repo }}. Check for logic errors, security issues, performance bottlenecks, and style violations. Produce a consolidated review.",
                parameters=[
                    TemplateParameter(name="repo", type=ParameterType.REPO, required=True),
                    TemplateParameter(name="pr_number", type=ParameterType.STRING, required=True),
                ],
                tags=["review", "pr"],
            ),
            TaskTemplate(
                id="pr-create-from-issue",
                name="Implement Issue & Open PR",
                description="Implement a fix for an issue and open a PR.",
                prompt_template="Read issue #{{ issue_number }} in {{ repo }}. Create a new branch, implement the requested fix or feature, ensure tests pass, and create a Pull Request resolving the issue.",
                parameters=[
                    TemplateParameter(name="repo", type=ParameterType.REPO, required=True),
                    TemplateParameter(
                        name="issue_number", type=ParameterType.STRING, required=True
                    ),
                ],
                tags=["feature", "pr"],
            ),
            # 5. Testing
            TaskTemplate(
                id="generate-unit-tests",
                name="Generate Unit Tests",
                description="Write unit tests for a specific module.",
                prompt_template="Write comprehensive pytest unit tests for the functions in {{ file_path }}. Place the tests in {{ test_path }} and aim for high coverage of edge cases.",
                parameters=[
                    TemplateParameter(name="file_path", type=ParameterType.FILE, required=True),
                    TemplateParameter(name="test_path", type=ParameterType.FILE, required=True),
                ],
                tags=["testing"],
            ),
            TaskTemplate(
                id="fix-failing-tests",
                name="Fix Failing Tests",
                description="Diagnose and fix tests that are currently failing.",
                prompt_template="Run the test suite in {{ target_dir }}. Identify any failing tests, diagnose the root cause (whether the code is broken or the test is outdated), and apply the necessary fixes.",
                parameters=[
                    TemplateParameter(name="target_dir", type=ParameterType.STRING, required=True)
                ],
                tags=["testing", "fix"],
            ),
            # 6. Refactoring
            TaskTemplate(
                id="refactor-to-async",
                name="Refactor to Async/Await",
                description="Convert synchronous code to asyncio.",
                prompt_template="Refactor {{ file_path }} to use Python's async/await where appropriate (e.g., I/O bound operations). Ensure backwards compatibility or update callers accordingly.",
                parameters=[
                    TemplateParameter(name="file_path", type=ParameterType.FILE, required=True)
                ],
                tags=["refactor", "async"],
            ),
            TaskTemplate(
                id="type-hint-backfill",
                name="Backfill Type Hints",
                description="Add Python type hints to a file and run mypy.",
                prompt_template="Add strict Python type hints to all function signatures and complex variables in {{ file_path }}. Run mypy to ensure there are no typing errors.",
                parameters=[
                    TemplateParameter(name="file_path", type=ParameterType.FILE, required=True)
                ],
                tags=["refactor", "typing"],
            ),
            # 7. Security & Maintenance
            TaskTemplate(
                id="security-scan",
                name="Security Audit",
                description="Audit code for common security vulnerabilities.",
                prompt_template="Perform a security audit on {{ target_dir }}. Look for SQL injection, hardcoded secrets, XSS vulnerabilities, and unsafe file I/O. Provide a remediation report.",
                parameters=[
                    TemplateParameter(name="target_dir", type=ParameterType.STRING, required=True)
                ],
                tags=["security", "audit"],
            ),
            TaskTemplate(
                id="dep-upgrade",
                name="Upgrade Dependencies",
                description="Update outdated dependencies in pyproject.toml or requirements.txt.",
                prompt_template="Check the dependencies defined in {{ file_path }} for newer secure versions. Update the file and ensure that `pip install` works without resolution conflicts.",
                parameters=[
                    TemplateParameter(name="file_path", type=ParameterType.FILE, required=True)
                ],
                tags=["maintenance", "deps"],
            ),
            # 8. Performance
            TaskTemplate(
                id="performance-profile",
                name="Performance Profile Analysis",
                description="Analyze code for performance bottlenecks.",
                prompt_template="Analyze {{ file_path }} for computational or I/O bottlenecks. Suggest and implement optimizations to reduce time complexity or unnecessary memory allocations.",
                parameters=[
                    TemplateParameter(name="file_path", type=ParameterType.FILE, required=True)
                ],
                tags=["performance", "optimization"],
            ),
            TaskTemplate(
                id="sql-query-optimization",
                name="Optimize SQL Queries",
                description="Review and optimize database queries.",
                prompt_template="Review the SQL queries or ORM calls in {{ file_path }}. Suggest indexes, N+1 query fixes, or query rewrites to improve execution speed.",
                parameters=[
                    TemplateParameter(name="file_path", type=ParameterType.FILE, required=True)
                ],
                tags=["performance", "database"],
            ),
            # 9. Miscellaneous / Scaffolding
            TaskTemplate(
                id="scaffold-cli",
                name="Scaffold CLI Command",
                description="Create a new CLI entry point using Typer.",
                prompt_template="Create a new Typer CLI command in {{ file_path }} named `{{ command_name }}`. It should accept options for `verbose` and `dry-run`, and include basic docstrings.",
                parameters=[
                    TemplateParameter(name="file_path", type=ParameterType.FILE, required=True),
                    TemplateParameter(
                        name="command_name", type=ParameterType.STRING, required=True
                    ),
                ],
                tags=["scaffold", "cli"],
            ),
            TaskTemplate(
                id="scaffold-fastapi-route",
                name="Scaffold FastAPI Route",
                description="Create a new API route in a FastAPI app.",
                prompt_template="Add a new `{{ method }}` route at `{{ route_path }}` to the FastAPI router in {{ file_path }}. Include a basic Pydantic model for request/response.",
                parameters=[
                    TemplateParameter(name="file_path", type=ParameterType.FILE, required=True),
                    TemplateParameter(
                        name="route_path",
                        type=ParameterType.STRING,
                        required=True,
                        description="/api/v1/resource",
                    ),
                    TemplateParameter(
                        name="method",
                        type=ParameterType.ENUM,
                        required=True,
                        options=["GET", "POST", "PUT", "DELETE"],
                        default="GET",
                    ),
                ],
                tags=["scaffold", "api"],
            ),
        ]
        for t in builtins:
            self._templates[t.id] = t

    def _load_from_disk(self) -> None:
        """Load user templates from disk."""
        if not self._path or not self._path.exists():
            return

        for child in self._path.glob("*.json"):
            if not child.is_file():
                continue
            try:
                data = json.loads(child.read_text("utf-8"))
                template = TaskTemplate.model_validate(data)
                self._templates[template.id] = template
            except Exception:
                log.warning("Failed to load template from %s", child.name, exc_info=True)

    def list_templates(self) -> list[TaskTemplate]:
        return list(self._templates.values())

    def get_template(self, template_id: str) -> TaskTemplate | None:
        return self._templates.get(template_id)
