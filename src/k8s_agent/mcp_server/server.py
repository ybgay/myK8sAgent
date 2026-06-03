"""Kubernetes MCP Server — creates and configures the MCP server with all K8s tools.

This server can be used:
1. Standalone — any MCP-compatible client (Claude Desktop, Claude Code, etc.) can connect.
2. Embedded — the k8s-agent system uses it internally as its K8s tool provider.

Usage:
    # stdio mode (for Claude Desktop, etc.)
    python -m k8s_agent.mcp_server.server

    # HTTP mode
    python -m k8s_agent.mcp_server.server --transport http --port 3100
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from k8s_agent.mcp_server.tools import register_all_tools
from k8s_agent.shared.config import get_settings
from k8s_agent.shared.logging import get_logger, setup_logging

logger = get_logger(__name__)


def create_k8s_mcp_server(
    server_name: str = "k8s-agent-mcp-server",
    server_version: str = "0.1.0",
) -> FastMCP:
    """Create and configure the Kubernetes MCP Server.

    Registers all K8s tools (pods, deployments, services, etc.) on the server.
    Uses FastMCP which provides the @server.tool() decorator API.

    Args:
        server_name: Name for the MCP server.
        server_version: Version string.

    Returns:
        Configured FastMCP instance.
    """
    settings = get_settings()

    # Create FastMCP server
    server = FastMCP(
        name=server_name,
        instructions=f"Kubernetes MCP Server v{server_version} — provides K8s cluster operations",
    )

    # Register all Kubernetes tools via @server.tool() decorators
    register_all_tools(server)

    logger.info(
        "mcp_server_created",
        name=server_name,
        version=server_version,
        transport=settings.mcp_server.transport,
    )

    return server


async def run_stdio_server() -> None:
    """Run the MCP server over stdio transport (for Claude Desktop etc.)."""
    setup_logging(level="info", output_format="json")
    server = create_k8s_mcp_server()

    logger.info("mcp_server_starting", transport="stdio")
    await server.run_stdio_async()


async def run_http_server(host: str = "0.0.0.0", port: int = 3100) -> None:
    """Run the MCP server over HTTP with StreamableHTTP transport.

    Args:
        host: Host to bind to.
        port: Port to listen on.
    """
    setup_logging(level="info", output_format="pretty")
    server = create_k8s_mcp_server()

    logger.info("mcp_server_starting", transport="http", host=host, port=port)
    # Override default settings for host/port
    server.settings.host = host
    server.settings.port = port
    await server.run_streamable_http_async()


if __name__ == "__main__":
    import sys

    if "--transport" in sys.argv and "http" in sys.argv:
        host = "0.0.0.0"
        port = 3100
        for i, arg in enumerate(sys.argv):
            if arg == "--host" and i + 1 < len(sys.argv):
                host = sys.argv[i + 1]
            if arg == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
        asyncio.run(run_http_server(host, port))
    else:
        asyncio.run(run_stdio_server())
