"""Topology/Visualization tool — generates K8s resource relationship graphs.

Returns structured nodes + edges data that can be rendered as a
force-directed graph, showing relationships between:
- Pod → Node (runs on)
- Pod → Deployment/StatefulSet/DaemonSet/ReplicaSet (owned by)
- Pod → Service (selected by / traffic routed)
- Deployment → ReplicaSet (manages)
- Service → Pod (routes to, via selector)
- Pod → PVC (mounts)
- Ingress → Service (routes to)
"""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client.exceptions import ApiException

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def register_topology_tools(mcp_server: Any) -> None:
    """Register topology/visualization tool."""

    @mcp_server.tool()
    async def get_topology(
        namespace: str = "default",
        include_nodes: bool = True,
        include_services: bool = True,
        include_ingresses: bool = False,
        include_metrics: bool = False,
    ) -> str:
        """Generate a topology graph of Kubernetes resources for visualization.

        Returns a JSON structure with `nodes` and `edges` that can be rendered
        as a relationship diagram (force-directed graph, tree, etc.).

        Each node has: id, label, type, group, metadata
        Each edge has: from, to, label, type

        Node types (groups): pod, deployment, statefulset, daemonset, replicaset,
                              service, node, ingress, pvc

        Edge types: runs_on, owned_by, selects, routes_to, mounts

        Args:
            namespace: The namespace to generate topology for.
            include_nodes: Include cluster nodes in the graph.
            include_services: Include services and their pod selectors.
            include_ingresses: Include ingresses (if any).
            include_metrics: Include CPU/memory metrics on pod nodes.
        """
        k8s = create_k8s_client()
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        node_ids: set[str] = set()

        def add_node(
            node_id: str,
            label: str,
            node_type: str,
            group: str,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            if node_id not in node_ids:
                node_ids.add(node_id)
                node_data: dict[str, Any] = {
                    "id": node_id,
                    "label": label,
                    "type": node_type,
                    "group": group,
                }
                if metadata:
                    node_data["metadata"] = metadata
                nodes.append(node_data)

        def add_edge(
            from_id: str, to_id: str, edge_type: str, label: str = ""
        ) -> None:
            edges.append({
                "from": from_id,
                "to": to_id,
                "type": edge_type,
                "label": label,
            })

        try:
            # ── 1. Cluster Nodes ──
            if include_nodes:
                try:
                    k8s_nodes = k8s.core_v1.list_node()
                    for node in k8s_nodes.items:
                        node_name = node.metadata.name if node.metadata else "unknown"
                        nid = f"node/{node_name}"
                        status = "Ready"
                        if node.status and node.status.conditions:
                            for c in node.status.conditions:
                                if c.type == "Ready":
                                    status = c.status
                        add_node(
                            nid, node_name, "node", "node",
                            metadata={
                                "status": status,
                                "version": (
                                    node.status.node_info.kubelet_version
                                    if node.status and node.status.node_info
                                    else ""
                                ),
                                "capacity_cpu": (
                                    node.status.capacity.get("cpu", "")
                                    if node.status and node.status.capacity
                                    else ""
                                ),
                                "capacity_memory": (
                                    node.status.capacity.get("memory", "")
                                    if node.status and node.status.capacity
                                    else ""
                                ),
                            },
                        )
                except ApiException:
                    pass

            # ── 2. Pods ──
            pods = k8s.core_v1.list_namespaced_pod(namespace)
            pod_metrics = {}
            if include_metrics:
                try:
                    resp = k8s.core_v1.api_client.call_api(
                        f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods",
                        "GET",
                        response_type="object",
                    )
                    for item in resp.get("items", []):
                        pname = item["metadata"]["name"]
                        usage = {}
                        for c in item.get("containers", []):
                            cu = c.get("usage", {})
                            usage[c["name"]] = {
                                "cpu": cu.get("cpu", ""),
                                "memory": cu.get("memory", ""),
                            }
                        pod_metrics[pname] = usage
                except ApiException:
                    pass

            # ── 3. Deployments, StatefulSets, DaemonSets ──
            controllers: list[dict[str, Any]] = []

            try:
                deploys = k8s.apps_v1.list_namespaced_deployment(namespace)
                for d in deploys.items:
                    dname = d.metadata.name if d.metadata else "unknown"
                    did = f"deployment/{namespace}/{dname}"
                    replicas = d.spec.replicas if d.spec else 0
                    ready = d.status.ready_replicas if d.status else 0
                    add_node(
                        did, dname, "deployment", "deployment",
                        metadata={"replicas": replicas, "ready": ready},
                    )
                    controllers.append({
                        "id": did, "name": dname, "kind": "Deployment",
                        "selector": (
                            d.spec.selector.match_labels
                            if d.spec and d.spec.selector
                            else {}
                        ),
                    })
            except ApiException:
                pass

            try:
                sts_list = k8s.apps_v1.list_namespaced_stateful_set(namespace)
                for s in sts_list.items:
                    sname = s.metadata.name if s.metadata else "unknown"
                    sid = f"statefulset/{namespace}/{sname}"
                    add_node(sid, sname, "statefulset", "statefulset",
                             metadata={"replicas": s.spec.replicas if s.spec else 0})
                    controllers.append({
                        "id": sid, "name": sname, "kind": "StatefulSet",
                        "selector": (
                            s.spec.selector.match_labels
                            if s.spec and s.spec.selector
                            else {}
                        ),
                    })
            except ApiException:
                pass

            try:
                ds_list = k8s.apps_v1.list_namespaced_daemon_set(namespace)
                for ds in ds_list.items:
                    dsname = ds.metadata.name if ds.metadata else "unknown"
                    dsid = f"daemonset/{namespace}/{dsname}"
                    add_node(dsid, dsname, "daemonset", "daemonset")
                    controllers.append({
                        "id": dsid, "name": dsname, "kind": "DaemonSet",
                        "selector": (
                            ds.spec.selector.match_labels
                            if ds.spec and ds.spec.selector
                            else {}
                        ),
                    })
            except ApiException:
                pass

            # ── 4. Services ──
            services_data: list[dict[str, Any]] = []
            if include_services:
                try:
                    svcs = k8s.core_v1.list_namespaced_service(namespace)
                    for svc in svcs.items:
                        sname = svc.metadata.name if svc.metadata else "unknown"
                        sid = f"service/{namespace}/{sname}"
                        stype = svc.spec.type if svc.spec else "ClusterIP"
                        cluster_ip = svc.spec.cluster_ip if svc.spec else ""
                        ports = []
                        if svc.spec and svc.spec.ports:
                            ports = [
                                f"{p.port}:{p.target_port}/{p.protocol or 'TCP'}"
                                for p in svc.spec.ports
                            ]
                        add_node(
                            sid, sname, "service", "service",
                            metadata={"type": stype, "cluster_ip": cluster_ip, "ports": ports},
                        )
                        services_data.append({
                            "id": sid, "name": sname,
                            "selector": svc.spec.selector if svc.spec else {},
                        })
                except ApiException:
                    pass

            # ── 5. Ingresses ──
            if include_ingresses:
                try:
                    from kubernetes.client import NetworkingV1Api
                    net = NetworkingV1Api(k8s.api_client)
                    ingresses = net.list_namespaced_ingress(namespace)
                    for ing in ingresses.items:
                        iname = ing.metadata.name if ing.metadata else "unknown"
                        iid = f"ingress/{namespace}/{iname}"
                        add_node(iid, iname, "ingress", "ingress")
                except ApiException:
                    pass

            # ── 6. Build relationships (edges) ──

            for pod in pods.items:
                pname = pod.metadata.name if pod.metadata else "unknown"
                pnamespace = pod.metadata.namespace if pod.metadata else namespace
                pid = f"pod/{pnamespace}/{pname}"
                phase = pod.status.phase if pod.status else "Unknown"
                node_name = pod.spec.node_name if pod.spec else ""
                labels = pod.metadata.labels if pod.metadata else {}
                owner_refs = pod.metadata.owner_references if pod.metadata else []

                # Pod metadata
                container_list = []
                if pod.spec and pod.spec.containers:
                    container_list = [
                        {"name": c.name, "image": c.image}
                        for c in pod.spec.containers
                    ]

                metrics_data = pod_metrics.get(pname)

                add_node(
                    pid, pname, "pod", "pod",
                    metadata={
                        "phase": phase,
                        "namespace": pnamespace,
                        "containers": container_list,
                        "labels": labels,
                        "metrics": metrics_data,
                        "restart_count": (
                            sum(
                                cs.restart_count or 0
                                for cs in (pod.status.container_statuses or [])
                            )
                            if pod.status and pod.status.container_statuses
                            else 0
                        ),
                    },
                )

                # Edge: Pod → Node (runs_on)
                if node_name and include_nodes:
                    add_edge(pid, f"node/{node_name}", "runs_on", "runs on")

                # Edge: Pod → Owner (owned_by)
                if owner_refs:
                    for ref in owner_refs:
                        owner_kind = ref.kind.lower()
                        owner_name = ref.name
                        owner_id = f"{owner_kind}/{pnamespace}/{owner_name}"
                        add_edge(pid, owner_id, "owned_by", f"owned by {ref.kind}")

                # Edge: Service → Pod (selects / routes_to)
                if include_services:
                    for svc_data in services_data:
                        if _labels_match(svc_data["selector"], labels):
                            add_edge(
                                svc_data["id"], pid, "routes_to",
                                f"routes to",
                            )

                # Edge: PVC → Pod (mounts)
                if pod.spec and pod.spec.volumes:
                    for vol in pod.spec.volumes:
                        if vol.persistent_volume_claim:
                            pvc_id = f"pvc/{pnamespace}/{vol.persistent_volume_claim.claim_name}"
                            add_node(pvc_id, vol.persistent_volume_claim.claim_name,
                                     "pvc", "pvc")
                            add_edge(pid, pvc_id, "mounts", "mounts")

            # Edge: Ingress → Service
            if include_ingresses:
                try:
                    from kubernetes.client import NetworkingV1Api
                    net = NetworkingV1Api(k8s.api_client)
                    ingresses = net.list_namespaced_ingress(namespace)
                    for ing in ingresses.items:
                        iid = f"ingress/{namespace}/{ing.metadata.name}"
                        if ing.spec and ing.spec.rules:
                            for rule in ing.spec.rules:
                                if rule.http:
                                    for path in rule.http.paths:
                                        if path.backend and path.backend.service:
                                            svc_name = path.backend.service.name
                                            svc_id = f"service/{namespace}/{svc_name}"
                                            add_edge(iid, svc_id, "routes_to", "routes to")
                except ApiException:
                    pass

            # ── 7. Build result ──
            groups = list(set(n["group"] for n in nodes))
            result = {
                "topology": {
                    "namespace": namespace,
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                    "groups": groups,
                    "nodes": nodes,
                    "edges": edges,
                },
                # Include a flat summary for the chat response
                "summary": _build_summary(nodes, edges, namespace),
            }
            return json.dumps(result, indent=2, default=str)

        except ApiException as e:
            return _api_error("get_topology", e)


def _labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    """Check if labels match a selector (all selector keys must match)."""
    if not selector:
        return False
    for key, value in selector.items():
        if labels.get(key) != value:
            return False
    return True


def _build_summary(
    nodes: list[dict], edges: list[dict], namespace: str
) -> str:
    """Build a human-readable summary of the topology."""
    groups: dict[str, int] = {}
    for n in nodes:
        g = n.get("group", "unknown")
        groups[g] = groups.get(g, 0) + 1

    parts = [f"Namespace: {namespace}"]
    for g, count in sorted(groups.items()):
        parts.append(f"  {g}: {count}")
    parts.append(f"  relationships: {len(edges)}")

    problem_pods = [
        n for n in nodes
        if n.get("group") == "pod"
        and n.get("metadata", {}).get("phase") not in ("Running", "Succeeded")
    ]
    if problem_pods:
        parts.append(f"\n⚠ Non-healthy pods: {len(problem_pods)}")
        for p in problem_pods[:5]:
            parts.append(f"  - {p['label']}: {p['metadata']['phase']}")

    return "\n".join(parts)


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
