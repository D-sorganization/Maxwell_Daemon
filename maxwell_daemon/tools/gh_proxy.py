"""Built-in ``gh_proxy`` tool — agents call GitHub through the daemon, never hold tokens.

Mirrors the Symphony §10.5 ``linear_graphql`` pattern: the runtime holds the
credential, the agent sees only a structured tool, and every call is written to
the audit ledger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from maxwell_daemon.audit import AuditLogger
from maxwell_daemon.gh import GitHubClient
from maxwell_daemon.logging import get_logger
from maxwell_daemon.tools.mcp import ToolParam, mcp_tool

log = get_logger("maxwell_daemon.tools.gh_proxy")

# Operations that an agent may request.  Keys are the ``operation`` field in the
# tool call payload; values describe which GitHubClient method is used.
#
# Each entry maps to:
#   method:  the async method name on GitHubClient
#   params:  required parameter names for that operation
#   returns: human-readable description of the return shape
_GH_PROXY_OPERATIONS: dict[str, dict[str, Any]] = {
    "create_pr": {
        "method": "create_pull_request",
        "params": ["repo", "head", "base", "title", "body"],
        "optional": ["draft"],
        "returns": "pr_url",
    },
    "create_issue": {
        "method": "create_issue",
        "params": ["repo", "title", "body"],
        "optional": ["labels"],
        "returns": "issue_url",
    },
    "comment": {
        "method": "create_comment",
        "params": ["repo", "issue_number", "body"],
        "returns": "comment_url",
    },
    "set_state": {
        "method": "set_issue_state",
        "params": ["repo", "issue_number", "state"],
        "returns": "issue_url",
    },
}


class GhProxyError(ValueError):
    """Raised when the gh_proxy tool receives an invalid operation or params."""


@dataclass(frozen=True, slots=True)
class GhProxyAllowlist:
    """Per-task allowlist for gh_proxy operations."""

    operations: frozenset[str]

    @classmethod
    def default(cls) -> GhProxyAllowlist:
        """Default allowlist: PR and comment operations only."""
        return cls(frozenset({"create_pr", "create_issue", "comment"}))

    def allowed(self, operation: str) -> bool:
        return operation in self.operations


def _validate_params(operation: str, params: dict[str, Any]) -> None:
    """Ensure all required params are present and no unknown params leak."""
    meta = _GH_PROXY_OPERATIONS.get(operation)
    if meta is None:
        raise GhProxyError(
            f"unknown operation {operation!r}; supported: {sorted(_GH_PROXY_OPERATIONS)}"
        )

    required = set(meta["params"])
    optional = set(meta.get("optional", []))
    present = set(params)

    missing = required - present
    if missing:
        raise GhProxyError(f"operation {operation!r} missing required params: {sorted(missing)}")

    unknown = present - required - optional
    if unknown:
        raise GhProxyError(f"operation {operation!r} received unknown params: {sorted(unknown)}")


async def _call_gh(
    client: GitHubClient,
    operation: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch to the underlying GitHubClient method."""
    meta = _GH_PROXY_OPERATIONS[operation]
    method_name = meta["method"]

    # GitHubClient may not yet implement every operation; guard gracefully.
    method = getattr(client, method_name, None)
    if method is None:
        raise GhProxyError(
            f"operation {operation!r} is declared but not yet implemented "
            f"(missing {method_name} on GitHubClient)"
        )

    result = await method(**params)

    # Normalise the result to a plain dict for the agent.
    if hasattr(result, "url"):
        return {"success": True, "url": result.url}
    if hasattr(result, "number"):
        return {"success": True, "number": result.number}
    if isinstance(result, str):
        return {"success": True, "url": result}
    return {"success": True, "result": str(result)}


def make_gh_proxy(
    *,
    client: GitHubClient,
    allowlist: GhProxyAllowlist | None = None,
    audit: AuditLogger | None = None,
    task_id: str | None = None,
) -> Any:
    """Factory returning the ``gh_proxy`` tool function bound to a GitHubClient.

    :param client: The GitHubClient instance that holds the live token.
    :param allowlist: Set of permitted operations for this task.  When ``None``
        the :meth:`GhProxyAllowlist.default` is used.
    :param audit: Optional audit logger for append-only ledger entries.
    :param task_id: Current task id, included in audit entries.
    """
    whitelist = allowlist or GhProxyAllowlist.default()

    @mcp_tool(
        name="gh_proxy",
        description=(
            "Execute a GitHub operation through the daemon's authenticated client. "
            "The agent never sees the GitHub token.  Supported operations: "
            f"{sorted(_GH_PROXY_OPERATIONS)}. "
            "Each call is logged to the audit ledger."
        ),
        capabilities=frozenset({"github_read", "github_write"}),
        risk_level="network_write",
        requires_approval=True,
        params=[
            ToolParam(
                name="operation",
                type="string",
                description="Operation name",
            ),
            ToolParam(
                name="params",
                type="object",
                description="Operation-specific parameters",
            ),
        ],
    )
    async def gh_proxy(operation: str, params: dict[str, Any]) -> str:
        """Run a GitHub operation via the proxy.

        :param operation: one of the supported operation names.
        :param params: dict of operation-specific keyword arguments.
        :returns: JSON string with the result or an error description.
        """
        if not whitelist.allowed(operation):
            log.warning(
                "gh_proxy denied",
                task_id=task_id,
                operation=operation,
                reason="not_in_allowlist",
            )
            _audit(
                audit,
                task_id,
                operation,
                params,
                False,
                "denied: not in allowlist",
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"operation {operation!r} is not allowed for this task. "
                        f"permitted: {sorted(whitelist.operations)}"
                    ),
                }
            )

        try:
            _validate_params(operation, params)
        except GhProxyError as exc:
            log.warning(
                "gh_proxy validation failed",
                task_id=task_id,
                operation=operation,
                error=str(exc),
            )
            _audit(
                audit,
                task_id,
                operation,
                params,
                False,
                str(exc),
            )
            return json.dumps({"success": False, "error": str(exc)})

        try:
            result = await _call_gh(client, operation, params)
        except Exception as exc:  # pragma: no cover  # noqa: BLE001
            log.warning(
                "gh_proxy execution failed",
                task_id=task_id,
                operation=operation,
                error=str(exc),
                exc_info=True,
            )
            _audit(
                audit,
                task_id,
                operation,
                params,
                False,
                str(exc),
            )
            return json.dumps({"success": False, "error": str(exc)})

        log.info("gh_proxy success", task_id=task_id, operation=operation)
        _audit(
            audit,
            task_id,
            operation,
            params,
            True,
            "ok",
        )
        return json.dumps(result)

    return gh_proxy


def _audit(
    audit: AuditLogger | None,
    task_id: str | None,
    operation: str,
    params: dict[str, Any],
    success: bool,
    summary: str,
) -> None:
    """Write a single gh_proxy call to the audit ledger if one is configured."""
    if audit is None:
        return
    try:
        audit.log_api_call(
            method="POST",
            path="/api/v1/gh_proxy",
            status=200 if success else 403,
            request_id=task_id,
            details={
                "operation": operation,
                "params": params,
                "success": success,
                "summary": summary,
            },
        )
    except Exception:  # noqa: BLE001
        log.warning("gh_proxy audit write failed", exc_info=True)
