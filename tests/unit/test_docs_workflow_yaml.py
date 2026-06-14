from __future__ import annotations

from pathlib import Path

import yaml


def test_release_docs_build_does_not_require_pages_deploy() -> None:
    docs_workflow = yaml.safe_load(Path(".github/workflows/docs.yml").read_text())
    jobs = docs_workflow["jobs"]

    upload_step = next(
        step for step in jobs["build"]["steps"] if step.get("name") == "Upload Pages artifact"
    )

    assert upload_step["if"] == "github.event_name == 'workflow_dispatch'"
    assert jobs["deploy"]["if"] == "github.event_name == 'workflow_dispatch'"
