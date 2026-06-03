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

from mcp.server import Server, NotificationOptions, InitializationOptions

from k8s_agent.mcp_server.tools import register_all_tools
from k8s_agent.shared.config import get_settings
from k8s_agent.shared.logging import get_logger, setup_logging

logger = get_logger(__name__)


def create_k8s_mcp_server(
    server_name: str = "k8s-agent-mcp-server",
    server_version: str = "0.1.0",
) -> Server:
    """Create and configure the Kubernetes MCP Server.

    Registers all K8s tools (pods, deployments, services, etc.) on the server.
    The server is an MCP Server instance ready to be connected to a transport.

    Args:
        server_name: Name for the MCP server.
        server_version: Version string.

    Returns:
        Configured mcp.server.Server instance.
    """
    settings = get_settings()

    # Create MCP server with capabilities
    server = Server(
        name=server_name,
        version=server_version,
    )

    # Register all Kubernetes tools
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
    from mcp.server.stdio import stdio_server

    setup_logging(level="info", output_format="json")
    server = create_k8s_mcp_server()

    logger.info("mcp_server_starting", transport="stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


async def run_http_server(host: str = "0.0.0.0", port: int = 3100) -> None:
    """Run the MCP server over HTTP with StreamableHTTP transport.

    Args:
        host: Host to bind to.
        port: Port to listen on.
    """
    from mcp.server.streamable_http import StreamableHTTPServerTransport

    # We need a simple HTTP server to host the transport
    import http.server
    import json

    setup_logging(level="info", output_format="pretty")
    server = create_k8s_mcp_server()

    logger.info("mcp_server_starting", transport="http", host=host, port=port)

    transport = StreamableHTTPServerTransport()

    class MCPHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            # Route to MCP transport
            response = asyncio.run(transport.handle_request(body.decode("utf-8")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response.encode("utf-8"))

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "server": "k8s-mcp-server"}).encode())
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "server": "k8s-agent-mcp-server",
                    "version": "0.1.0",
                    "tools": "/mcp",
                    "health": "/health",
                }).encode())

    httpd = http.server.HTTPServer((host, port), MCPHandler)
    logger.info(f"K8s MCP Server listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("mcp_server_shutting_down")
        httpd.shutdown()


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
