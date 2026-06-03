"""Skill interface — defines the contract for all skills (built-in and custom)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from k8s_agent.skills.manifest import SkillManifest
from k8s_agent.shared.logging import BoundLogger


@dataclass
class SkillTool:
    """A tool provided by a skill."""

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema for parameters
    handler: Any  # Async callable


@dataclass
class SkillContext:
    """Context passed to skills during initialization.

    Provides access to core services without coupling skills to implementation details.
    """

    logger: BoundLogger
    config: dict[str, Any] = field(default_factory=dict)
    k8s_client: Any = None  # K8sClientWrapper (optional)
    mcp_client_manager: Any = None  # MCPClientManager (optional)
    message_bus: Any = None  # MessageBus (optional)


@dataclass
class SkillToolContext:
    """Context passed to skill tool handlers during execution."""

    logger: BoundLogger
    k8s_client: Any = None
    mcp_clients: dict[str, Any] = field(default_factory=dict)
    signal: Any = None  # Optional abort signal


class ISkill(ABC):
    """Abstract base class for all skills.

    A skill is a self-contained unit of Kubernetes expertise with:
    - A manifest describing its metadata and requirements
    - A set of tools that agents can invoke
    - Lifecycle hooks for initialization and cleanup

    To create a custom skill:
    1. Subclass ISkill
    2. Implement get_manifest() to define metadata
    3. Implement get_tools() to expose tool functions
    4. Implement initialize() for any setup
    5. Optionally implement create_agent() for sub-agent skills

    Example:
        class MyCustomSkill(ISkill):
            def get_manifest(self):
                return SkillManifest(
                    name="my-custom-skill",
                    version="1.0.0",
                    description="My custom K8s operations",
                    capabilities=["custom_operation"],
                )

            def get_tools(self):
                return [
                    SkillTool(
                        name="my_tool",
                        description="Does something useful",
                        input_schema={
                            "type": "object",
                            "properties": {"param": {"type": "string"}},
                            "required": ["param"],
                        },
                        handler=self._my_tool,
                    )
                ]

            async def _my_tool(self, param: str) -> str:
                return f"Executed with {param}"
    """

    @abstractmethod
    def get_manifest(self) -> SkillManifest:
        """Return the skill's metadata manifest."""

    @abstractmethod
    def get_tools(self) -> list[SkillTool]:
        """Return the list of tools provided by this skill.

        Each tool has a name, description, JSON Schema for inputs, and a handler.

        Returns:
            List of SkillTool objects.
        """

    async def initialize(self, context: SkillContext) -> None:
        """Initialize the skill. Called once when the skill is loaded.

        Override for any setup: load configs, connect to external services, etc.

        Args:
            context: SkillContext with logger, config, and optional services.
        """

    async def activate(self) -> None:
        """Activate the skill. Called when the skill becomes active.

        Override for any runtime setup after initialization.
        """

    async def deactivate(self) -> None:
        """Deactivate the skill. Called when the skill is suspended.

        Override to pause operations without full cleanup.
        """

    async def cleanup(self) -> None:
        """Clean up the skill. Called when the skill is unloaded.

        Override to release resources: close connections, stop timers, etc.
        """

    async def health_check(self) -> dict[str, Any]:
        """Check skill health.

        Returns:
            Dict with at least {"healthy": bool, "message": str}.
        """
        return {"healthy": True, "message": "OK"}

    async def create_agent(self, context: Any) -> Any:
        """Create a specialized sub-agent for this skill.

        Only relevant for skills with agent_type="sub-agent".

        Args:
            context: AgentContext with required services.

        Returns:
            A BaseAgent instance, or None if not applicable.
        """
        return None

    async def execute_tool(self, tool_name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name. Default implementation looks up the tool.

        Args:
            tool_name: Name of the tool to execute.
            params: Tool parameters.

        Returns:
            Tool result as string.
        """
        tools = {t.name: t for t in self.get_tools()}
        tool = tools.get(tool_name)
        if not tool:
            return f'{{"error": true, "message": "Tool \\"{tool_name}\\" not found"}}'

        import json
        result = await tool.handler(**params)
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)
