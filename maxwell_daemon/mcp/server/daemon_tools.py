import json
from typing import Any

from maxwell_daemon.mcp.server.daemon_client import DaemonClient
from maxwell_daemon.tools.mcp import ToolParam, ToolRegistry, mcp_tool


def build_daemon_registry(client: DaemonClient) -> ToolRegistry:
    registry = ToolRegistry()

    @mcp_tool(
        description="Submit a new prompt task to the Maxwell Daemon.",
        params=[
            ToolParam(
                name="prompt",
                type="string",
                description="The instruction prompt for the agent.",
                required=True,
            ),
            ToolParam(
                name="repo",
                type="string",
                description="Optional repository context.",
                required=False,
            ),
            ToolParam(
                name="backend",
                type="string",
                description="Optional backend to route to.",
                required=False,
            ),
            ToolParam(
                name="model",
                type="string",
                description="Optional model to route to.",
                required=False,
            ),
            ToolParam(
                name="priority",
                type="integer",
                description="Priority (0-1000, 100 is normal).",
                required=False,
            ),
        ],
    )
    async def submit_task(prompt: str, **kwargs: Any) -> str:
        payload = {"prompt": prompt, **kwargs}
        res = await client.post("/tasks", json=payload)
        return json.dumps(res, indent=2)

    @mcp_tool(description="List tasks in the daemon.", params=[])
    async def list_tasks(**kwargs: Any) -> str:
        res = await client.get("/tasks")
        return json.dumps(res, indent=2)

    @mcp_tool(
        description="Get a specific task by ID.",
        params=[
            ToolParam(
                name="task_id",
                type="string",
                description="The ID of the task.",
                required=True,
            )
        ],
    )
    async def get_task(task_id: str, **kwargs: Any) -> str:
        res = await client.get(f"/tasks/{task_id}")
        return json.dumps(res, indent=2)

    @mcp_tool(
        description="Cancel a specific task by ID.",
        params=[
            ToolParam(
                name="task_id",
                type="string",
                description="The ID of the task to cancel.",
                required=True,
            )
        ],
    )
    async def cancel_task(task_id: str, **kwargs: Any) -> str:
        res = await client.post(f"/tasks/{task_id}/cancel")
        return json.dumps(res, indent=2)

    @mcp_tool(description="List the capabilities of the fleet.", params=[])
    async def list_fleet(**kwargs: Any) -> str:
        res = await client.get("/fleet")
        return json.dumps(res, indent=2)

    @mcp_tool(description="Get the daemon's cost analytics.", params=[])
    async def get_cost(**kwargs: Any) -> str:
        res = await client.get("/cost")
        return json.dumps(res, indent=2)

    @mcp_tool(description="List work items (issues) in the daemon.", params=[])
    async def list_work_items(**kwargs: Any) -> str:
        res = await client.get("/work-items")
        return json.dumps(res, indent=2)

    @mcp_tool(
        description="Submit a GitHub issue task.",
        params=[
            ToolParam(
                name="issue_repo",
                type="string",
                description="The target repository (owner/repo).",
                required=True,
            ),
            ToolParam(
                name="issue_number",
                type="integer",
                description="The issue number.",
                required=True,
            ),
            ToolParam(
                name="mode",
                type="string",
                description="'plan' or 'implement'.",
                required=False,
            ),
        ],
    )
    async def submit_issue(issue_repo: str, issue_number: int, **kwargs: Any) -> str:
        payload = {"issue_repo": issue_repo, "issue_number": issue_number, **kwargs}
        res = await client.post("/tasks", json=payload)
        return json.dumps(res, indent=2)

    @mcp_tool(description="List pending approvals.", params=[])
    async def list_approvals(**kwargs: Any) -> str:
        res = await client.get("/approvals")
        return json.dumps(res, indent=2)

    @mcp_tool(
        description="Approve a pending action.",
        params=[
            ToolParam(
                name="action_id",
                type="string",
                description="The ID of the action.",
                required=True,
            )
        ],
    )
    async def approve_action(action_id: str, **kwargs: Any) -> str:
        res = await client.post(f"/approvals/{action_id}/approve")
        return json.dumps(res, indent=2)

    @mcp_tool(
        description="Search episodic memory.",
        params=[
            ToolParam(
                name="query", type="string", description="Search query.", required=True
            )
        ],
    )
    async def search_memory(query: str, **kwargs: Any) -> str:
        res = await client.get("/memory/search", params={"q": query})
        return json.dumps(res, indent=2)

    registry.register(submit_task.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(list_tasks.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(get_task.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(cancel_task.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(list_fleet.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(get_cost.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(list_work_items.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(submit_issue.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(list_approvals.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(approve_action.__mcp_tool__)  # type: ignore[attr-defined]
    registry.register(search_memory.__mcp_tool__)  # type: ignore[attr-defined]

    return registry
