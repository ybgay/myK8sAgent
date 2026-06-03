"""Scaling tools for MCP server."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client.exceptions import ApiException

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_scale_tools(mcp_server: Any) -> None:
    @mcp_server.tool()
    async def scale_deployment(
        name: str, namespace: str = "default", replicas: int = 1
    ) -> str:
        """Scale a deployment to the specified number of replicas.

        Args:
            name: Deployment name.
            namespace: Target namespace.
            replicas: Desired number of replicas.
        """
        k8s = create_k8s_client()
        try:
            dep = k8s.apps_v1.read_namespaced_deployment(name, namespace)
            old_replicas = dep.spec.replicas if dep.spec else 0
            dep.spec.replicas = replicas
            k8s.apps_v1.patch_namespaced_deployment(name, namespace, dep)
            return json.dumps({
                "message": f"Deployment '{name}' scaled from {old_replicas} to {replicas} replicas.",
                "name": name, "namespace": namespace,
                "old_replicas": old_replicas, "new_replicas": replicas,
            }, indent=2)
        except ApiException as e:
            return _api_error("scale_deployment", e)

    @mcp_server.tool()
    async def scale_statefulset(
        name: str, namespace: str = "default", replicas: int = 1
    ) -> str:
        """Scale a StatefulSet to the specified replicas.

        Args:
            name: StatefulSet name.
            namespace: Target namespace.
            replicas: Desired replicas.
        """
        k8s = create_k8s_client()
        try:
            sts = k8s.apps_v1.read_namespaced_stateful_set(name, namespace)
            old_replicas = sts.spec.replicas if sts.spec else 0
            sts.spec.replicas = replicas
            k8s.apps_v1.patch_namespaced_stateful_set(name, namespace, sts)
            return json.dumps({
                "message": f"StatefulSet '{name}' scaled from {old_replicas} to {replicas} replicas.",
                "name": name, "namespace": namespace,
            }, indent=2)
        except ApiException as e:
            return _api_error("scale_statefulset", e)

    @mcp_server.tool()
    async def get_hpa(
        name: str, namespace: str = "default"
    ) -> str:
        """Get HorizontalPodAutoscaler details.

        Args:
            name: HPA name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            from kubernetes.client import AutoscalingV1Api
            hpa_api = AutoscalingV1Api(k8s.api_client)
            hpa = hpa_api.read_namespaced_horizontal_pod_autoscaler(name, namespace)
            status = hpa.status
            spec = hpa.spec
            return json.dumps({
                "name": hpa.metadata.name if hpa.metadata else "",
                "namespace": namespace,
                "target": f"{spec.scale_target_ref.kind}/{spec.scale_target_ref.name}" if spec and spec.scale_target_ref else "",
                "min_replicas": spec.min_replicas if spec else 0,
                "max_replicas": spec.max_replicas if spec else 0,
                "current_replicas": status.current_replicas if status else 0,
                "desired_replicas": status.desired_replicas if status else 0,
                "current_cpu_utilization": status.current_cpu_utilization_percentage if status else None,
            }, indent=2, default=str)
        except ApiException as e:
            return _api_error("get_hpa", e)


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
