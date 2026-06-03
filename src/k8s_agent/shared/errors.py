"""Error hierarchy for the K8s Agent system."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base error for all application errors."""

    def __init__(
        self,
        message: str,
        code: str | None = None,
        cause: Exception | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.__class__.__name__
        self.cause = cause
        self.context = context or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "context": self.context,
        }


# ============================================================================
# Config Errors
# ============================================================================


class ConfigError(AppError):
    """Base error for configuration issues."""


class ConfigValidationError(ConfigError):
    """Configuration validation failed."""


class ConfigNotFoundError(ConfigError):
    """Configuration file not found."""


# ============================================================================
# Kubernetes Errors
# ============================================================================


class K8sError(AppError):
    """Base error for Kubernetes operations."""


class K8sConnectionError(K8sError):
    """Failed to connect to Kubernetes cluster."""


class K8sAuthenticationError(K8sError):
    """Kubernetes authentication failed."""


class K8sResourceNotFoundError(K8sError):
    """Requested Kubernetes resource not found."""


class K8sConflictError(K8sError):
    """Resource conflict (e.g., already exists)."""


class K8sForbiddenError(K8sError):
    """Operation forbidden by Kubernetes RBAC."""


class K8sTimeoutError(K8sError):
    """Kubernetes operation timed out."""


# ============================================================================
# Agent Errors
# ============================================================================


class AgentError(AppError):
    """Base error for agent operations."""


class TaskDecompositionError(AgentError):
    """Failed to decompose a user task."""


class AgentTimeoutError(AgentError):
    """Agent operation timed out."""


class MaxIterationsExceededError(AgentError):
    """Agent exceeded maximum tool call iterations."""


class ToolExecutionError(AgentError):
    """Tool execution failed."""


# ============================================================================
# Skill Errors
# ============================================================================


class SkillError(AppError):
    """Base error for skill operations."""


class SkillLoadError(SkillError):
    """Failed to load a skill."""


class SkillValidationError(SkillError):
    """Skill validation failed."""


class SkillNotFoundError(SkillError):
    """Requested skill not found."""


class SkillExecutionError(SkillError):
    """Skill execution failed."""


# ============================================================================
# LLM Errors
# ============================================================================


class LLMError(AppError):
    """Base error for LLM operations."""


class LLMAuthenticationError(LLMError):
    """LLM API authentication failed."""


class LLMRateLimitError(LLMError):
    """LLM API rate limit exceeded."""


class LLMContextLengthError(LLMError):
    """Context length exceeded for LLM."""


class LLMResponseError(LLMError):
    """Invalid or malformed LLM response."""


# ============================================================================
# MCP Errors
# ============================================================================


class MCPError(AppError):
    """Base error for MCP protocol operations."""


class MCPConnectionError(MCPError):
    """Failed to connect to MCP server."""


class MCPProtocolError(MCPError):
    """MCP protocol violation."""


class MCPToolError(MCPError):
    """MCP tool execution error."""
