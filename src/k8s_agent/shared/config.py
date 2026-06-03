"""Configuration management using pydantic-settings."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class KubernetesConfig(BaseSettings):
    """Kubernetes connection configuration."""

    auth_mode: str = "kubeconfig"  # kubeconfig | in-cluster | token
    kubeconfig_path: str = "~/.kube/config"
    context: str | None = None
    api_server: str | None = Field(default=None, alias="k8s_api_server")
    token: str | None = Field(default=None, alias="k8s_token")

    model_config = SettingsConfigDict(env_prefix="K8S_", extra="ignore")


class LLMConfig(BaseSettings):
    """LLM provider configuration."""

    provider: str = "anthropic"
    api_key: str = Field(default="", alias="anthropic_api_key")
    model: str = "claude-sonnet-4-5-20250929"
    max_tokens: int = 4096
    temperature: float = 0.2
    sub_agent_model: dict[str, str] = Field(default_factory=dict)
    retry_max_retries: int = 3
    retry_base_delay_ms: int = 1000
    retry_max_delay_ms: int = 30000
    circuit_failure_threshold: int = 5
    circuit_reset_timeout_ms: int = 30000

    model_config = SettingsConfigDict(env_prefix="LLM_", extra="ignore")


class MCPServerConfig(BaseSettings):
    """MCP Server configuration."""

    transport: str = "stdio"  # stdio | http
    http_host: str = "0.0.0.0"
    http_port: int = 3100
    session_timeout_ms: int = 3_600_000
    access_control_enabled: bool = True
    allowed_tools: list[str] = Field(default_factory=lambda: ["*"])
    denied_tools: list[str] = Field(default_factory=list)

    model_config = SettingsConfigDict(env_prefix="MCP_", extra="ignore")


class AgentConfig(BaseSettings):
    """Agent orchestration configuration."""

    max_iterations: int = 15
    task_timeout_ms: int = 300_000
    parallel_task_limit: int = 3
    confirmation_required: list[str] = Field(
        default_factory=lambda: [
            "delete_namespace",
            "delete_deployment",
            "delete_service",
            "drain_node",
            "uninstall_helm_release",
        ]
    )

    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")


class SkillConfig(BaseSettings):
    """Skill system configuration."""

    paths: list[str] = Field(
        default_factory=lambda: ["~/.k8s-agent/skills", "./.k8s-agent/skills"]
    )
    auto_load_npm: bool = False
    enabled: list[str] = Field(default_factory=list)
    skill_configs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sandbox_max_tool_calls: int = 20
    sandbox_network_access: bool = True
    sandbox_filesystem_access: str = "readonly"

    model_config = SettingsConfigDict(env_prefix="SKILL_", extra="ignore")


class LoggingConfig(BaseSettings):
    """Logging configuration."""

    level: str = "info"
    format: str = "pretty"  # pretty | json
    output: str = "stdout"
    file_path: str = "./logs/k8s-agent.log"
    file_max_size: str = "10m"
    file_max_files: int = 5

    model_config = SettingsConfigDict(env_prefix="LOG_", extra="ignore")


class Settings(BaseSettings):
    """Root settings aggregating all configuration sections."""

    kubernetes: KubernetesConfig = Field(default_factory=KubernetesConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mcp_server: MCPServerConfig = Field(default_factory=MCPServerConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    skills: SkillConfig = Field(default_factory=SkillConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    model_config = SettingsConfigDict(
        env_prefix="K8S_AGENT_",
        extra="ignore",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Settings":
        """Load settings from a YAML configuration file."""
        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Invalid YAML config: {path}")

        # Resolve environment variable references like ${VAR_NAME}
        resolved = cls._resolve_env_vars(data)
        return cls(**resolved)

    @staticmethod
    def _resolve_env_vars(data: Any) -> Any:
        """Recursively resolve ${ENV_VAR} references in config values."""
        import re

        if isinstance(data, dict):
            return {k: Settings._resolve_env_vars(v) for k, v in data.items()}
        if isinstance(data, list):
            return [Settings._resolve_env_vars(item) for item in data]
        if isinstance(data, str):
            pattern = re.compile(r"\$\{([^}]+)\}")

            def replacer(m: re.Match[str]) -> str:
                return os.environ.get(m.group(1), m.group(0))

            return pattern.sub(replacer, data)
        return data


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings, loading from file or defaults.

    Checks K8S_AGENT_CONFIG env var for YAML config path,
    otherwise uses environment variables and defaults.

    Also loads .env file into os.environ so that non-prefixed
    env vars (e.g. DEEPSEEK_API_KEY) are available to providers.
    """
    _load_dotenv()
    config_path = os.environ.get("K8S_AGENT_CONFIG")
    if config_path:
        return Settings.from_yaml(config_path)
    return Settings()


def _load_dotenv() -> None:
    """Load .env file into os.environ (only if not already set)."""
    env_file = Path(".env")
    if not env_file.exists():
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Only set if not already in environment (env vars take precedence)
            if key and key not in os.environ:
                os.environ[key] = value
