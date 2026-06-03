"""MCP tool registration — imports and registers all K8s tools on the MCP server."""

from __future__ import annotations

from typing import Any

from k8s_agent.mcp_server.tools.namespaces import register_namespace_tools
from k8s_agent.mcp_server.tools.pods import register_pod_tools
from k8s_agent.mcp_server.tools.deployments import register_deployment_tools
from k8s_agent.mcp_server.tools.services import register_service_tools
from k8s_agent.mcp_server.tools.configmaps import register_configmap_tools
from k8s_agent.mcp_server.tools.secrets import register_secret_tools
from k8s_agent.mcp_server.tools.nodes import register_node_tools
from k8s_agent.mcp_server.tools.events import register_event_tools
from k8s_agent.mcp_server.tools.logs import register_log_tools
from k8s_agent.mcp_server.tools.scale import register_scale_tools
from k8s_agent.mcp_server.tools.helm import register_helm_tools
from k8s_agent.mcp_server.tools.metrics import register_metrics_tools
from k8s_agent.mcp_server.tools.topology import register_topology_tools


def register_all_tools(mcp_server: Any) -> None:
    """Register all Kubernetes tools on the given MCP server instance.

    Args:
        mcp_server: An MCP Server instance (mcp.server.Server).
    """
    register_namespace_tools(mcp_server)
    register_pod_tools(mcp_server)
    register_deployment_tools(mcp_server)
    register_service_tools(mcp_server)
    register_configmap_tools(mcp_server)
    register_secret_tools(mcp_server)
    register_node_tools(mcp_server)
    register_event_tools(mcp_server)
    register_log_tools(mcp_server)
    register_scale_tools(mcp_server)
    register_helm_tools(mcp_server)
    register_metrics_tools(mcp_server)
    register_topology_tools(mcp_server)
