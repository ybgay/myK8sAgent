"""Metrics tools for MCP server (requires metrics-server in cluster)."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client.exceptions import ApiException

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_metrics_tools(mcp_server: Any) -> None:
    @mcp_server.tool()
    async def get_cluster_info() -> str:
        """Get basic cluster information (version, API server)."""
        k8s = create_k8s_client()
        try:
            # Try the /version endpoint
            resp = k8s.core_v1.api_client.call_api(
                "/version", "GET", response_type="object"
            )
            # Convert to dict if needed
            version_info = resp if isinstance(resp, dict) else resp.to_dict() if hasattr(resp, 'to_dict') else str(resp)
            return json.dumps({
                "cluster_version": version_info,
                "connected": True,
            }, indent=2, default=str)
        except ApiException as e:
            return json.dumps({
                "connected": False,
                "error": str(e.reason or str(e)),
            }, indent=2)

    @mcp_server.tool()
    async def get_node_metrics(name: str | None = None) -> str:
        """Get CPU and memory metrics for nodes.

        Requires metrics-server to be installed in the cluster.

        Args:
            name: Optional node name. If omitted, returns all nodes.
        """
        k8s = create_k8s_client()
        try:
            # metrics.k8s.io/v1beta1 NodeMetrics
            if name:
                path = f"/apis/metrics.k8s.io/v1beta1/nodes/{name}"
            else:
                path = "/apis/metrics.k8s.io/v1beta1/nodes"

            resp = k8s.core_v1.api_client.call_api(path, "GET", response_type="object")
            return json.dumps(resp, indent=2, default=str)
        except ApiException as e:
            if e.status == 404 or e.status == 503:
                return json.dumps({
                    "error": True,
                    "message": "Metrics API not available. Is metrics-server installed?",
                    "hint": "Run: kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml",
                })
            return _api_error("get_node_metrics", e)

    @mcp_server.tool()
    async def get_pod_metrics(
        namespace: str = "default", name: str | None = None
    ) -> str:
        """Get CPU and memory metrics for pods.

        Requires metrics-server.

        Args:
            namespace: Target namespace.
            name: Optional pod name. If omitted, returns all pods in namespace.
        """
        k8s = create_k8s_client()
        try:
            if name:
                path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods/{name}"
            else:
                path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods"

            resp = k8s.core_v1.api_client.call_api(path, "GET", response_type="object")
            return json.dumps(resp, indent=2, default=str)
        except ApiException as e:
            if e.status == 404 or e.status == 503:
                return json.dumps({
                    "error": True,
                    "message": "Metrics API not available. Is metrics-server installed?",
                })
            return _api_error("get_pod_metrics", e)

    @mcp_server.tool()
    async def api_resources(api_group: str | None = None) -> str:
        """List available API resources on the cluster (like kubectl api-resources).

        Args:
            api_group: Optional API group to filter.
        """
        k8s = create_k8s_client()
        try:
            # Use discovery API
            from kubernetes.client import ApisApi
            apis_api = ApisApi(k8s.api_client)

            if api_group:
                path = f"/apis/{api_group}"
            else:
                # Get core API resources
                core = k8s.core_v1.api_client.call_api(
                    "/api/v1", "GET", response_type="object"
                )
                return json.dumps(core, indent=2, default=str)

            resp = apis_api.api_client.call_api(path, "GET", response_type="object")
            return json.dumps(resp, indent=2, default=str)
        except ApiException as e:
            return _api_error("api_resources", e)

    @mcp_server.tool()
    async def apply_manifest(manifest: str) -> str:
        """Apply a YAML manifest to the cluster.

        Supports multi-document YAML (separated by ---).

        Args:
            manifest: YAML manifest string.
        """
        import yaml
        k8s = create_k8s_client()
        try:
            docs = list(yaml.safe_load_all(manifest))
            results = []
            for doc in docs:
                if doc is None:
                    continue
                kind = doc.get("kind", "Unknown")
                name = doc.get("metadata", {}).get("name", "unnamed")
                namespace = doc.get("metadata", {}).get("namespace", "default")

                # Use the generic dynamic client approach
                try:
                    k8s.core_v1.api_client.call_api(
                        f"/api/v1/namespaces/{namespace}/{kind.lower()}s",
                        "POST",
                        body=doc,
                        response_type="object",
                    )
                    results.append(f"Created {kind}/{name} in {namespace}")
                except ApiException:
                    # Try apps/v1 for deployments etc.
                    try:
                        k8s.core_v1.api_client.call_api(
                            f"/apis/apps/v1/namespaces/{namespace}/{kind.lower()}s",
                            "POST",
                            body=doc,
                            response_type="object",
                        )
                        results.append(f"Created {kind}/{name} in {namespace}")
                    except ApiException as e2:
                        results.append(f"Failed to apply {kind}/{name}: {e2.reason}")

            return json.dumps({"results": results}, indent=2)
        except Exception as e:
            logger.error("apply_manifest_error", error=str(e))
            return json.dumps({"error": True, "message": str(e)})


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
