"""Pod management tools for MCP server."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client import V1Pod, V1ObjectMeta, V1PodSpec, V1Container, V1ContainerPort
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_pod_tools(mcp_server: Any) -> None:
    """Register pod-related tools on the MCP server."""

    @mcp_server.tool()
    async def list_pods(
        namespace: str = "default",
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> str:
        """List all pods in a namespace.

        Args:
            namespace: The namespace to list pods from.
            label_selector: Optional label selector filter.
            field_selector: Optional field selector filter.
        """
        k8s = create_k8s_client()
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if field_selector:
                kwargs["field_selector"] = field_selector

            pod_list = k8s.core_v1.list_namespaced_pod(namespace, **kwargs)
            pods = []
            for pod in pod_list.items:
                md = pod.metadata
                status = pod.status
                containers = []
                if status and status.container_statuses:
                    for cs in status.container_statuses:
                        containers.append({
                            "name": cs.name,
                            "ready": cs.ready,
                            "restart_count": cs.restart_count,
                            "state": _get_container_state(cs),
                        })
                pods.append({
                    "name": md.name if md else "",
                    "namespace": md.namespace if md else namespace,
                    "phase": status.phase if status else "Unknown",
                    "node": pod.spec.node_name if pod.spec else "",
                    "containers": containers,
                    "labels": md.labels if md else {},
                })
            return json.dumps({"pods": pods, "count": len(pods)}, indent=2, default=str)
        except ApiException as e:
            return _api_error("list_pods", e)

    @mcp_server.tool()
    async def get_pod(name: str, namespace: str = "default") -> str:
        """Get detailed information about a specific pod.

        Args:
            name: The pod name.
            namespace: The namespace.
        """
        k8s = create_k8s_client()
        try:
            pod = k8s.core_v1.read_namespaced_pod(name, namespace)
            return json.dumps(_pod_to_dict(pod), indent=2, default=str)
        except ApiException as e:
            return _api_error("get_pod", e)

    @mcp_server.tool()
    async def describe_pod(name: str, namespace: str = "default") -> str:
        """Get a comprehensive description of a pod (like kubectl describe).

        Args:
            name: The pod name.
            namespace: The namespace.
        """
        k8s = create_k8s_client()
        try:
            pod = k8s.core_v1.read_namespaced_pod(name, namespace)
            # Also get events for this pod
            events = k8s.core_v1.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={name},involvedObject.kind=Pod",
            )

            pod_info = _pod_to_dict(pod)
            pod_info["events"] = []
            for evt in events.items:
                pod_info["events"].append({
                    "type": evt.type,
                    "reason": evt.reason,
                    "message": evt.message,
                    "first_timestamp": str(evt.first_timestamp) if evt.first_timestamp else "",
                    "last_timestamp": str(evt.last_timestamp) if evt.last_timestamp else "",
                    "count": evt.count,
                })
            return json.dumps(pod_info, indent=2, default=str)
        except ApiException as e:
            return _api_error("describe_pod", e)

    @mcp_server.tool()
    async def create_pod(
        name: str,
        namespace: str = "default",
        image: str = "nginx:latest",
        port: int | None = None,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Create a simple pod.

        Args:
            name: The pod name.
            namespace: Target namespace.
            image: Container image.
            port: Optional container port to expose.
            labels: Optional labels.
            env: Optional environment variables.
        """
        k8s = create_k8s_client()
        try:
            env_list = []
            if env:
                from kubernetes.client import V1EnvVar
                env_list = [V1EnvVar(name=k, value=v) for k, v in env.items()]

            ports = []
            if port:
                ports = [V1ContainerPort(container_port=port)]

            container = V1Container(name=name, image=image, ports=ports, env=env_list)
            pod_spec = V1PodSpec(containers=[container])
            pod = V1Pod(
                metadata=V1ObjectMeta(name=name, labels=labels or {}),
                spec=pod_spec,
            )
            result = k8s.core_v1.create_namespaced_pod(namespace, pod)
            return f"Pod '{result.metadata.name}' created in namespace '{namespace}'."
        except ApiException as e:
            return _api_error("create_pod", e)

    @mcp_server.tool()
    async def delete_pod(name: str, namespace: str = "default", grace_period: int | None = None) -> str:
        """Delete a pod.

        Args:
            name: The pod name.
            namespace: The namespace.
            grace_period: Grace period in seconds.
        """
        k8s = create_k8s_client()
        try:
            kwargs = {}
            if grace_period is not None:
                kwargs["grace_period_seconds"] = grace_period
            k8s.core_v1.delete_namespaced_pod(name, namespace, **kwargs)
            return f"Pod '{name}' deletion initiated in namespace '{namespace}'."
        except ApiException as e:
            return _api_error("delete_pod", e)

    @mcp_server.tool()
    async def get_pod_logs(
        name: str,
        namespace: str = "default",
        container: str | None = None,
        tail: int | None = 100,
        previous: bool = False,
        since_seconds: int | None = None,
        timestamps: bool = False,
    ) -> str:
        """Get logs from a pod container.

        Args:
            name: The pod name.
            namespace: The namespace.
            container: Container name (if pod has multiple containers).
            tail: Number of lines from the end.
            previous: Get logs from the previous terminated container.
            since_seconds: Get logs from the last N seconds.
            timestamps: Include timestamps.
        """
        k8s = create_k8s_client()
        try:
            kwargs = {}
            if container:
                kwargs["container"] = container
            if tail is not None:
                kwargs["tail_lines"] = tail
            if previous:
                kwargs["previous"] = True
            if since_seconds:
                kwargs["since_seconds"] = since_seconds
            if timestamps:
                kwargs["timestamps"] = True

            logs = k8s.core_v1.read_namespaced_pod_log(name, namespace, **kwargs)
            return logs
        except ApiException as e:
            return _api_error("get_pod_logs", e)

    @mcp_server.tool()
    async def exec_in_pod(
        name: str,
        namespace: str = "default",
        command: str = "ls",
        container: str | None = None,
    ) -> str:
        """Execute a command in a pod container (like kubectl exec).

        Args:
            name: The pod name.
            namespace: The namespace.
            command: The command to execute.
            container: Container name (if multiple containers).
        """
        k8s = create_k8s_client()
        try:
            cmd_parts = command.split()
            exec_kwargs = {
                "command": cmd_parts,
                "stdout": True,
                "stderr": True,
            }
            if container:
                exec_kwargs["container"] = container

            resp = stream(
                k8s.core_v1.connect_get_namespaced_pod_exec,
                name,
                namespace,
                **exec_kwargs,
            )
            return resp or "(no output)"
        except ApiException as e:
            return _api_error("exec_in_pod", e)


def _get_container_state(cs: Any) -> str:
    """Extract the current container state as a string."""
    if not hasattr(cs, 'state') or cs.state is None:
        return "unknown"
    state = cs.state
    if state.running:
        return "running"
    elif state.waiting:
        return f"waiting({state.waiting.reason})"
    elif state.terminated:
        return f"terminated({state.terminated.reason})"
    return "unknown"


def _pod_to_dict(pod: Any) -> dict:
    """Convert a V1Pod to a detailed dictionary."""
    md = pod.metadata
    status = pod.status
    spec = pod.spec

    containers = []
    if status and status.container_statuses:
        for cs in status.container_statuses:
            containers.append({
                "name": cs.name,
                "image": cs.image,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "state": _get_container_state(cs),
            })

    conditions = []
    if status and status.conditions:
        for cond in status.conditions:
            conditions.append({
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": cond.message,
            })

    return {
        "name": md.name if md else "",
        "namespace": md.namespace if md else "",
        "phase": status.phase if status else "Unknown",
        "node": spec.node_name if spec else "",
        "ip": status.pod_ip if status else "",
        "host_ip": status.host_ip if status else "",
        "labels": md.labels if md else {},
        "annotations": md.annotations if md else {},
        "containers": containers,
        "conditions": conditions,
        "creation_time": str(md.creation_timestamp) if md and md.creation_timestamp else "",
        "start_time": str(status.start_time) if status and status.start_time else "",
    }


def _api_error(tool: str, error: ApiException) -> str:
    """Format API error as JSON."""
    logger.error("k8s_tool_error", tool=tool, status=error.status)
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
