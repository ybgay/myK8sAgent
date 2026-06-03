"""Kubernetes client factory supporting multiple authentication strategies."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from kubernetes import client, config
from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api

from k8s_agent.shared.config import KubernetesConfig, get_settings
from k8s_agent.shared.errors import K8sAuthenticationError, K8sConnectionError
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


class K8sClientWrapper:
    """Wrapper around Kubernetes Python client providing convenient access."""

    def __init__(self, api_client: ApiClient) -> None:
        self._api_client = api_client
        self.core_v1 = CoreV1Api(api_client)
        self.apps_v1 = AppsV1Api(api_client)

    @property
    def api_client(self) -> ApiClient:
        return self._api_client

    def close(self) -> None:
        """Close the API client connection."""
        self._api_client.close()


def _load_kubeconfig(kubeconfig_path: str, context: str | None) -> ApiClient:
    """Load Kubernetes configuration from kubeconfig file."""
    path = Path(kubeconfig_path).expanduser()
    if not path.exists():
        raise K8sConnectionError(
            f"Kubeconfig not found: {path}",
            context={"kubeconfig_path": str(path)},
        )

    kwargs: dict[str, Any] = {"config_file": str(path)}
    if context:
        kwargs["context"] = context

    try:
        loader = config.new_client_from_config(**kwargs)
        return loader
    except config.ConfigException as e:
        raise K8sAuthenticationError(
            f"Failed to load kubeconfig: {e}",
            cause=e,
            context={"kubeconfig_path": str(path), "context": context},
        )


def _load_in_cluster() -> ApiClient:
    """Load in-cluster Kubernetes configuration (for running inside a pod)."""
    try:
        config.load_incluster_config()
        return client.ApiClient()
    except config.ConfigException as e:
        raise K8sConnectionError(
            "Failed to load in-cluster config. Are you running inside a pod?",
            cause=e,
        )


def _load_token(api_server: str, token: str) -> ApiClient:
    """Load Kubernetes configuration using explicit API server URL and token."""
    configuration = client.Configuration()
    configuration.host = api_server
    configuration.api_key = {"authorization": f"Bearer {token}"}
    configuration.verify_ssl = True
    return client.ApiClient(configuration)


@lru_cache(maxsize=1)
def create_k8s_client(config_override: KubernetesConfig | None = None) -> K8sClientWrapper:
    """Create a Kubernetes API client based on configuration.

    Supports three authentication modes:
    - kubeconfig: Load from a kubeconfig file (default ~/.kube/config)
    - in-cluster: Use service account token when running inside a pod
    - token: Use explicit API server URL and bearer token

    Args:
        config_override: Optional configuration override.

    Returns:
        K8sClientWrapper with initialized API clients.
    """
    k8s_config = config_override or get_settings().kubernetes
    auth_mode = k8s_config.auth_mode.lower()

    logger.info("creating_k8s_client", auth_mode=auth_mode)

    try:
        if auth_mode == "in-cluster":
            api_client = _load_in_cluster()
        elif auth_mode == "token":
            if not k8s_config.api_server or not k8s_config.token:
                raise K8sAuthenticationError(
                    "Token auth requires api_server and token",
                    context={"auth_mode": auth_mode},
                )
            api_client = _load_token(k8s_config.api_server, k8s_config.token)
        else:  # kubeconfig (default)
            api_client = _load_kubeconfig(
                k8s_config.kubeconfig_path,
                k8s_config.context,
            )

        # Verify connectivity
        core_api = CoreV1Api(api_client)
        core_api.list_namespace(limit=1)

        logger.info("k8s_client_created", auth_mode=auth_mode)
        return K8sClientWrapper(api_client)

    except (K8sConnectionError, K8sAuthenticationError):
        raise
    except Exception as e:
        raise K8sConnectionError(
            f"Failed to connect to Kubernetes: {e}",
            cause=e,
            context={"auth_mode": auth_mode},
        )


def get_cluster_info(k8s: K8sClientWrapper) -> dict[str, Any]:
    """Get basic cluster information."""
    try:
        version = k8s.core_v1.api_client.call_api(
            "/version", "GET", response_type="object"
        )
        return {
            "version": version,
            "connected": True,
        }
    except Exception as e:
        logger.warning("failed_get_cluster_info", error=str(e))
        return {"version": "unknown", "connected": False, "error": str(e)}
