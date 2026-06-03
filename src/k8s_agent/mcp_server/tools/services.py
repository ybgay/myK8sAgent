"""Service management tools for MCP server."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client import (
    V1Service, V1ObjectMeta, V1ServiceSpec, V1ServicePort,
)
from kubernetes.client.exceptions import ApiException

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_service_tools(mcp_server: Any) -> None:
    """Register service-related tools."""

    @mcp_server.tool()
    async def list_services(
        namespace: str = "default",
        label_selector: str | None = None,
    ) -> str:
        """List all services in a namespace.

        Args:
            namespace: Target namespace.
            label_selector: Optional label filter.
        """
        k8s = create_k8s_client()
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            svcs = k8s.core_v1.list_namespaced_service(namespace, **kwargs)
            result = []
            for svc in svcs.items:
                md = svc.metadata
                spec = svc.spec
                ports = []
                if spec and spec.ports:
                    for p in spec.ports:
                        ports.append({
                            "name": p.name or "",
                            "port": p.port,
                            "target_port": p.target_port,
                            "protocol": p.protocol or "TCP",
                        })
                result.append({
                    "name": md.name if md else "",
                    "namespace": md.namespace if md else namespace,
                    "type": spec.type if spec else "ClusterIP",
                    "cluster_ip": spec.cluster_ip if spec else "",
                    "ports": ports,
                    "selector": spec.selector if spec else {},
                })
            return json.dumps({"services": result, "count": len(result)}, indent=2, default=str)
        except ApiException as e:
            return _api_error("list_services", e)

    @mcp_server.tool()
    async def get_service(name: str, namespace: str = "default") -> str:
        """Get details of a specific service.

        Args:
            name: Service name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            svc = k8s.core_v1.read_namespaced_service(name, namespace)
            return json.dumps(
                _service_to_dict(svc), indent=2, default=str
            )
        except ApiException as e:
            return _api_error("get_service", e)

    @mcp_server.tool()
    async def create_service(
        name: str,
        namespace: str = "default",
        port: int = 80,
        target_port: int | None = None,
        service_type: str = "ClusterIP",
        selector: dict[str, str] | None = None,
    ) -> str:
        """Create a new service.

        Args:
            name: Service name.
            namespace: Target namespace.
            port: Service port.
            target_port: Container port (defaults to same as port).
            service_type: Service type (ClusterIP, NodePort, LoadBalancer).
            selector: Pod selector labels.
        """
        k8s = create_k8s_client()
        try:
            sel = selector or {"app": name}
            tp = target_port or port
            svc = V1Service(
                metadata=V1ObjectMeta(name=name),
                spec=V1ServiceSpec(
                    selector=sel,
                    type=service_type,
                    ports=[V1ServicePort(port=port, target_port=tp, protocol="TCP")],
                ),
            )
            result = k8s.core_v1.create_namespaced_service(namespace, svc)
            return json.dumps({
                "message": f"Service '{result.metadata.name}' created in '{namespace}'.",
                "name": result.metadata.name,
                "cluster_ip": result.spec.cluster_ip if result.spec else "",
            }, indent=2)
        except ApiException as e:
            return _api_error("create_service", e)

    @mcp_server.tool()
    async def delete_service(name: str, namespace: str = "default") -> str:
        """Delete a service.

        Args:
            name: Service name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            k8s.core_v1.delete_namespaced_service(name, namespace)
            return f"Service '{name}' deleted from '{namespace}'."
        except ApiException as e:
            return _api_error("delete_service", e)

    @mcp_server.tool()
    async def expose_deployment(
        deployment_name: str,
        namespace: str = "default",
        port: int = 80,
        service_type: str = "ClusterIP",
    ) -> str:
        """Create a service to expose a deployment.

        Args:
            deployment_name: The deployment to expose.
            namespace: Target namespace.
            port: Service port.
            service_type: Service type.
        """
        k8s = create_k8s_client()
        try:
            dep = k8s.apps_v1.read_namespaced_deployment(deployment_name, namespace)
            selector = dep.spec.selector.match_labels if dep.spec and dep.spec.selector else {"app": deployment_name}

            svc = V1Service(
                metadata=V1ObjectMeta(name=deployment_name + "-svc"),
                spec=V1ServiceSpec(
                    selector=selector,
                    type=service_type,
                    ports=[V1ServicePort(port=port, target_port=port, protocol="TCP")],
                ),
            )
            result = k8s.core_v1.create_namespaced_service(namespace, svc)
            return json.dumps({
                "message": f"Service '{result.metadata.name}' created exposing '{deployment_name}'.",
                "name": result.metadata.name,
                "cluster_ip": result.spec.cluster_ip if result.spec else "",
            }, indent=2)
        except ApiException as e:
            return _api_error("expose_deployment", e)


def _service_to_dict(svc: Any) -> dict:
    md = svc.metadata
    spec = svc.spec
    status = svc.status
    ports = []
    if spec and spec.ports:
        for p in spec.ports:
            ports.append({
                "name": p.name or "", "port": p.port,
                "target_port": p.target_port, "protocol": p.protocol or "TCP",
                "node_port": p.node_port,
            })
    return {
        "name": md.name if md else "",
        "namespace": md.namespace if md else "",
        "type": spec.type if spec else "ClusterIP",
        "cluster_ip": spec.cluster_ip if spec else "",
        "external_ip": spec.external_i_ps if spec else [],
        "ports": ports,
        "selector": spec.selector if spec else {},
        "labels": md.labels if md else {},
        "creation_time": str(md.creation_timestamp) if md and md.creation_timestamp else "",
    }


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
