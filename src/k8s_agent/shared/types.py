"""Core type definitions using Pydantic models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ============================================================================
# Agent Types
# ============================================================================


class AgentStatus(str, Enum):
    READY = "ready"
    BUSY = "busy"
    ERROR = "error"
    IDLE = "idle"


class AgentCapability(BaseModel):
    """A capability that an agent can provide."""

    name: str
    description: str
    tags: list[str] = Field(default_factory=list)


class AgentIdentity(BaseModel):
    """Identity and metadata for an agent."""

    agent_id: str = Field(default_factory=lambda: f"agent-{uuid.uuid4().hex[:8]}")
    agent_type: str  # "master", "k8s", "skill"
    name: str
    capabilities: list[AgentCapability] = Field(default_factory=list)
    status: AgentStatus = AgentStatus.IDLE
    metadata: dict[str, str] = Field(default_factory=dict)


# ============================================================================
# Skill Types
# ============================================================================


class SkillToolDef(BaseModel):
    """Definition of a tool provided by a skill."""

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema for tool parameters


class SkillManifest(BaseModel):
    """Metadata manifest for a skill."""

    name: str
    version: str
    description: str
    author: str = "unknown"
    license: str | None = None
    keywords: list[str] = Field(default_factory=list)
    agent_type: str = "tool-only"  # "tool-only" | "sub-agent"
    requires: dict[str, list[str]] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    agent_config: dict[str, Any] | None = None  # sub-agent config


# ============================================================================
# Tool Types
# ============================================================================


class ToolDefinition(BaseModel):
    """Complete tool definition (MCP-compatible)."""

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema


class ToolResult(BaseModel):
    """Result from a tool execution."""

    content: list[dict[str, Any]]
    is_error: bool = False
    tool_call_id: str | None = None


# ============================================================================
# Message / Conversation Types
# ============================================================================


class ContentBlock(BaseModel):
    """A single content block in a message."""

    type: str  # "text" | "tool_use" | "tool_result"
    text: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    content: str | None = None  # For tool_result


class Message(BaseModel):
    """A message in a conversation."""

    role: str  # "user" | "assistant" | "system"
    content: list[ContentBlock] | str


# ============================================================================
# Task / Communication Types
# ============================================================================


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRequest(BaseModel):
    """A request to execute a task."""

    task_id: str = Field(default_factory=lambda: f"task-{uuid.uuid4().hex[:12]}")
    parent_task_id: str | None = None
    task_type: str  # "k8s", "skill", "llm", "system"
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    timeout_ms: int = 300_000  # 5 minutes


class TaskProgress(BaseModel):
    """Progress update for a task."""

    task_id: str
    status: TaskStatus
    message: str = ""
    percent_complete: float = 0.0


class TaskResult(BaseModel):
    """Final result of a task."""

    task_id: str
    status: TaskStatus
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# K8s Resource Types
# ============================================================================


class K8sResourceKind(str, Enum):
    POD = "Pod"
    DEPLOYMENT = "Deployment"
    SERVICE = "Service"
    NAMESPACE = "Namespace"
    CONFIGMAP = "ConfigMap"
    SECRET = "Secret"
    NODE = "Node"
    EVENT = "Event"
    STATEFULSET = "StatefulSet"
    DAEMONSET = "DaemonSet"
    JOB = "Job"
    CRONJOB = "CronJob"
    INGRESS = "Ingress"
    PVC = "PersistentVolumeClaim"
    PV = "PersistentVolume"
    SERVICEACCOUNT = "ServiceAccount"
    ROLE = "Role"
    ROLEBINDING = "RoleBinding"
    HPA = "HorizontalPodAutoscaler"
