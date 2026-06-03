"""Deployment Analyzer skill — validate manifests, audit resources, analyze rollout risks."""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from k8s_agent.skills.interface import ISkill, SkillContext, SkillTool
from k8s_agent.skills.manifest import SkillManifest


class DeploymentAnalyzerSkill(ISkill):
    """Skill for analyzing and validating Kubernetes deployments.

    Tools:
    - validate_manifest: Lint K8s YAML for best practices
    - audit_resources: Check for missing resource limits, probes, PDBs
    - analyze_rollout_risk: Assess risk of pending deployment rollout
    """

    def get_manifest(self) -> SkillManifest:
        return SkillManifest(
            name="deployment-analyzer",
            version="1.0.0",
            description="Validate K8s manifests, audit resource configs, assess rollout risks",
            author="k8s-agent",
            keywords=["kubernetes", "deployment", "manifest", "audit", "security"],
            agent_type="tool-only",
            capabilities=["validate_manifest", "audit_resources", "analyze_rollout_risk"],
        )

    def get_tools(self) -> list[SkillTool]:
        return [
            SkillTool(
                name="validate_manifest",
                description="Validate a Kubernetes YAML manifest for best practices: missing limits, probes, security contexts, image tags, etc.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "manifest": {"type": "string", "description": "The K8s YAML manifest to validate"},
                    },
                    "required": ["manifest"],
                },
                handler=self._validate_manifest,
            ),
            SkillTool(
                name="audit_resources",
                description="Audit resources in a namespace for missing resource limits, health probes, pod disruption budgets, and security contexts.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string", "default": "default"},
                    },
                },
                handler=self._audit_resources,
            ),
            SkillTool(
                name="analyze_rollout_risk",
                description="Assess the risk of a deployment rollout based on resource changes, replica count, and strategy.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "deployment_name": {"type": "string"},
                        "namespace": {"type": "string", "default": "default"},
                    },
                    "required": ["deployment_name"],
                },
                handler=self._analyze_rollout_risk,
            ),
        ]

    async def initialize(self, context: SkillContext) -> None:
        self._ctx = context
        self._ctx.logger.info("deployment_analyzer_initialized")

    async def _validate_manifest(self, manifest: str) -> str:
        """Validate a K8s YAML manifest for best practices."""
        findings = []

        try:
            docs = list(yaml.safe_load_all(manifest))
        except yaml.YAMLError as e:
            return json.dumps({"error": True, "message": f"YAML parse error: {e}"})

        for i, doc in enumerate(docs):
            if doc is None:
                continue

            kind = doc.get("kind", "Unknown")
            metadata = doc.get("metadata", {})
            spec = doc.get("spec", {})

            findings.append({
                "document_index": i,
                "kind": kind,
                "name": metadata.get("name", "unnamed"),
            })

            # Check for recommended labels
            labels = metadata.get("labels", {})
            if "app" not in labels and "app.kubernetes.io/name" not in labels:
                findings.append({
                    "type": "recommendation",
                    "severity": "low",
                    "message": "Add 'app' or 'app.kubernetes.io/name' label for resource identification",
                })

            # Deployment-specific checks
            if kind in ("Deployment", "StatefulSet", "DaemonSet"):
                template = spec.get("template", {})
                pod_spec = template.get("spec", {})
                containers = pod_spec.get("containers", [])

                for c in containers:
                    cname = c.get("name", "unnamed")

                    # Check image tag
                    image = c.get("image", "")
                    if ":latest" in image or ":" not in image:
                        findings.append({
                            "type": "warning",
                            "severity": "medium",
                            "message": f"Container '{cname}': Image '{image}' uses ':latest' or no tag. Pin a specific version.",
                        })

                    # Check resource limits
                    resources = c.get("resources", {})
                    limits = resources.get("limits", {})
                    requests = resources.get("requests", {})
                    if not limits:
                        findings.append({
                            "type": "warning",
                            "severity": "high",
                            "message": f"Container '{cname}': No resource limits set. This can lead to resource starvation.",
                        })
                    if not requests:
                        findings.append({
                            "type": "warning",
                            "severity": "medium",
                            "message": f"Container '{cname}': No resource requests set. Scheduler cannot make optimal decisions.",
                        })

                    # Check probes
                    if not c.get("livenessProbe"):
                        findings.append({
                            "type": "recommendation",
                            "severity": "medium",
                            "message": f"Container '{cname}': No liveness probe. K8s cannot detect stuck containers.",
                        })
                    if not c.get("readinessProbe"):
                        findings.append({
                            "type": "recommendation",
                            "severity": "medium",
                            "message": f"Container '{cname}': No readiness probe. Traffic may route to unready pods.",
                        })

                # Check security context
                security = pod_spec.get("securityContext", {})
                if not security.get("runAsNonRoot"):
                    findings.append({
                        "type": "recommendation",
                        "severity": "medium",
                        "message": "Pod securityContext: Consider setting runAsNonRoot=true",
                    })

                # Check replica count
                replicas = spec.get("replicas", 1)
                if kind == "Deployment" and replicas == 1:
                    findings.append({
                        "type": "recommendation",
                        "severity": "low",
                        "message": "Single replica — no high availability. Consider replicas >= 2 for production.",
                    })

        summary = {
            "total_findings": len(findings),
            "errors": len([f for f in findings if f.get("severity") == "high"]),
            "warnings": len([f for f in findings if f.get("severity") == "medium"]),
            "recommendations": len([f for f in findings if f.get("severity") == "low"]),
            "findings": findings,
        }
        return json.dumps(summary, indent=2, default=str)

    async def _audit_resources(self, namespace: str = "default") -> str:
        """Audit deployments in a namespace for missing best practices."""
        results = {"namespace": namespace, "deployments": [], "summary": {}}

        try:
            k8s = self._ctx.k8s_client
            if not k8s:
                return json.dumps({"error": True, "message": "K8s client not available"})

            from kubernetes.client import AppsV1Api
            apps = AppsV1Api(k8s.api_client)
            deploys = apps.list_namespaced_deployment(namespace)

            total_issues = 0
            for d in deploys.items:
                issues = []
                if d.spec and d.spec.template and d.spec.template.spec:
                    for c in d.spec.template.spec.containers:
                        if not (c.resources and c.resources.limits):
                            issues.append(f"Container '{c.name}': missing resource limits")
                        if not (c.resources and c.resources.requests):
                            issues.append(f"Container '{c.name}': missing resource requests")
                        if not c.liveness_probe:
                            issues.append(f"Container '{c.name}': missing liveness probe")
                        if not c.readiness_probe:
                            issues.append(f"Container '{c.name}': missing readiness probe")

                total_issues += len(issues)
                results["deployments"].append({
                    "name": d.metadata.name if d.metadata else "",
                    "replicas": d.spec.replicas if d.spec else 0,
                    "issues": issues,
                    "score": "good" if len(issues) == 0 else "needs_improvement" if len(issues) < 3 else "poor",
                })

            results["summary"] = {
                "deployments_checked": len(deploys.items),
                "total_issues": total_issues,
                "good": sum(1 for d in results["deployments"] if d["score"] == "good"),
                "needs_improvement": sum(1 for d in results["deployments"] if d["score"] == "needs_improvement"),
                "poor": sum(1 for d in results["deployments"] if d["score"] == "poor"),
            }

        except Exception as e:
            results["error"] = str(e)

        return json.dumps(results, indent=2, default=str)

    async def _analyze_rollout_risk(self, deployment_name: str, namespace: str = "default") -> str:
        """Assess rollout risk for a deployment."""
        risk_factors = []
        risk_score = 0

        try:
            k8s = self._ctx.k8s_client
            if not k8s:
                return json.dumps({"error": True, "message": "K8s client not available"})

            dep = k8s.apps_v1.read_namespaced_deployment(deployment_name, namespace)
            spec = dep.spec
            status = dep.status

            # Replica count risk
            replicas = spec.replicas if spec else 0
            if replicas == 1:
                risk_factors.append("Single replica — no redundancy during rollout")
                risk_score += 30

            # Strategy risk
            if spec and spec.strategy:
                strategy_type = str(spec.strategy.type) if spec.strategy.type else "RollingUpdate"
                if strategy_type == "Recreate":
                    risk_factors.append("Recreate strategy — downtime during rollout")
                    risk_score += 40
                elif spec.strategy.rolling_update:
                    max_unavailable = spec.strategy.rolling_update.max_unavailable or "25%"
                    if max_unavailable == "25%" and replicas <= 3:
                        risk_factors.append(f"MaxUnavailable={max_unavailable} with only {replicas} replicas")
                        risk_score += 20

            # Check current rollout state
            if status:
                if status.unavailable_replicas and status.unavailable_replicas > 0:
                    risk_factors.append(f"Currently {status.unavailable_replicas} unavailable replicas")
                    risk_score += 25

            # Resource change risk (approximate by checking if container images look different)
            if spec and spec.template and spec.template.spec:
                for c in spec.template.spec.containers:
                    if ":latest" in c.image:
                        risk_factors.append(f"Container '{c.name}' uses ':latest' tag — unpredictable changes")
                        risk_score += 20

            # Determine risk level
            if risk_score >= 50:
                risk_level = "high"
            elif risk_score >= 25:
                risk_level = "medium"
            else:
                risk_level = "low"

        except Exception as e:
            return json.dumps({"error": True, "message": str(e)})

        return json.dumps({
            "deployment": deployment_name,
            "namespace": namespace,
            "risk_level": risk_level,
            "risk_score": risk_score,
            "risk_factors": risk_factors,
            "recommendation": (
                "Proceed with caution. Review the risk factors before rolling out."
                if risk_level != "low"
                else "Low risk. Normal rollout should succeed."
            ),
        }, indent=2)
