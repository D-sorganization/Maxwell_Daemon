# Model Context Protocol (MCP) Guide

The Maxwell Daemon supports the **Model Context Protocol (MCP)** as both a **Server** and a **Client**, enabling seamless integration with other MCP-compatible tools and clients like Claude Desktop.

## Connecting Claude Desktop to Maxwell (Daemon as Server)

You can use Maxwell Daemon as an MCP server, exposing its built-in sandbox tools and daemon-specific tools (like `submit_task`, `list_tasks`, and `list_work_items`) to Claude Desktop.

To connect Claude Desktop to Maxwell Daemon, add the following configuration to your Claude Desktop config file (`~/Library/Application Support/Claude/claude_desktop_config.json` on Mac, or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "maxwell": {
      "command": "python",
      "args": [
        "-m",
        "maxwell_daemon.cli",
        "mcp",
        "server"
      ]
    }
  }
}
```

Once connected, Claude Desktop will have access to:
- **Tools**: Sandbox tools, Daemon API endpoints, and Action approval workflows.
- **Resources**: `artifact://list`, `workspace://list`, `memory://list`.
- **Prompts**: `maxwell_architect`, `maxwell_security`, and `maxwell_validator`.

## Let Maxwell Drive your MCP Servers (Daemon as Client)

The Maxwell Daemon can connect to external MCP servers to expand its own toolset. You can configure the Daemon to connect via `stdio`, `sse`, or `http` transport.

To add an MCP server to the Maxwell Daemon, edit your `~/.config/maxwell-daemon/config.toml` file:

```toml
[mcp.servers.local_filesystem]
enabled = true
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/expose"]
env = { "DEBUG" = "1" }

[mcp.servers.remote_service]
enabled = true
transport = "sse"
url = "http://localhost:8080/sse"
```

The tools from these MCP servers will automatically be injected into the Daemon's `ToolRegistry` and made available to agents during tasks!
