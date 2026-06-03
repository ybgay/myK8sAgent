"""Kubernetes MCP Server — exposes K8s operations via the Model Context Protocol."""

from k8s_agent.mcp_server.server import create_k8s_mcp_server

__all__ = ["create_k8s_mcp_server"]
