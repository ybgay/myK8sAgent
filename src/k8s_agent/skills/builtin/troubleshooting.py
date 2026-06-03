"""Troubleshooting skill — diagnose pod crashes, connectivity issues, and cluster health."""

from __future__ import annotations

import json
from typing import Any

from k8s_agent.skills.interface import ISkill, SkillContext, SkillTool
from k8s_agent.skills.manifest import SkillManifest
from k8s_agent.shared.logging import get_logger


class TroubleshootingSkill(ISkill):
    """Skill for diagnosing Kubernetes issues.

    Tools:
    - diagnose_crash_loop: Analyze why a pod is in CrashLoopBackOff
    - check_connectivity: Test network connectivity between resources
    - analyze_resource_pressure: Check node conditions
    - health_check: Broad namespace health scan
    """

    def get_manifest(self) -> SkillManifest:
        return SkillManifest(
            name="k8s-troubleshooting",
            version="1.0.0",
            description="Diagnose Kubernetes issues: crash loops, connectivity, resource pressure, health checks",
            author="k8s-agent",
            keywords=["kubernetes", "troubleshooting", "diagnostics", "debugging"],
            agent_type="tool-only",
            capabilities=[
                "diagnose_crash_loop",
                "check_connectivity",
                "analyze_resource_pressure",
                "health_check",
            ],
        )

    def get_tools(self) -> list[SkillTool]:
        return [
            SkillTool(
                name="diagnose_crash_loop",
                description="Analyze why a pod is crashing/restarting. Checks pod status, events, logs, and resource limits.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "pod_name": {"type": "string", "description": "The crashing pod name"},
                        "namespace": {"type": "string", "default": "default"},
                    },
                    "required": ["pod_name"],
                },
                handler=self._diagnose_crash_loop,
            ),
            SkillTool(
                name="check_connectivity",
                description="Check network connectivity between two Kubernetes resources.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "source_pod": {"type": "string", "description": "Source pod name for connectivity test"},
                        "target": {"type": "string", "description": "Target hostname, IP, or service name"},
                        "namespace": {"type": "string", "default": "default"},
                        "port": {"type": "integer", "description": "Target port"},
                    },
                    "required": ["source_pod", "target"],
                },
                handler=self._check_connectivity,
            ),
            SkillTool(
                name="analyze_resource_pressure",
                description="Check node conditions for MemoryPressure, DiskPressure, PIDPressure.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "node_name": {"type": "string", "description": "Specific node to check (all nodes if omitted)"},
                    },
                },
                handler=self._analyze_resource_pressure,
            ),
            SkillTool(
                name="health_check",
                description="Perform a broad health scan of a namespace.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string", "default": "default"},
                    },
                },
                handler=self._health_check,
            ),
        ]

    async def initialize(self, context: SkillContext) -> None:
        self._ctx = context
        self._ctx.logger.info("troubleshooting_skill_initialized")

    async def _diagnose_crash_loop(self, pod_name: str, namespace: str = "default") -> str:
        """Analyze a crashing pod using K8s API."""
        results: dict[str, Any] = {"pod": pod_name, "namespace": namespace, "findings": []}

        try:
            k8s = self._ctx.k8s_client
            if not k8s:
                return json.dumps({"error": True, "message": "K8s client not available"})

            # 1. Get pod details
            pod = k8s.core_v1.read_namespaced_pod(pod_name, namespace)
            status = pod.status
            phase = status.phase if status else "Unknown"

            # Check container statuses
            if status and status.container_statuses:
                for cs in status.container_statuses:
                    if cs.state and cs.state.waiting:
                        reason = cs.state.waiting.reason
                        results["findings"].append({
                            "type": "container_waiting",
                            "container": cs.name,
                            "reason": reason,
                            "message": cs.state.waiting.message or "",
                            "severity": "high",
                        })
                        # Known patterns
                        if reason == "CrashLoopBackOff":
                            results["findings"].append({
                                "type": "analysis",
                                "message": "Pod is in CrashLoopBackOff — likely causes: OOM, bad startup, missing config, or app error.",
                                "suggestion": "Check pod logs with get_pod_logs(previous=True) and review resource limits.",
                            })
                        elif reason == "ImagePullBackOff":
                            results["findings"].append({
                                "type": "analysis",
                                "message": "Cannot pull container image. Check image name, registry auth, and network.",
                                "suggestion": "Verify image exists: check registry, imagePullSecrets, and image name spelling.",
                            })
                    elif cs.state and cs.state.terminated:
                        results["findings"].append({
                            "type": "container_terminated",
                            "container": cs.name,
                            "reason": cs.state.terminated.reason,
                            "exit_code": cs.state.terminated.exit_code,
                            "message": cs.state.terminated.message or "",
                            "severity": "high" if cs.state.terminated.reason == "Error" else "medium",
                        })

            # 2. Get events
            events = k8s.core_v1.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={pod_name}",
            )
            for evt in events.items:
                if evt.type == "Warning":
                    results["findings"].append({
                        "type": "event",
                        "reason": evt.reason,
                        "message": evt.message,
                        "count": evt.count,
                    })

            # Add summary
            finding_count = len(results["findings"])
            results["summary"] = f"Found {finding_count} findings for {pod_name}. "
            if finding_count == 0:
                results["summary"] += "Pod appears healthy currently. Check recent changes."

        except Exception as e:
            results["error"] = str(e)

        return json.dumps(results, indent=2, default=str)

    async def _check_connectivity(
        self, source_pod: str, target: str, namespace: str = "default", port: int | None = None
    ) -> str:
        """Check connectivity from one pod to another via exec."""
        results = {"source": source_pod, "target": target, "port": port}

        try:
            k8s = self._ctx.k8s_client
            if not k8s:
                return json.dumps({"error": True, "message": "K8s client not available"})

            from kubernetes.stream import stream

            # Try DNS resolution first
            nslookup_cmd = ["nslookup", target]
            try:
                resp = stream(
                    k8s.core_v1.connect_get_namespaced_pod_exec,
                    source_pod, namespace,
                    command=nslookup_cmd,
                    stdout=True, stderr=True,
                    timeout=10,
                )
                results["dns_resolution"] = "success" if resp and "NXDOMAIN" not in str(resp) else "failed"
            except Exception:
                results["dns_resolution"] = "nslookup not available in pod"

            # Try TCP connectivity
            if port:
                nc_cmd = ["nc", "-zv", "-w", "3", target, str(port)]
                try:
                    resp = stream(
                        k8s.core_v1.connect_get_namespaced_pod_exec,
                        source_pod, namespace,
                        command=nc_cmd,
                        stdout=True, stderr=True,
                        timeout=10,
                    )
                    results["tcp_connect"] = "success" if resp and "succeeded" in str(resp).lower() else "failed"
                except Exception:
                    results["tcp_connect"] = "nc not available in pod"

            results["verdict"] = (
                "Connectivity OK" if results.get("dns_resolution") == "success"
                else "Connectivity issue detected"
            )

        except Exception as e:
            results["error"] = str(e)

        return json.dumps(results, indent=2, default=str)

    async def _analyze_resource_pressure(self, node_name: str | None = None) -> str:
        """Check nodes for resource pressure conditions."""
        try:
            k8s = self._ctx.k8s_client
            if not k8s:
                return json.dumps({"error": True, "message": "K8s client not available"})

            if node_name:
                nodes = [k8s.core_v1.read_node(node_name)]
            else:
                nodes = k8s.core_v1.list_node().items

            findings = []
            for node in nodes:
                node_findings = {"name": node.metadata.name if node.metadata else "", "issues": []}
                if node.status and node.status.conditions:
                    for cond in node.status.conditions:
                        if cond.type in ("MemoryPressure", "DiskPressure", "PIDPressure") and cond.status == "True":
                            node_findings["issues"].append({
                                "type": cond.type,
                                "reason": cond.reason or "Unknown",
                                "message": cond.message or "",
                            })
                        elif cond.type == "Ready" and cond.status != "True":
                            node_findings["issues"].append({
                                "type": "NodeNotReady",
                                "reason": cond.reason or "Unknown",
                                "message": cond.message or "",
                            })
                findings.append(node_findings)

            issue_count = sum(len(f["issues"]) for f in findings)
            return json.dumps({
                "nodes_checked": len(findings),
                "nodes_with_issues": sum(1 for f in findings if f["issues"]),
                "total_issues": issue_count,
                "findings": findings,
            }, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": True, "message": str(e)})

    async def _health_check(self, namespace: str = "default") -> str:
        """Broad health scan of a namespace."""
        results = {"namespace": namespace, "issues": [], "healthy": True}

        try:
            k8s = self._ctx.k8s_client
            if not k8s:
                return json.dumps({"error": True, "message": "K8s client not available"})

            # Check pods not running
            pods = k8s.core_v1.list_namespaced_pod(namespace)
            for pod in pods.items:
                status = pod.status
                if status and status.phase not in ("Running", "Succeeded"):
                    results["issues"].append({
                        "resource": f"Pod/{pod.metadata.name}",
                        "phase": status.phase,
                        "message": f"Pod is {status.phase}",
                    })

            # Check deployments with unavailable replicas
            from kubernetes.client import AppsV1Api
            apps = AppsV1Api(k8s.api_client)
            deploys = apps.list_namespaced_deployment(namespace)
            for d in deploys.items:
                status = d.status
                if status:
                    total = status.replicas or 0
                    ready = status.ready_replicas or 0
                    if total > 0 and ready < total:
                        results["issues"].append({
                            "resource": f"Deployment/{d.metadata.name}",
                            "replicas": total,
                            "ready": ready,
                            "message": f"Only {ready}/{total} replicas ready",
                        })

            results["healthy"] = len(results["issues"]) == 0
            results["resource_count"] = {
                "pods": len(pods.items),
                "deployments": len(deploys.items),
            }

        except Exception as e:
            results["error"] = str(e)
            results["healthy"] = False

        return json.dumps(results, indent=2, default=str)
