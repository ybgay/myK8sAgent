"""Log streaming tools for MCP server."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client.exceptions import ApiException
from kubernetes.watch import Watch

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_log_tools(mcp_server: Any) -> None:
    @mcp_server.tool()
    async def get_pod_logs_extended(
        name: str,
        namespace: str = "default",
        container: str | None = None,
        tail: int = 100,
        previous: bool = False,
        since_seconds: int | None = None,
        timestamps: bool = True,
    ) -> str:
        """Get logs from a pod with extended options (like kubectl logs).

        Args:
            name: Pod name.
            namespace: Target namespace.
            container: Container name (if multiple containers).
            tail: Lines from end of logs.
            previous: Get logs from previous container instance.
            since_seconds: Only show logs from last N seconds.
            timestamps: Include timestamps in output.
        """
        k8s = create_k8s_client()
        try:
            kwargs: dict[str, Any] = {"tail_lines": tail, "timestamps": timestamps}
            if container:
                kwargs["container"] = container
            if previous:
                kwargs["previous"] = True
            if since_seconds:
                kwargs["since_seconds"] = since_seconds

            logs = k8s.core_v1.read_namespaced_pod_log(name, namespace, **kwargs)
            # Return with metadata
            return json.dumps({
                "pod": name,
                "namespace": namespace,
                "container": container or "(default)",
                "previous": previous,
                "line_count": len(logs.split("\n")) if logs else 0,
                "logs": logs,
            }, indent=2)
        except ApiException as e:
            return _api_error("get_pod_logs_extended", e)

    @mcp_server.tool()
    async def stream_logs(
        name: str,
        namespace: str = "default",
        container: str | None = None,
        tail: int = 50,
    ) -> str:
        """Stream recent logs from a pod (snapshot mode — returns last N lines).

        Note: True log following requires WebSocket and is not supported
        via MCP's request-response model. Use get_pod_logs_extended with
        since_seconds for real-time monitoring.

        Args:
            name: Pod name.
            namespace: Target namespace.
            container: Container name.
            tail: Number of recent lines to fetch.
        """
        k8s = create_k8s_client()
        try:
            kwargs = {"tail_lines": tail, "timestamps": True}
            if container:
                kwargs["container"] = container
            logs = k8s.core_v1.read_namespaced_pod_log(name, namespace, **kwargs)
            return json.dumps({
                "pod": name,
                "namespace": namespace,
                "logs": logs,
                "note": "Use get_pod_logs_extended with since_seconds for time-based filtering.",
            }, indent=2)
        except ApiException as e:
            return _api_error("stream_logs", e)


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
