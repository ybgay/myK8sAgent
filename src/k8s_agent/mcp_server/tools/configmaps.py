"""ConfigMap management tools for MCP server."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client import V1ConfigMap, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_configmap_tools(mcp_server: Any) -> None:
    @mcp_server.tool()
    async def list_configmaps(namespace: str = "default") -> str:
        """List all ConfigMaps in a namespace.

        Args:
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            cms = k8s.core_v1.list_namespaced_config_map(namespace)
            result = [
                {
                    "name": cm.metadata.name if cm.metadata else "",
                    "namespace": namespace,
                    "data_keys": list(cm.data.keys()) if cm.data else [],
                }
                for cm in cms.items
            ]
            return json.dumps({"configmaps": result, "count": len(result)}, indent=2)
        except ApiException as e:
            return _api_error("list_configmaps", e)

    @mcp_server.tool()
    async def get_configmap(name: str, namespace: str = "default") -> str:
        """Get a ConfigMap's data.

        Args:
            name: ConfigMap name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            cm = k8s.core_v1.read_namespaced_config_map(name, namespace)
            return json.dumps({
                "name": cm.metadata.name if cm.metadata else "",
                "namespace": namespace,
                "data": cm.data or {},
                "binary_data_keys": list(cm.binary_data.keys()) if cm.binary_data else [],
            }, indent=2)
        except ApiException as e:
            return _api_error("get_configmap", e)

    @mcp_server.tool()
    async def create_configmap(
        name: str, namespace: str = "default", data: dict[str, str] | None = None
    ) -> str:
        """Create a ConfigMap.

        Args:
            name: ConfigMap name.
            namespace: Target namespace.
            data: Key-value data for the ConfigMap.
        """
        k8s = create_k8s_client()
        try:
            cm = V1ConfigMap(
                metadata=V1ObjectMeta(name=name),
                data=data or {},
            )
            result = k8s.core_v1.create_namespaced_config_map(namespace, cm)
            return f"ConfigMap '{result.metadata.name}' created in '{namespace}'."
        except ApiException as e:
            return _api_error("create_configmap", e)

    @mcp_server.tool()
    async def update_configmap(
        name: str, namespace: str = "default", data: dict[str, str] | None = None
    ) -> str:
        """Update a ConfigMap's data (merges with existing).

        Args:
            name: ConfigMap name.
            namespace: Target namespace.
            data: New key-value data to merge.
        """
        k8s = create_k8s_client()
        try:
            cm = k8s.core_v1.read_namespaced_config_map(name, namespace)
            if cm.data is None:
                cm.data = {}
            if data:
                cm.data.update(data)
            k8s.core_v1.patch_namespaced_config_map(name, namespace, cm)
            return f"ConfigMap '{name}' updated in '{namespace}'."
        except ApiException as e:
            return _api_error("update_configmap", e)

    @mcp_server.tool()
    async def delete_configmap(name: str, namespace: str = "default") -> str:
        """Delete a ConfigMap.

        Args:
            name: ConfigMap name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            k8s.core_v1.delete_namespaced_config_map(name, namespace)
            return f"ConfigMap '{name}' deleted from '{namespace}'."
        except ApiException as e:
            return _api_error("delete_configmap", e)


def _api_error(tool: str, error: ApiException) -> str:
    logger.error("k8s_tool_error", tool=tool, status=error.status)
    eb = {}
    try:
        eb = json.loads(error.body) if error.body else {}
    except (json.JSONDecodeError, AttributeError):
        pass
    return json.dumps({
        "error": True, "tool": tool, "status": error.status,
        "reason": error.reason, "message": eb.get("message", str(error)),
    }, indent=2)
