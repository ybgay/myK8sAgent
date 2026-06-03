"""Namespace management tools for MCP server."""

from __future__ import annotations

from typing import Any

from kubernetes.client import V1Namespace, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_namespace_tools(mcp_server: Any) -> None:
    """Register namespace-related tools on the MCP server."""

    @mcp_server.tool()
    async def list_namespaces(label_selector: str | None = None) -> str:
        """List all namespaces, optionally filtered by label selector.

        Args:
            label_selector: Optional Kubernetes label selector (e.g. "env=prod").
        """
        k8s = create_k8s_client()
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            ns_list = k8s.core_v1.list_namespace(**kwargs)
            namespaces = []
            for ns in ns_list.items:
                namespaces.append({
                    "name": ns.metadata.name if ns.metadata else "",
                    "status": ns.status.phase if ns.status else "Unknown",
                    "labels": ns.metadata.labels if ns.metadata else {},
                    "creation_time": str(ns.metadata.creation_timestamp) if ns.metadata else "",
                })
            return _format_result("Namespaces", namespaces)
        except ApiException as e:
            return _format_error("list_namespaces", e)

    @mcp_server.tool()
    async def get_namespace(name: str) -> str:
        """Get details of a specific namespace.

        Args:
            name: The namespace name.
        """
        k8s = create_k8s_client()
        try:
            ns = k8s.core_v1.read_namespace(name)
            info = {
                "name": ns.metadata.name if ns.metadata else "",
                "status": ns.status.phase if ns.status else "Unknown",
                "labels": ns.metadata.labels if ns.metadata else {},
                "annotations": ns.metadata.annotations if ns.metadata else {},
                "creation_time": str(ns.metadata.creation_timestamp) if ns.metadata else "",
            }
            return _format_result("Namespace", info)
        except ApiException as e:
            return _format_error("get_namespace", e)

    @mcp_server.tool()
    async def create_namespace(
        name: str,
        labels: dict[str, str] | None = None,
        annotations: dict[str, str] | None = None,
    ) -> str:
        """Create a new namespace.

        Args:
            name: The namespace name.
            labels: Optional labels to apply.
            annotations: Optional annotations to apply.
        """
        k8s = create_k8s_client()
        try:
            ns = V1Namespace(
                metadata=V1ObjectMeta(
                    name=name,
                    labels=labels or {},
                    annotations=annotations or {},
                )
            )
            result = k8s.core_v1.create_namespace(ns)
            return f"Namespace '{result.metadata.name}' created successfully."
        except ApiException as e:
            return _format_error("create_namespace", e)

    @mcp_server.tool()
    async def delete_namespace(name: str, grace_period: int | None = None) -> str:
        """Delete a namespace. WARNING: This deletes all resources in the namespace!

        Args:
            name: The namespace to delete.
            grace_period: Grace period in seconds for pod termination.
        """
        k8s = create_k8s_client()
        try:
            kwargs = {}
            if grace_period is not None:
                kwargs["grace_period_seconds"] = grace_period
            k8s.core_v1.delete_namespace(name, **kwargs)
            return f"Namespace '{name}' deletion initiated."
        except ApiException as e:
            return _format_error("delete_namespace", e)


def _format_result(title: str, data: Any) -> str:
    """Format a successful tool result as JSON string."""
    import json
    output = {title.lower().replace(" ", "_"): data}
    return json.dumps(output, indent=2, default=str)


def _format_error(tool: str, error: ApiException) -> str:
    """Format a Kubernetes API error."""
    import json
    logger.error("k8s_tool_error", tool=tool, status=error.status, reason=error.reason)
    error_body = {}
    try:
        error_body = json.loads(error.body) if error.body else {}
    except (json.JSONDecodeError, AttributeError):
        pass
    return json.dumps({
        "error": True,
        "tool": tool,
        "status": error.status,
        "reason": error.reason,
        "message": error_body.get("message", str(error)),
    }, indent=2)
