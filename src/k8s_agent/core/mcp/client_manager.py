"""MCP Client Manager — connects to ANY MCP-compatible server.

Supports connecting to:
- Our built-in K8s MCP Server
- Open-source MCP servers (GitHub MCP, Postgres MCP, Slack MCP, etc.)
- Any MCP-compatible server via stdio or HTTP transport

Configuration for third-party MCP servers in config file:

```yaml
mcp_servers:
  - name: "github"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@anthropic/mcp-server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"
  - name: "postgres"
    transport: "http"
    url: "http://localhost:3200/mcp"
  - name: "custom-k8s-tools"
    transport: "stdio"
    command: "python"
    args: ["-m", "my_custom_mcp.server"]
```
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from k8s_agent.shared.config import get_settings
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server connection."""

    name: str
    transport: str = "stdio"  # "stdio" | "http"
    command: str | None = None  # For stdio: the executable
    args: list[str] = field(default_factory=list)  # For stdio: command args
    env: dict[str, str] = field(default_factory=dict)  # For stdio: env vars
    url: str | None = None  # For http: server URL
    headers: dict[str, str] = field(default_factory=dict)  # For http: auth headers


@dataclass
class MCPConnection:
    """Active connection to an MCP server."""

    config: MCPServerConfig
    process: subprocess.Popen | None = None  # For stdio transport
    http_client: httpx.AsyncClient | None = None  # For http transport
    tools: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 0

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id


class MCPClientManager:
    """Manages connections to multiple MCP servers.

    Each server is identified by a unique name. Tools from all connected
    servers are aggregated and made available to agents.

    Usage:
        manager = MCPClientManager()

        # Connect to our built-in K8s MCP server
        await manager.connect(MCPServerConfig(
            name="k8s",
            transport="stdio",
            command="python",
            args=["-m", "k8s_agent.mcp_server.server"],
        ))

        # Connect to GitHub MCP (open source)
        await manager.connect(MCPServerConfig(
            name="github",
            transport="stdio",
            command="npx",
            args=["-y", "@anthropic/mcp-server-github"],
            env={"GITHUB_TOKEN": os.environ["GITHUB_TOKEN"]},
        ))

        # Connect to a remote MCP server via HTTP
        await manager.connect(MCPServerConfig(
            name="postgres",
            transport="http",
            url="http://pg-mcp:3200/mcp",
        ))

        # Call a tool
        result = await manager.call_tool("k8s", "list_pods", {"namespace": "default"})
    """

    def __init__(self) -> None:
        self._connections: dict[str, MCPConnection] = {}

    async def connect(self, config: MCPServerConfig | None = None, **kwargs: Any) -> MCPConnection:
        """Connect to an MCP server.

        Can be called with an MCPServerConfig object, or with keyword args
        to construct one.

        Args:
            config: Full server configuration.
            **kwargs: Shorthand for constructing MCPServerConfig.

        Returns:
            The active MCPConnection.
        """
        if config is None:
            config = MCPServerConfig(**kwargs)

        if config.name in self._connections:
            logger.info("mcp_already_connected", server=config.name)
            return self._connections[config.name]

        logger.info("mcp_connecting", server=config.name, transport=config.transport)

        if config.transport == "stdio":
            conn = await self._connect_stdio(config)
        elif config.transport == "http":
            conn = await self._connect_http(config)
        else:
            raise ValueError(f"Unknown transport: {config.transport}")

        # Discover tools from the server
        conn.tools = await self._discover_tools(conn)

        self._connections[config.name] = conn
        logger.info(
            "mcp_connected",
            server=config.name,
            transport=config.transport,
            tool_count=len(conn.tools),
        )

        return conn

    async def _connect_stdio(self, config: MCPServerConfig) -> MCPConnection:
        """Connect to an MCP server over stdio (subprocess).

        Launches the server as a subprocess and communicates via
        JSON-RPC over stdin/stdout.
        """
        if not config.command:
            raise ValueError("stdio transport requires 'command'")

        # Resolve environment variables
        env = os.environ.copy()
        for key, value in config.env.items():
            # Resolve ${VAR} references
            import re
            def replacer(m: re.Match[str]) -> str:
                return os.environ.get(m.group(1), m.group(0))
            value = re.sub(r"\$\{([^}]+)\}", replacer, value)
            env[key] = value

        try:
            process = subprocess.Popen(
                [config.command] + config.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )
            logger.info(
                "mcp_stdio_process_started",
                server=config.name,
                pid=process.pid,
                command=f"{config.command} {' '.join(config.args)}",
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"Command not found: {config.command}. "
                f"Is it installed? Try: pip install {config.command}"
            ) from e

        conn = MCPConnection(config=config, process=process)

        # Initialize MCP session
        try:
            await self._send_jsonrpc(conn, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "k8s-agent", "version": "0.1.0"},
            })
            # Send initialized notification
            await self._send_jsonrpc(conn, "notifications/initialized", {})
        except Exception as e:
            process.kill()
            raise RuntimeError(
                f"Failed to initialize MCP server '{config.name}': {e}"
            ) from e

        return conn

    async def _connect_http(self, config: MCPServerConfig) -> MCPConnection:
        """Connect to an MCP server over HTTP.

        Uses the StreamableHTTP transport for MCP over HTTP.
        """
        if not config.url:
            raise ValueError("http transport requires 'url'")

        client = httpx.AsyncClient(
            base_url=config.url,
            headers={
                "Content-Type": "application/json",
                **config.headers,
            },
            timeout=30.0,
        )

        conn = MCPConnection(config=config, http_client=client)

        # Initialize MCP session over HTTP
        try:
            response = await client.post("/mcp", json={
                "jsonrpc": "2.0",
                "id": conn.next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "k8s-agent", "version": "0.1.0"},
                },
            })
            response.raise_for_status()
        except httpx.HTTPError as e:
            await client.aclose()
            raise RuntimeError(
                f"Failed to connect to MCP server at {config.url}: {e}"
            ) from e

        return conn

    async def _discover_tools(self, conn: MCPConnection) -> list[dict[str, Any]]:
        """Discover available tools from the connected MCP server."""
        try:
            result = await self._send_jsonrpc(conn, "tools/list", {})
            if isinstance(result, dict):
                return result.get("tools", [])
            return []
        except Exception as e:
            logger.warning("mcp_tool_discovery_failed", server=conn.config.name, error=str(e))
            return []

    async def _send_jsonrpc(
        self,
        conn: MCPConnection,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        """Send a JSON-RPC request to an MCP server and return the result.

        Handles both stdio and HTTP transports transparently.
        """
        request_id = conn.next_id()
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        if conn.config.transport == "stdio" and conn.process:
            return await self._stdio_request(conn, request)
        elif conn.config.transport == "http" and conn.http_client:
            return await self._http_request(conn, request)
        else:
            raise RuntimeError(f"No active transport for server '{conn.config.name}'")

    async def _stdio_request(
        self, conn: MCPConnection, request: dict[str, Any]
    ) -> Any:
        """Send JSON-RPC over stdin/stdout to a subprocess."""
        process = conn.process
        if not process or not process.stdin or not process.stdout:
            raise RuntimeError(f"stdio process not running for '{conn.config.name}'")

        request_str = json.dumps(request) + "\n"
        process.stdin.write(request_str)
        process.stdin.flush()

        # Read response line
        response_line = process.stdout.readline()
        if not response_line:
            # Check stderr for errors
            stderr = process.stderr.read() if process.stderr else ""
            if stderr:
                logger.error("mcp_stdio_stderr", server=conn.config.name, stderr=stderr[:2000])
            raise RuntimeError(f"MCP server '{conn.config.name}' closed connection")

        try:
            response = json.loads(response_line)
        except json.JSONDecodeError as e:
            logger.error("mcp_bad_response", server=conn.config.name, raw=response_line[:200])
            raise RuntimeError(f"Invalid JSON from MCP server: {e}") from e

        if "error" in response:
            error = response["error"]
            raise RuntimeError(
                f"MCP error from '{conn.config.name}': {error.get('message', str(error))}"
            )

        return response.get("result")

    async def _http_request(
        self, conn: MCPConnection, request: dict[str, Any]
    ) -> Any:
        """Send JSON-RPC over HTTP to an MCP server."""
        if not conn.http_client:
            raise RuntimeError(f"HTTP client not initialized for '{conn.config.name}'")

        response = await conn.http_client.post("/mcp", json=request)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            error = data["error"]
            raise RuntimeError(
                f"MCP error from '{conn.config.name}': {error.get('message', str(error))}"
            )

        return data.get("result")

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Call a tool on a connected MCP server.

        Args:
            server_name: Name of the connected server.
            tool_name: Name of the tool to call.
            arguments: Tool arguments.

        Returns:
            The tool's result as a string.

        Raises:
            RuntimeError: If server not connected or tool call fails.
        """
        if server_name not in self._connections:
            raise RuntimeError(
                f"MCP server '{server_name}' not connected. "
                f"Available: {list(self._connections.keys())}"
            )

        conn = self._connections[server_name]
        start = time.monotonic()

        try:
            result = await self._send_jsonrpc(conn, "tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })

            elapsed = (time.monotonic() - start) * 1000
            logger.debug(
                "mcp_tool_call",
                server=server_name,
                tool=tool_name,
                elapsed_ms=round(elapsed, 1),
            )

            # Handle different result formats
            if isinstance(result, dict):
                # Extract text content from MCP result format
                content = result.get("content", [])
                if isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                    return "\n".join(text_parts) if text_parts else json.dumps(result, default=str)
                return json.dumps(result, default=str)
            return str(result)

        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            logger.error(
                "mcp_tool_call_failed",
                server=server_name,
                tool=tool_name,
                elapsed_ms=round(elapsed, 1),
                error=str(e),
            )
            raise

    async def list_all_tools(self) -> dict[str, list[dict[str, Any]]]:
        """List tools from all connected MCP servers.

        Returns:
            Dict mapping server_name -> list of tools.
        """
        return {
            name: conn.tools
            for name, conn in self._connections.items()
        }

    async def get_aggregated_tools(self) -> list[dict[str, Any]]:
        """Get all tools from all servers, with server name prefix.

        Tools are prefixed with server name to avoid collisions:
        "k8s/list_pods", "github/search_repos", etc.

        Returns:
            Combined list of tools from all servers.
        """
        all_tools = []
        for server_name, conn in self._connections.items():
            for tool in conn.tools:
                all_tools.append({
                    "name": f"{server_name}/{tool.get('name', 'unknown')}",
                    "description": f"[{server_name}] {tool.get('description', '')}",
                    "input_schema": tool.get("input_schema", {}),
                    "_server": server_name,
                    "_original_name": tool.get("name", ""),
                })
        return all_tools

    async def disconnect(self, server_name: str) -> None:
        """Disconnect from an MCP server."""
        if server_name not in self._connections:
            return

        conn = self._connections.pop(server_name)
        logger.info("mcp_disconnecting", server=server_name)

        if conn.process:
            try:
                conn.process.stdin.close()
                conn.process.stdout.close()
                conn.process.terminate()
                conn.process.wait(timeout=5)
            except Exception as e:
                logger.warning("mcp_process_cleanup_error", server=server_name, error=str(e))
                conn.process.kill()

        if conn.http_client:
            await conn.http_client.aclose()

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers."""
        for name in list(self._connections.keys()):
            await self.disconnect(name)

    def get_connection_names(self) -> list[str]:
        """Get names of all connected MCP servers."""
        return list(self._connections.keys())

    def is_connected(self, server_name: str) -> bool:
        """Check if a server is connected."""
        return server_name in self._connections

    # ------------------------------------------------------------------
    # Convenience methods for configuring third-party servers
    # ------------------------------------------------------------------

    @classmethod
    def github_server_config(cls, token: str | None = None) -> MCPServerConfig:
        """Get configuration for the GitHub MCP server (open source).

        GitHub MCP provides: search_repos, create_issue, create_pr, etc.
        Repo: https://github.com/anthropics/mcp-servers
        """
        return MCPServerConfig(
            name="github",
            transport="stdio",
            command="npx",
            args=["-y", "@anthropic/mcp-server-github"],
            env={"GITHUB_TOKEN": token or os.environ.get("GITHUB_TOKEN", "")},
        )

    @classmethod
    def postgres_server_config(cls, connection_string: str) -> MCPServerConfig:
        """Get configuration for a Postgres MCP server.

        Postgres MCP provides SQL query tools.
        Repo: https://github.com/modelcontextprotocol/servers
        """
        return MCPServerConfig(
            name="postgres",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-postgres", connection_string],
        )

    @classmethod
    def slack_server_config(cls, token: str | None = None) -> MCPServerConfig:
        """Get configuration for a Slack MCP server."""
        return MCPServerConfig(
            name="slack",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-slack"],
            env={"SLACK_BOT_TOKEN": token or os.environ.get("SLACK_BOT_TOKEN", "")},
        )

    @classmethod
    def filesystem_server_config(cls, path: str = ".") -> MCPServerConfig:
        """Get configuration for a filesystem MCP server.

        Provides file read/write tools within the allowed directory.
        """
        return MCPServerConfig(
            name="filesystem",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", path],
        )
