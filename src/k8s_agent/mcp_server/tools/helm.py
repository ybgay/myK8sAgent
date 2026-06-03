"""Helm management tools for MCP server.

These tools require the helm CLI to be installed and available in PATH.
Uses subprocess to call helm commands.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from k8s_agent.mcp_server.k8s_client import create_k8s_client
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def _helm_available() -> bool:
    """Check if helm CLI is available."""
    return shutil.which("helm") is not None


async def _run_helm(*args: str) -> tuple[int, str, str]:
    """Run a helm command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "helm", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


def register_helm_tools(mcp_server: Any) -> None:
    @mcp_server.tool()
    async def list_helm_releases(
        namespace: str | None = None, all_namespaces: bool = False
    ) -> str:
        """List Helm releases.

        Args:
            namespace: Filter by namespace.
            all_namespaces: List releases across all namespaces.
        """
        if not _helm_available():
            return json.dumps({"error": True, "message": "helm CLI not found in PATH"})

        args = ["list"]
        if all_namespaces:
            args.append("--all-namespaces")
        elif namespace:
            args.extend(["--namespace", namespace])
        args.extend(["--output", "json"])

        rc, stdout, stderr = await _run_helm(*args)
        if rc != 0:
            return json.dumps({"error": True, "message": stderr})
        return stdout

    @mcp_server.tool()
    async def get_helm_release(name: str, namespace: str = "default") -> str:
        """Get details of a Helm release.

        Args:
            name: Release name.
            namespace: Target namespace.
        """
        if not _helm_available():
            return json.dumps({"error": True, "message": "helm CLI not found in PATH"})

        rc, stdout, stderr = await _run_helm(
            "status", name, "--namespace", namespace, "--output", "json"
        )
        if rc != 0:
            return json.dumps({"error": True, "message": stderr})
        return stdout

    @mcp_server.tool()
    async def install_helm_chart(
        chart: str,
        name: str,
        namespace: str = "default",
        version: str | None = None,
        values: str | None = None,
    ) -> str:
        """Install a Helm chart.

        Args:
            chart: Chart reference (repo/chart or local path).
            name: Release name.
            namespace: Target namespace.
            version: Chart version.
            values: JSON string of values to override.
        """
        if not _helm_available():
            return json.dumps({"error": True, "message": "helm CLI not found in PATH"})

        args = ["install", name, chart, "--namespace", namespace, "--create-namespace"]
        if version:
            args.extend(["--version", version])
        if values:
            args.extend(["--set-json", values])

        rc, stdout, stderr = await _run_helm(*args)
        if rc != 0:
            return json.dumps({"error": True, "message": stderr, "stdout": stdout})
        return json.dumps({
            "message": f"Helm release '{name}' installed in '{namespace}'.",
            "output": stdout.strip(),
        })

    @mcp_server.tool()
    async def uninstall_helm_release(
        name: str, namespace: str = "default"
    ) -> str:
        """Uninstall a Helm release.

        Args:
            name: Release name.
            namespace: Target namespace.
        """
        if not _helm_available():
            return json.dumps({"error": True, "message": "helm CLI not found in PATH"})

        rc, stdout, stderr = await _run_helm(
            "uninstall", name, "--namespace", namespace
        )
        if rc != 0:
            return json.dumps({"error": True, "message": stderr})
        return json.dumps({
            "message": f"Helm release '{name}' uninstalled from '{namespace}'.",
            "output": stdout.strip(),
        })

    @mcp_server.tool()
    async def helm_repo_list() -> str:
        """List configured Helm repositories."""
        if not _helm_available():
            return json.dumps({"error": True, "message": "helm CLI not found in PATH"})

        rc, stdout, stderr = await _run_helm("repo", "list", "--output", "json")
        if rc != 0:
            return json.dumps({"error": True, "message": stderr})
        return stdout

    @mcp_server.tool()
    async def helm_repo_add(name: str, url: str) -> str:
        """Add a Helm repository.

        Args:
            name: Repository name.
            url: Repository URL.
        """
        if not _helm_available():
            return json.dumps({"error": True, "message": "helm CLI not found in PATH"})

        rc, stdout, stderr = await _run_helm("repo", "add", name, url)
        if rc != 0:
            return json.dumps({"error": True, "message": stderr})
        return json.dumps({"message": f"Helm repo '{name}' added.", "output": stdout.strip()})
