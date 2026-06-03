"""Agent factory — creates agents from configuration."""

from __future__ import annotations

from typing import Any

from k8s_agent.core.agents.base import BaseAgent
from k8s_agent.core.agents.k8s_agent import K8sAgent
from k8s_agent.core.agents.skill_agent import SkillAgent
from k8s_agent.core.communication.message_bus import MessageBus
from k8s_agent.core.communication.context import SharedContext
from k8s_agent.shared.logging import get_logger
from k8s_agent.shared.types import SkillManifest

logger = get_logger(__name__)


class AgentFactory:
    """Creates and manages agent instances.

    Supports:
    - Built-in agents (K8sAgent, MasterAgent)
    - Skill-based agents (created from skill manifests)
    - Custom agents (registered via plugins)
    """

    def __init__(
        self,
        message_bus: MessageBus,
        context: SharedContext,
        mcp_client_manager: Any = None,
        llm_provider: Any = None,
    ) -> None:
        self.message_bus = message_bus
        self.context = context
        self.mcp_client_manager = mcp_client_manager
        self.llm_provider = llm_provider
        self._agents: dict[str, BaseAgent] = {}
        self._agent_builders: dict[str, Any] = {}

    def register_builder(self, agent_type: str, builder: Any) -> None:
        """Register a custom agent builder.

        Args:
            agent_type: The agent type identifier.
            builder: A callable that creates the agent.
        """
        self._agent_builders[agent_type] = builder
        logger.info("agent_builder_registered", agent_type=agent_type)

    async def create_agent(
        self,
        agent_type: str,
        name: str = "",
        **kwargs: Any,
    ) -> BaseAgent:
        """Create and initialize an agent.

        Args:
            agent_type: "k8s", "skill", or a registered custom type.
            name: Optional agent name override.
            **kwargs: Additional arguments passed to the agent constructor.

        Returns:
            An initialized agent.
        """
        if agent_type == "k8s":
            agent = K8sAgent(
                message_bus=self.message_bus,
                context=self.context,
                mcp_client_manager=self.mcp_client_manager,
                **kwargs,
            )
        elif agent_type == "skill":
            manifest = kwargs.get("manifest")
            skill_instance = kwargs.get("skill_instance")
            if not manifest or not skill_instance:
                raise ValueError("Skill agent requires 'manifest' and 'skill_instance'")
            agent = SkillAgent(
                manifest=manifest,
                skill_instance=skill_instance,
                message_bus=self.message_bus,
                context=self.context,
                llm_provider=self.llm_provider,
                **{k: v for k, v in kwargs.items() if k not in ("manifest", "skill_instance")},
            )
        elif agent_type in self._agent_builders:
            builder = self._agent_builders[agent_type]
            agent = await builder(
                message_bus=self.message_bus,
                context=self.context,
                mcp_client_manager=self.mcp_client_manager,
                llm_provider=self.llm_provider,
                **kwargs,
            )
        else:
            raise ValueError(f"Unknown agent type: {agent_type}")

        await agent.initialize()
        self._agents[agent.identity.agent_id] = agent
        logger.info("agent_created", agent_type=agent_type, agent_id=agent.identity.agent_id)
        return agent

    async def create_skill_agent(
        self,
        manifest: SkillManifest,
        skill_instance: Any,
    ) -> SkillAgent:
        """Create a skill agent from a manifest and skill instance.

        Args:
            manifest: The skill manifest.
            skill_instance: The ISkill implementation.

        Returns:
            An initialized SkillAgent.
        """
        agent = SkillAgent(
            manifest=manifest,
            skill_instance=skill_instance,
            message_bus=self.message_bus,
            context=self.context,
            llm_provider=self.llm_provider,
        )
        await agent.initialize()
        self._agents[agent.identity.agent_id] = agent
        return agent

    def get_agent(self, agent_id: str) -> BaseAgent | None:
        """Get an agent by ID."""
        return self._agents.get(agent_id)

    def list_agents(self) -> list[BaseAgent]:
        """List all created agents."""
        return list(self._agents.values())

    def get_agents_by_type(self, agent_type: str) -> list[BaseAgent]:
        """Get all agents of a specific type."""
        return [
            a for a in self._agents.values()
            if a.identity.agent_type == agent_type
        ]

    async def shutdown_all(self) -> None:
        """Shut down all agents."""
        for agent in self._agents.values():
            try:
                await agent.shutdown()
            except Exception as e:
                logger.error("agent_shutdown_error", agent_id=agent.identity.agent_id, error=str(e))
        self._agents.clear()
        logger.info("all_agents_shutdown")
