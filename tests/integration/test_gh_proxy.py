import json
from pathlib import Path

import pytest

from maxwell_daemon.gh.client import GitHubClient
from maxwell_daemon.tools.builtins import build_default_registry


@pytest.mark.asyncio
async def test_gh_proxy_allowlist_rejection(tmp_path: Path) -> None:
    """Agent requesting a non-allowlisted operation gets a structured failure response."""
    gh_client = GitHubClient()
    registry = build_default_registry(
        tmp_path,
        gh_client=gh_client,
        gh_allowed_operations=frozenset({"comment"}),
    )

    result = await registry.invoke(
        "gh_proxy",
        {
            "operation": "delete_repo",
            "params_json": json.dumps({"repo": "foo/bar"}),
        },
    )

    # The session continues because it's a normal string return,
    # but the content contains the rejection message.
    assert "not in the allowlist" in result.content
    assert "delete_repo" in result.content
