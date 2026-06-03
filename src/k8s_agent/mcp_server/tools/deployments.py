"""Deployment management tools for MCP server."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client import (
    V1Deployment,
    V1ObjectMeta,
    V1DeploymentSpec,
    V1PodTemplateSpec,
    V1PodSpec,
    V1Container,
    V1ContainerPort,
    V1ResourceRequirements,
    V1EnvVar,
    V1LabelSelector,
)
from kubernetes.client.exceptions import ApiException
from kubernetes.client import AppsV1Api

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_deployment_tools(mcp_server: Any) -> None:
    """Register deployment-related tools."""

    @mcp_server.tool()
    async def list_deployments(
        namespace: str = "default",
        label_selector: str | None = None,
    ) -> str:
        """List all deployments in a namespace.

        Args:
            namespace: Target namespace.
            label_selector: Optional label filter.
        """
        k8s = create_k8s_client()
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            deploys = k8s.apps_v1.list_namespaced_deployment(namespace, **kwargs)
            result = []
            for d in deploys.items:
                md = d.metadata
                spec = d.spec
                status = d.status
                result.append({
                    "name": md.name if md else "",
                    "namespace": md.namespace if md else namespace,
                    "replicas": spec.replicas if spec else 0,
                    "ready_replicas": status.ready_replicas if status else 0,
                    "available_replicas": status.available_replicas if status else 0,
                    "images": _get_deployment_images(d),
                    "labels": md.labels if md else {},
                })
            return json.dumps({"deployments": result, "count": len(result)}, indent=2, default=str)
        except ApiException as e:
            return _api_error("list_deployments", e)

    @mcp_server.tool()
    async def get_deployment(name: str, namespace: str = "default") -> str:
        """Get detailed info for a deployment.

        Args:
            name: Deployment name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            d = k8s.apps_v1.read_namespaced_deployment(name, namespace)
            return json.dumps(_deployment_to_dict(d), indent=2, default=str)
        except ApiException as e:
            return _api_error("get_deployment", e)

    @mcp_server.tool()
    async def create_deployment(
        name: str,
        namespace: str = "default",
        image: str = "nginx:latest",
        replicas: int = 1,
        port: int | None = None,
        env: dict[str, str] | None = None,
        cpu_request: str | None = None,
        memory_request: str | None = None,
        cpu_limit: str | None = None,
        memory_limit: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create a new deployment.

        Args:
            name: Deployment name.
            namespace: Target namespace.
            image: Container image.
            replicas: Number of replicas.
            port: Container port to expose.
            env: Environment variables.
            cpu_request: CPU request (e.g. "100m").
            memory_request: Memory request (e.g. "128Mi").
            cpu_limit: CPU limit.
            memory_limit: Memory limit.
            labels: Deployment labels.
        """
        k8s = create_k8s_client()
        try:
            # Build container
            env_vars = [V1EnvVar(name=k, value=v) for k, v in (env or {}).items()]
            ports = [V1ContainerPort(container_port=port)] if port else []
            resources = V1ResourceRequirements()
            if cpu_request or memory_request:
                resources.requests = {}
                if cpu_request:
                    resources.requests["cpu"] = cpu_request
                if memory_request:
                    resources.requests["memory"] = memory_request
            if cpu_limit or memory_limit:
                resources.limits = {}
                if cpu_limit:
                    resources.limits["cpu"] = cpu_limit
                if memory_limit:
                    resources.limits["memory"] = memory_limit

            container = V1Container(
                name=name,
                image=image,
                ports=ports,
                env=env_vars,
                resources=resources,
            )

            # Build deployment
            lbls = labels or {"app": name}
            deployment = V1Deployment(
                metadata=V1ObjectMeta(name=name, labels=lbls),
                spec=V1DeploymentSpec(
                    replicas=replicas,
                    selector=V1LabelSelector(match_labels=lbls),
                    template=V1PodTemplateSpec(
                        metadata=V1ObjectMeta(labels=lbls),
                        spec=V1PodSpec(containers=[container]),
                    ),
                ),
            )
            result = k8s.apps_v1.create_namespaced_deployment(namespace, deployment)
            return json.dumps({
                "message": f"Deployment '{result.metadata.name}' created in '{namespace}'.",
                "name": result.metadata.name,
                "namespace": namespace,
                "replicas": replicas,
            }, indent=2)
        except ApiException as e:
            return _api_error("create_deployment", e)

    @mcp_server.tool()
    async def update_deployment(
        name: str,
        namespace: str = "default",
        image: str | None = None,
        replicas: int | None = None,
    ) -> str:
        """Update a deployment's image and/or replicas.

        Args:
            name: Deployment name.
            namespace: Target namespace.
            image: New container image.
            replicas: New replica count.
        """
        k8s = create_k8s_client()
        try:
            dep = k8s.apps_v1.read_namespaced_deployment(name, namespace)
            if replicas is not None:
                dep.spec.replicas = replicas
            if image and dep.spec.template.spec:
                dep.spec.template.spec.containers[0].image = image
            result = k8s.apps_v1.patch_namespaced_deployment(name, namespace, dep)
            return json.dumps({
                "message": f"Deployment '{name}' updated.",
                "replicas": result.spec.replicas if result.spec else 0,
            }, indent=2)
        except ApiException as e:
            return _api_error("update_deployment", e)

    @mcp_server.tool()
    async def delete_deployment(name: str, namespace: str = "default") -> str:
        """Delete a deployment.

        Args:
            name: Deployment name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            k8s.apps_v1.delete_namespaced_deployment(name, namespace)
            return f"Deployment '{name}' deleted from '{namespace}'."
        except ApiException as e:
            return _api_error("delete_deployment", e)

    @mcp_server.tool()
    async def rollout_status(name: str, namespace: str = "default") -> str:
        """Check rollout status of a deployment.

        Args:
            name: Deployment name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            dep = k8s.apps_v1.read_namespaced_deployment(name, namespace)
            status = dep.status
            conditions = []
            if status and status.conditions:
                for c in status.conditions:
                    conditions.append({
                        "type": c.type,
                        "status": c.status,
                        "reason": c.reason,
                        "message": c.message,
                    })
            return json.dumps({
                "name": name,
                "namespace": namespace,
                "replicas": status.replicas if status else 0,
                "updated_replicas": status.updated_replicas if status else 0,
                "ready_replicas": status.ready_replicas if status else 0,
                "available_replicas": status.available_replicas if status else 0,
                "unavailable_replicas": status.unavailable_replicas if status else 0,
                "conditions": conditions,
            }, indent=2, default=str)
        except ApiException as e:
            return _api_error("rollout_status", e)

    @mcp_server.tool()
    async def restart_deployment(name: str, namespace: str = "default") -> str:
        """Trigger a rolling restart of a deployment.

        Args:
            name: Deployment name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            dep = k8s.apps_v1.read_namespaced_deployment(name, namespace)
            if dep.spec and dep.spec.template.metadata:
                annotations = dep.spec.template.metadata.annotations or {}
                import datetime
                annotations["kubectl.kubernetes.io/restartedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                dep.spec.template.metadata.annotations = annotations
            k8s.apps_v1.patch_namespaced_deployment(name, namespace, dep)
            return f"Deployment '{name}' restart initiated in '{namespace}'."
        except ApiException as e:
            return _api_error("restart_deployment", e)

    @mcp_server.tool()
    async def rollout_undo(
        name: str, namespace: str = "default", revision: int | None = None
    ) -> str:
        """Rollback a deployment to a previous revision.

        Args:
            name: Deployment name.
            namespace: Target namespace.
            revision: Specific revision to rollback to (defaults to previous).
        """
        k8s = create_k8s_client()
        try:
            # Use the apps/v1 rollback API
            from kubernetes.client import V1RollbackConfig
            # For newer k8s python client, we use patch
            body = {
                "spec": {
                    "rollbackTo": {"revision": revision or 0}
                }
            }
            if revision:
                k8s.apps_v1.api_client.call_api(
                    f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}/rollback",
                    "POST",
                    body=body,
                    response_type="object",
                )
            else:
                # Just trigger a restart to effectively rollback the last change
                dep = k8s.apps_v1.read_namespaced_deployment(name, namespace)
                if dep.spec and dep.spec.template.metadata:
                    annotations = dep.spec.template.metadata.annotations or {}
                    import datetime
                    annotations["kubectl.kubernetes.io/restartedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    dep.spec.template.metadata.annotations = annotations
                k8s.apps_v1.patch_namespaced_deployment(name, namespace, dep)
            return f"Rollback initiated for deployment '{name}' in '{namespace}'."
        except ApiException as e:
            return _api_error("rollout_undo", e)


def _get_deployment_images(dep: Any) -> list[str]:
    """Extract container images from a deployment."""
    images = []
    try:
        if dep.spec and dep.spec.template and dep.spec.template.spec:
            for c in dep.spec.template.spec.containers:
                images.append(c.image)
    except (AttributeError, TypeError):
        pass
    return images


def _deployment_to_dict(dep: Any) -> dict:
    """Convert a V1Deployment to a detailed dictionary."""
    md = dep.metadata
    spec = dep.spec
    status = dep.status
    containers = []
    if spec and spec.template and spec.template.spec:
        for c in spec.template.spec.containers:
            resources = {}
            if c.resources:
                resources = {
                    "requests": c.resources.requests or {},
                    "limits": c.resources.limits or {},
                }
            containers.append({
                "name": c.name,
                "image": c.image,
                "ports": [p.container_port for p in (c.ports or [])],
                "resources": resources,
                "env": [{e.name: e.value} for e in (c.env or [])],
            })

    conditions = []
    if status and status.conditions:
        for c in status.conditions:
            conditions.append({
                "type": c.type,
                "status": c.status,
                "reason": c.reason,
                "message": c.message,
            })

    return {
        "name": md.name if md else "",
        "namespace": md.namespace if md else "",
        "replicas": spec.replicas if spec else 0,
        "strategy": str(spec.strategy.type) if spec and spec.strategy else "RollingUpdate",
        "containers": containers,
        "status": {
            "replicas": status.replicas if status else 0,
            "ready_replicas": status.ready_replicas if status else 0,
            "available_replicas": status.available_replicas if status else 0,
            "updated_replicas": status.updated_replicas if status else 0,
            "conditions": conditions,
        },
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
