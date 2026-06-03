"""Core agent orchestration system — multi-agent collaboration, MCP client management."""

from k8s_agent.core.communication.message_bus import MessageBus
from k8s_agent.core.communication.context import SharedContext
from k8s_agent.core.agents.base import BaseAgent
from k8s_agent.core.agents.k8s_agent import K8sAgent
from k8s_agent.core.agents.skill_agent import SkillAgent
from k8s_agent.core.agents.factory import AgentFactory
from k8s_agent.core.orchestrator import MasterAgent, TaskDecomposer, PlanExecutor
from k8s_agent.core.mcp.client_manager import MCPClientManager
from k8s_agent.core.session import SessionManager

__all__ = [
    "MessageBus",
    "SharedContext",
    "BaseAgent",
    "K8sAgent",
    "SkillAgent",
    "AgentFactory",
    "MasterAgent",
    "TaskDecomposer",
    "PlanExecutor",
    "MCPClientManager",
    "SessionManager",
]
