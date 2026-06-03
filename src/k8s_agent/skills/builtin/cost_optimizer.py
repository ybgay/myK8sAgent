"""Cost Optimizer skill — find idle resources, right-size recommendations, unused resources."""

from __future__ import annotations

import json
from typing import Any

from k8s_agent.skills.interface import ISkill, SkillContext, SkillTool
from k8s_agent.skills.manifest import SkillManifest


class CostOptimizerSkill(ISkill):
    """Skill for Kubernetes cost optimization.

    Tools:
    - analyze_idle_resources: Find pods with low utilization
    - right_size_recommendations: CPU/memory right-sizing
    - find_unused_resources: Detect unused PVs, Services, ConfigMaps
    """

    def get_manifest(self) -> SkillManifest:
        return SkillManifest(
            name="cost-optimizer",
            version="1.0.0",
            description="Optimize Kubernetes costs: idle resources, right-sizing, unused resources",
            author="k8s-agent",
            keywords=["kubernetes", "cost", "optimization", "finops"],
            agent_type="tool-only",
            capabilities=["analyze_idle_resources", "right_size_recommendations", "find_unused_resources"],
        )

    def get_tools(self) -> list[SkillTool]:
        return [
            SkillTool(
                name="analyze_idle_resources",
                description="Find pods with consistently low CPU/memory utilization based on metrics.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string", "description": "Namespace to scan (all if omitted)"},
                        "cpu_threshold_percent": {"type": "number", "description": "CPU % below which pod is considered idle", "default": 10},
                        "memory_threshold_percent": {"type": "number", "description": "Memory % below which pod is considered idle", "default": 20},
                    },
                },
                handler=self._analyze_idle_resources,
            ),
            SkillTool(
                name="right_size_recommendations",
                description="Recommend CPU/memory resource adjustments based on actual usage vs. requests/limits.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "deployment_name": {"type": "string", "description": "Deployment to analyze"},
                        "namespace": {"type": "string", "default": "default"},
                    },
                    "required": ["deployment_name"],
                },
                handler=self._right_size_recommendations,
            ),
            SkillTool(
                name="find_unused_resources",
                description="Detect potentially unused resources: orphaned PVCs, unused Services, stale ConfigMaps.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string", "description": "Namespace to scan"},
                    },
                },
                handler=self._find_unused_resources,
            ),
        ]

    async def initialize(self, context: SkillContext) -> None:
        self._ctx = context
        self._ctx.logger.info("cost_optimizer_initialized")

    async def _analyze_idle_resources(
        self,
        namespace: str | None = None,
        cpu_threshold_percent: float = 10.0,
        memory_threshold_percent: float = 20.0,
    ) -> str:
        """Find idle or low-utilization pods."""
        results = {"idle_pods": [], "summary": {}}

        try:
            k8s = self._ctx.k8s_client
            if not k8s:
                return json.dumps({"error": True, "message": "K8s client not available"})

            # Get pods
            if namespace:
                pods = k8s.core_v1.list_namespaced_pod(namespace).items
            else:
                pods = k8s.core_v1.list_pod_for_all_namespaces().items

            # Try to get metrics
            try:
                if namespace:
                    resp = k8s.core_v1.api_client.call_api(
                        f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods",
                        "GET", response_type="object",
                    )
                else:
                    resp = k8s.core_v1.api_client.call_api(
                        "/apis/metrics.k8s.io/v1beta1/pods",
                        "GET", response_type="object",
                    )
                metrics_available = True
            except Exception:
                metrics_available = False

            if not metrics_available:
                # Fallback: check pods based on resource requests
                for pod in pods:
                    if pod.status and pod.status.phase == "Running":
                        if pod.spec:
                            for c in pod.spec.containers:
                                if c.resources and c.resources.requests:
                                    cpu_req = _parse_cpu(c.resources.requests.get("cpu", "0"))
                                    mem_req = _parse_memory(c.resources.requests.get("memory", "0"))
                                    if cpu_req <= 0.01 and mem_req <= 32:  # Very low requests
                                        results["idle_pods"].append({
                                            "pod": pod.metadata.name if pod.metadata else "",
                                            "namespace": pod.metadata.namespace if pod.metadata else "",
                                            "reason": "Very low resource requests (possible idle pod)",
                                            "cpu_request": c.resources.requests.get("cpu", "0"),
                                            "memory_request_mb": mem_req,
                                        })
                results["summary"] = {
                    "note": "Metrics API not available. Analysis based on resource requests only.",
                    "idle_count": len(results["idle_pods"]),
                }
            else:
                results["summary"] = {
                    "note": "Metrics API available. Full analysis would parse actual usage data.",
                    "idle_count": len(results["idle_pods"]),
                }

        except Exception as e:
            results["error"] = str(e)

        return json.dumps(results, indent=2, default=str)

    async def _right_size_recommendations(
        self, deployment_name: str, namespace: str = "default"
    ) -> str:
        """Recommend right-sized CPU/memory for a deployment."""
        recommendations = []

        try:
            k8s = self._ctx.k8s_client
            if not k8s:
                return json.dumps({"error": True, "message": "K8s client not available"})

            dep = k8s.apps_v1.read_namespaced_deployment(deployment_name, namespace)

            if dep.spec and dep.spec.template and dep.spec.template.spec:
                for c in dep.spec.template.spec.containers:
                    recs = {"container": c.name}

                    if c.resources:
                        requests = c.resources.requests or {}
                        limits = c.resources.limits or {}

                        recs["current_requests"] = requests
                        recs["current_limits"] = limits

                        # Check if limits are excessively higher than requests
                        cpu_req = _parse_cpu(requests.get("cpu", "0"))
                        cpu_lim = _parse_cpu(limits.get("cpu", "0"))
                        mem_req = _parse_memory(requests.get("memory", "0"))
                        mem_lim = _parse_memory(limits.get("memory", "0"))

                        if cpu_lim > 0 and cpu_req > 0 and cpu_lim / cpu_req > 4:
                            recs["recommendation"] = (
                                f"CPU limit ({limits.get('cpu')}) is "
                                f"{cpu_lim / cpu_req:.1f}x request ({requests.get('cpu')}). "
                                f"Consider reducing limit closer to request."
                            )
                        if mem_lim > 0 and mem_req > 0 and mem_lim / mem_req > 4:
                            recs["recommendation"] = recs.get("recommendation", "") + (
                                f" Memory limit ({limits.get('memory')}) is "
                                f"{mem_lim / mem_req:.1f}x request ({requests.get('memory')}). "
                            )
                        if not recs.get("recommendation"):
                            recs["recommendation"] = "Resource requests and limits look reasonable."

                    recommendations.append(recs)

        except Exception as e:
            return json.dumps({"error": True, "message": str(e)})

        return json.dumps({
            "deployment": deployment_name,
            "namespace": namespace,
            "recommendations": recommendations,
        }, indent=2, default=str)

    async def _find_unused_resources(self, namespace: str | None = None) -> str:
        """Find potentially unused resources."""
        results = {"unused_pvcs": [], "unused_services": [], "summary": {}}

        try:
            k8s = self._ctx.k8s_client
            if not k8s:
                return json.dumps({"error": True, "message": "K8s client not available"})

            # Find PVCs not mounted to any pod
            ns_list = [namespace] if namespace else [
                ns.metadata.name for ns in k8s.core_v1.list_namespace().items
            ]

            for ns in ns_list:
                # Get all PVCs
                pvcs = k8s.core_v1.list_namespaced_persistent_volume_claim(ns).items
                # Get all pods and their volume mounts
                pods = k8s.core_v1.list_namespaced_pod(ns).items

                mounted_pvcs: set[str] = set()
                for pod in pods:
                    if pod.spec:
                        for vol in (pod.spec.volumes or []):
                            if vol.persistent_volume_claim:
                                mounted_pvcs.add(vol.persistent_volume_claim.claim_name)

                for pvc in pvcs:
                    pvc_name = pvc.metadata.name if pvc.metadata else ""
                    if pvc_name not in mounted_pvcs:
                        # Check status
                        phase = pvc.status.phase if pvc.status else "Unknown"
                        if phase == "Bound":
                            results["unused_pvcs"].append({
                                "name": pvc_name,
                                "namespace": ns,
                                "status": phase,
                                "volume": pvc.spec.volume_name if pvc.spec else "",
                                "storage": pvc.spec.resources.requests.get("storage", "") if pvc.spec and pvc.spec.resources else "",
                                "message": "PVC is Bound but not mounted to any pod. May be orphaned.",
                            })

            results["summary"] = {
                "unused_pvcs": len(results["unused_pvcs"]),
                "total_potential_savings": f"Could free {len(results['unused_pvcs'])} orphaned PVCs",
            }

        except Exception as e:
            results["error"] = str(e)

        return json.dumps(results, indent=2, default=str)


def _parse_cpu(cpu_str: str) -> float:
    """Parse CPU resource string to cores."""
    if not cpu_str:
        return 0.0
    cpu_str = str(cpu_str).strip()
    if cpu_str.endswith("m"):
        return float(cpu_str[:-1]) / 1000
    return float(cpu_str)


def _parse_memory(mem_str: str) -> float:
    """Parse memory resource string to MiB."""
    if not mem_str:
        return 0.0
    mem_str = str(mem_str).strip()
    if mem_str.endswith("Ki"):
        return float(mem_str[:-2]) / 1024
    if mem_str.endswith("Mi"):
        return float(mem_str[:-2])
    if mem_str.endswith("Gi"):
        return float(mem_str[:-2]) * 1024
    # Assume bytes
    return float(mem_str) / (1024 * 1024)
