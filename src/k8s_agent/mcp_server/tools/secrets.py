"""Secret management tools for MCP server (values redacted by default)."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client import V1Secret, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_secret_tools(mcp_server: Any) -> None:
    @mcp_server.tool()
    async def list_secrets(namespace: str = "default") -> str:
        """List all Secrets in a namespace (names only, values redacted).

        Args:
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            secrets = k8s.core_v1.list_namespaced_secret(namespace)
            result = [
                {
                    "name": s.metadata.name if s.metadata else "",
                    "namespace": namespace,
                    "type": s.type or "Opaque",
                    "data_keys": list(s.data.keys()) if s.data else [],
                }
                for s in secrets.items
            ]
            return json.dumps({"secrets": result, "count": len(result)}, indent=2)
        except ApiException as e:
            return _api_error("list_secrets", e)

    @mcp_server.tool()
    async def get_secret(
        name: str, namespace: str = "default", reveal: bool = False
    ) -> str:
        """Get a Secret's metadata. Values are REDACTED by default.

        Args:
            name: Secret name.
            namespace: Target namespace.
            reveal: If True, show decoded values (use with caution!).
        """
        k8s = create_k8s_client()
        try:
            s = k8s.core_v1.read_namespaced_secret(name, namespace)
            import base64
            data_display = {}
            if s.data:
                for k, v in s.data.items():
                    if reveal:
                        try:
                            data_display[k] = base64.b64decode(v).decode("utf-8")
                        except Exception:
                            data_display[k] = f"<binary:{len(v)}bytes>"
                    else:
                        data_display[k] = f"<redacted:{len(v)}chars>"
            return json.dumps({
                "name": s.metadata.name if s.metadata else "",
                "namespace": namespace,
                "type": s.type,
                "data": data_display,
                "labels": s.metadata.labels if s.metadata else {},
            }, indent=2)
        except ApiException as e:
            return _api_error("get_secret", e)

    @mcp_server.tool()
    async def create_secret(
        name: str,
        namespace: str = "default",
        string_data: dict[str, str] | None = None,
        secret_type: str = "Opaque",
    ) -> str:
        """Create a Secret with string data.

        Args:
            name: Secret name.
            namespace: Target namespace.
            string_data: Key-value pairs (will be encoded automatically).
            secret_type: Secret type (Opaque, kubernetes.io/tls, etc.).
        """
        k8s = create_k8s_client()
        try:
            s = V1Secret(
                metadata=V1ObjectMeta(name=name),
                type=secret_type,
                string_data=string_data or {},
            )
            result = k8s.core_v1.create_namespaced_secret(namespace, s)
            return f"Secret '{result.metadata.name}' created in '{namespace}'."
        except ApiException as e:
            return _api_error("create_secret", e)

    @mcp_server.tool()
    async def delete_secret(name: str, namespace: str = "default") -> str:
        """Delete a Secret.

        Args:
            name: Secret name.
            namespace: Target namespace.
        """
        k8s = create_k8s_client()
        try:
            k8s.core_v1.delete_namespaced_secret(name, namespace)
            return f"Secret '{name}' deleted from '{namespace}'."
        except ApiException as e:
            return _api_error("delete_secret", e)


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
