"""Event tools for MCP server."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client.exceptions import ApiException

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_event_tools(mcp_server: Any) -> None:
    @mcp_server.tool()
    async def list_events(
        namespace: str = "default",
        field_selector: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> str:
        """List Kubernetes events with filtering.

        Args:
            namespace: Target namespace.
            field_selector: Field selector (e.g. "involvedObject.name=my-pod").
            event_type: Filter by type (Normal or Warning).
            limit: Max number of events.
        """
        k8s = create_k8s_client()
        try:
            kwargs = {"limit": limit}
            if field_selector:
                kwargs["field_selector"] = field_selector

            events = k8s.core_v1.list_namespaced_event(namespace, **kwargs)
            result = []
            for e in events.items:
                if event_type and e.type != event_type:
                    continue
                result.append({
                    "type": e.type,
                    "reason": e.reason,
                    "message": e.message,
                    "source": f"{e.source.component}/{e.source.host}" if e.source else "",
                    "involved_object": f"{e.involved_object.kind}/{e.involved_object.name}" if e.involved_object else "",
                    "first_seen": str(e.first_timestamp) if e.first_timestamp else "",
                    "last_seen": str(e.last_timestamp) if e.last_timestamp else "",
                    "count": e.count,
                })
            return json.dumps({"events": result, "count": len(result)}, indent=2, default=str)
        except ApiException as e:
            return _api_error("list_events", e)

    @mcp_server.tool()
    async def get_resource_events(
        kind: str, name: str, namespace: str = "default"
    ) -> str:
        """Get events for a specific Kubernetes resource.

        Args:
            kind: Resource kind (Pod, Deployment, Service, etc.).
            name: Resource name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            field = f"involvedObject.name={name},involvedObject.kind={kind}"
            events = k8s.core_v1.list_namespaced_event(
                namespace, field_selector=field
            )
            result = [
                {
                    "type": e.type, "reason": e.reason,
                    "message": e.message,
                    "first_seen": str(e.first_timestamp) if e.first_timestamp else "",
                    "last_seen": str(e.last_timestamp) if e.last_timestamp else "",
                    "count": e.count,
                }
                for e in events.items
            ]
            return json.dumps({
                "resource": f"{kind}/{name}", "namespace": namespace,
                "events": result, "count": len(result),
            }, indent=2, default=str)
        except ApiException as e:
            return _api_error("get_resource_events", e)


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
