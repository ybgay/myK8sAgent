"""Node management tools for MCP server."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client import V1Node, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_node_tools(mcp_server: Any) -> None:
    @mcp_server.tool()
    async def list_nodes(label_selector: str | None = None) -> str:
        """List all nodes in the cluster, optionally filtered by label.

        Args:
            label_selector: Optional label filter.
        """
        k8s = create_k8s_client()
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            nodes = k8s.core_v1.list_node(**kwargs)
            result = []
            for n in nodes.items:
                md = n.metadata
                status = n.status
                conditions = {}
                if status and status.conditions:
                    for c in status.conditions:
                        conditions[c.type] = c.status
                allocatable = {}
                if status and status.allocatable:
                    allocatable = {
                        "cpu": status.allocatable.get("cpu", ""),
                        "memory": status.allocatable.get("memory", ""),
                        "pods": status.allocatable.get("pods", ""),
                    }
                result.append({
                    "name": md.name if md else "",
                    "ready": conditions.get("Ready", "Unknown"),
                    "version": status.node_info.kubelet_version if status and status.node_info else "",
                    "role": _get_node_role(md),
                    "allocatable": allocatable,
                    "capacity": {
                        "cpu": status.capacity.get("cpu", "") if status and status.capacity else "",
                        "memory": status.capacity.get("memory", "") if status and status.capacity else "",
                    } if status and status.capacity else {},
                })
            return json.dumps({"nodes": result, "count": len(result)}, indent=2, default=str)
        except ApiException as e:
            return _api_error("list_nodes", e)

    @mcp_server.tool()
    async def get_node(name: str) -> str:
        """Get detailed information about a node.

        Args:
            name: Node name.
        """
        k8s = create_k8s_client()
        try:
            n = k8s.core_v1.read_node(name)
            md = n.metadata
            status = n.status
            conditions = {}
            if status and status.conditions:
                for c in status.conditions:
                    conditions[c.type] = {
                        "status": c.status, "reason": c.reason or "",
                        "message": c.message or "",
                    }
            return json.dumps({
                "name": md.name if md else "",
                "labels": md.labels if md else {},
                "taints": [f"{t.key}={t.value}:{t.effect}" for t in (n.spec.taints or [])],
                "conditions": conditions,
                "allocatable": status.allocatable if status else {},
                "capacity": status.capacity if status else {},
                "node_info": {
                    "kubelet_version": status.node_info.kubelet_version if status and status.node_info else "",
                    "os": status.node_info.operating_system if status and status.node_info else "",
                    "architecture": status.node_info.architecture if status and status.node_info else "",
                    "container_runtime": status.node_info.container_runtime_version if status and status.node_info else "",
                } if status and status.node_info else {},
            }, indent=2, default=str)
        except ApiException as e:
            return _api_error("get_node", e)

    @mcp_server.tool()
    async def cordon_node(name: str) -> str:
        """Mark a node as unschedulable (cordon).

        Args:
            name: Node name.
        """
        k8s = create_k8s_client()
        try:
            body = {"spec": {"unschedulable": True}}
            k8s.core_v1.patch_node(name, body)
            return f"Node '{name}' cordoned (marked unschedulable)."
        except ApiException as e:
            return _api_error("cordon_node", e)

    @mcp_server.tool()
    async def uncordon_node(name: str) -> str:
        """Mark a node as schedulable (uncordon).

        Args:
            name: Node name.
        """
        k8s = create_k8s_client()
        try:
            body = {"spec": {"unschedulable": False}}
            k8s.core_v1.patch_node(name, body)
            return f"Node '{name}' uncordoned (marked schedulable)."
        except ApiException as e:
            return _api_error("uncordon_node", e)

    @mcp_server.tool()
    async def drain_node(
        name: str,
        grace_period: int = 30,
        delete_local_data: bool = False,
        ignore_daemonsets: bool = True,
    ) -> str:
        """Drain a node (evict all pods). WARNING: Destructive operation!

        Args:
            name: Node name.
            grace_period: Grace period in seconds.
            delete_local_data: Delete local data of emptyDir pods.
            ignore_daemonsets: Ignore DaemonSet-managed pods.
        """
        k8s = create_k8s_client()
        try:
            # First cordon the node
            body = {"spec": {"unschedulable": True}}
            k8s.core_v1.patch_node(name, body)

            # List pods on the node
            pods = k8s.core_v1.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={name}"
            )
            evicted = 0
            for pod in pods.items:
                if pod.status and pod.status.phase in ("Succeeded", "Failed"):
                    continue
                # Skip daemonset pods if requested
                if ignore_daemonsets and _is_daemonset_pod(pod):
                    continue
                try:
                    from kubernetes.client import V1DeleteOptions
                    k8s.core_v1.delete_namespaced_pod(
                        pod.metadata.name,
                        pod.metadata.namespace,
                        grace_period_seconds=grace_period,
                    )
                    evicted += 1
                except ApiException:
                    pass
            return json.dumps({
                "message": f"Node '{name}' drain initiated.",
                "cordoned": True,
                "pods_evicted": evicted,
            }, indent=2)
        except ApiException as e:
            return _api_error("drain_node", e)


def _get_node_role(md: Any) -> str:
    """Extract node role from labels."""
    if not md or not md.labels:
        return "worker"
    for label in md.labels:
        if label.startswith("node-role.kubernetes.io/"):
            return label.replace("node-role.kubernetes.io/", "")
    if md.labels.get("node.kubernetes.io/role"):
        return md.labels["node.kubernetes.io/role"]
    return "worker"


def _is_daemonset_pod(pod: Any) -> bool:
    """Check if a pod is managed by a DaemonSet."""
    if pod.metadata and pod.metadata.owner_references:
        for ref in pod.metadata.owner_references:
            if ref.kind == "DaemonSet":
                return True
    return False


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
