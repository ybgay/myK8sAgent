"""K8s Agent — specialized agent that connects to the K8s MCP server.

This agent acts as an MCP client that connects to the k8s-agent MCP server
(or any MCP-compatible K8s server) and exposes K8s tools to the orchestrator.
"""

from __future__ import annotations

import json
from typing import Any

from k8s_agent.core.agents.base import BaseAgent
from k8s_agent.core.communication.message_bus import MessageBus
from k8s_agent.core.communication.context import SharedContext
from k8s_agent.shared.logging import get_logger
from k8s_agent.shared.types import (
    AgentCapability,
    TaskRequest,
    TaskResult,
    TaskStatus,
)


class K8sAgent(BaseAgent):
    """Agent specialized in Kubernetes operations.

    Connects to the K8s MCP server to execute cluster operations.
    Can also execute direct K8s API calls as a fallback.
    """

    def __init__(
        self,
        message_bus: MessageBus,
        context: SharedContext,
        mcp_client_manager: Any = None,  # MCPClientManager
    ) -> None:
        super().__init__(
            agent_type="k8s",
            name="k8s-agent",
            message_bus=message_bus,
            context=context,
            capabilities=[
                AgentCapability(
                    name="k8s_operations",
                    description="Execute Kubernetes cluster operations via MCP tools",
                    tags=["kubernetes", "pods", "deployments", "services", "cluster"],
                ),
            ],
        )
        self._mcp_client_manager = mcp_client_manager
        self._k8s_mcp_client = None  # Lazy init via mcp_client_manager

    async def initialize(self) -> None:
        """Initialize the K8s agent and connect to MCP server."""
        await super().initialize()

        # Connect to K8s MCP server via client manager
        if self._mcp_client_manager:
            try:
                self._k8s_mcp_client = await self._mcp_client_manager.connect(
                    server_name="k8s-agent-mcp-server",
                    transport="stdio",  # or "http" for remote
                )
                self.logger.info("k8s_mcp_connected")
            except Exception as e:
                self.logger.error("k8s_mcp_connect_failed", error=str(e))
                # Agent can still work with direct API calls as fallback

    async def execute_task(self, task: TaskRequest) -> TaskResult:
        """Execute a K8s task via MCP tools or direct API.

        The task params should include:
        - action: The tool name to call (e.g. "list_pods")
        - params: Tool parameters
        """
        action = task.params.get("action", task.action)

        try:
            if self._k8s_mcp_client:
                # Use MCP client
                result = await self._call_mcp_tool(
                    action,
                    task.params.get("params", {}),
                )
            else:
                # Fallback: direct K8s API via the mcp_server tool functions
                result = await self._call_direct(action, task.params.get("params", {}))

            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.SUCCESS,
                output=result,
            )
        except Exception as e:
            self.logger.error("k8s_task_failed", action=action, error=str(e))
            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                error=str(e),
            )

    async def _call_mcp_tool(self, tool_name: str, params: dict[str, Any]) -> str:
        """Call a tool on the K8s MCP server."""
        if self._mcp_client_manager:
            return await self._mcp_client_manager.call_tool(
                "k8s-agent-mcp-server",
                tool_name,
                params,
            )
        raise RuntimeError("MCP client manager not available")

    async def _call_direct(self, tool_name: str, params: dict[str, Any]) -> str:
        """Directly call K8s tool functions (fallback without MCP server)."""
        # Import the tool registration to access individual tool functions
        try:
            from k8s_agent.mcp_server.k8s_client import create_k8s_client
            from k8s_agent.mcp_server.tools.namespaces import (
                list_namespaces, get_namespace, create_namespace, delete_namespace,
            )

            k8s = create_k8s_client()

            # Route to the appropriate tool function
            if tool_name == "list_namespaces":
                return await list_namespaces()
            elif tool_name == "get_namespace":
                return await get_namespace(params["name"])
            else:
                return json.dumps({
                    "error": True,
                    "message": f"Direct tool '{tool_name}' not implemented in fallback. Use MCP server.",
                    "hint": "Run 'k8s-agent serve' to start the MCP server.",
                })
        except ImportError:
            return json.dumps({
                "error": True,
                "message": "Neither MCP server nor direct K8s client available. Install kubernetes package.",
            })

    async def get_available_tools(self) -> list[dict[str, Any]]:
        """Return all K8s MCP tools available to the orchestrator."""
        # These mirror the tools registered in mcp_server/tools/
        return [
            # Namespaces
            {"name": "list_namespaces", "description": "List all namespaces",
             "input_schema": {"type": "object", "properties": {"label_selector": {"type": "string"}}}},
            {"name": "get_namespace", "description": "Get namespace details",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            {"name": "create_namespace", "description": "Create a new namespace",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "labels": {"type": "object"}}, "required": ["name"]}},
            {"name": "delete_namespace", "description": "Delete a namespace",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            # Pods
            {"name": "list_pods", "description": "List pods in a namespace",
             "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}, "label_selector": {"type": "string"}}}},
            {"name": "get_pod", "description": "Get pod details",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]}},
            {"name": "describe_pod", "description": "Comprehensive pod description with events",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]}},
            {"name": "get_pod_logs", "description": "Get pod container logs",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}, "container": {"type": "string"}, "tail": {"type": "integer"}, "previous": {"type": "boolean"}}, "required": ["name"]}},
            # Deployments
            {"name": "list_deployments", "description": "List deployments in a namespace",
             "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}, "label_selector": {"type": "string"}}}},
            {"name": "get_deployment", "description": "Get deployment details",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]}},
            {"name": "create_deployment", "description": "Create a new deployment",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}, "image": {"type": "string"}, "replicas": {"type": "integer"}}, "required": ["name"]}},
            {"name": "update_deployment", "description": "Update a deployment",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}, "image": {"type": "string"}, "replicas": {"type": "integer"}}, "required": ["name"]}},
            {"name": "restart_deployment", "description": "Trigger a rolling restart",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]}},
            {"name": "rollout_status", "description": "Check deployment rollout status",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]}},
            {"name": "scale_deployment", "description": "Scale a deployment",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}, "replicas": {"type": "integer"}}, "required": ["name", "replicas"]}},
            # Services
            {"name": "list_services", "description": "List services in a namespace",
             "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}}}},
            {"name": "get_service", "description": "Get service details",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]}},
            {"name": "create_service", "description": "Create a service",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}, "port": {"type": "integer"}, "service_type": {"type": "string"}}, "required": ["name"]}},
            # Nodes
            {"name": "list_nodes", "description": "List all cluster nodes",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "get_node", "description": "Get node details",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            # Events and Logs
            {"name": "list_events", "description": "List Kubernetes events",
             "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}, "event_type": {"type": "string"}}}},
            {"name": "get_resource_events", "description": "Get events for a resource",
             "input_schema": {"type": "object", "properties": {"kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["kind", "name"]}},
            # ConfigMaps and Secrets
            {"name": "list_configmaps", "description": "List ConfigMaps",
             "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}}}},
            {"name": "get_configmap", "description": "Get ConfigMap data",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["name"]}},
            {"name": "list_secrets", "description": "List Secrets (names only)",
             "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}}}},
            # Helm
            {"name": "list_helm_releases", "description": "List Helm releases",
             "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}, "all_namespaces": {"type": "boolean"}}}},
            {"name": "install_helm_chart", "description": "Install a Helm chart",
             "input_schema": {"type": "object", "properties": {"chart": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["chart", "name"]}},
            # Metrics and Info
            {"name": "get_cluster_info", "description": "Get cluster version info",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "get_node_metrics", "description": "Get node CPU/memory metrics",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}}},
            {"name": "get_pod_metrics", "description": "Get pod CPU/memory metrics",
             "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}, "name": {"type": "string"}}}},
        ]
