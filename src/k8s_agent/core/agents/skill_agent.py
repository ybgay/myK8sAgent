"""Skill Agent — wraps a skill plugin as a sub-agent."""

from __future__ import annotations

import json
from typing import Any

from k8s_agent.core.agents.base import BaseAgent
from k8s_agent.core.communication.message_bus import MessageBus
from k8s_agent.core.communication.context import SharedContext
from k8s_agent.shared.types import (
    AgentCapability,
    SkillManifest,
    TaskRequest,
    TaskResult,
    TaskStatus,
)


class SkillAgent(BaseAgent):
    """Agent wrapping a skill plugin.

    Skill agents can be "tool-only" (provide tools to the master agent)
    or "sub-agent" (have their own system prompt and decision-making).

    For sub-agent skills, this agent uses the LLM to process tasks
    using its skill-specific tools.
    """

    def __init__(
        self,
        manifest: SkillManifest,
        skill_instance: Any,  # ISkill implementation
        message_bus: MessageBus,
        context: SharedContext,
        llm_provider: Any = None,
    ) -> None:
        super().__init__(
            agent_type="skill",
            name=manifest.name,
            message_bus=message_bus,
            context=context,
            capabilities=[
                AgentCapability(
                    name=cap,
                    description=f"Skill: {manifest.name} — {manifest.description}",
                    tags=manifest.keywords,
                )
                for cap in manifest.capabilities
            ],
        )
        self.manifest = manifest
        self.skill = skill_instance
        self._llm_provider = llm_provider
        self._tools: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        """Initialize the skill."""
        await super().initialize()

        # Get tools from the skill
        if hasattr(self.skill, 'get_tools'):
            self._tools = self.skill.get_tools()

        self.logger.info(
            "skill_initialized",
            skill=self.manifest.name,
            tool_count=len(self._tools),
        )

    async def execute_task(self, task: TaskRequest) -> TaskResult:
        """Execute a task using the skill's tools.

        For tool-only skills: directly execute the requested tool.
        For sub-agent skills: use LLM to decide which tools to call.
        """
        action = task.params.get("action", task.action)
        tool_params = task.params.get("params", {})

        try:
            if self.manifest.agent_type == "sub-agent" and self._llm_provider:
                # Sub-agent: use LLM to process the task
                result = await self._execute_as_sub_agent(task)
            else:
                # Tool-only: directly call the tool
                result = await self._execute_tool(action, tool_params)

            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.SUCCESS,
                output=result,
            )
        except Exception as e:
            self.logger.error(
                "skill_task_failed",
                skill=self.manifest.name,
                action=action,
                error=str(e),
            )
            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                error=str(e),
            )

    async def _execute_tool(self, tool_name: str, params: dict[str, Any]) -> str:
        """Execute a specific skill tool."""
        if hasattr(self.skill, 'execute_tool'):
            return await self.skill.execute_tool(tool_name, params)

        # Fallback: look up the tool and call it
        for tool in self._tools:
            if tool.get("name") == tool_name:
                handler = tool.get("handler")
                if handler:
                    result = await handler(**params)
                    return result if isinstance(result, str) else json.dumps(result, default=str)

        return json.dumps({
            "error": True,
            "message": f"Tool '{tool_name}' not found in skill '{self.manifest.name}'",
        })

    async def _execute_as_sub_agent(self, task: TaskRequest) -> str:
        """Process task as a sub-agent using LLM reasoning.

        This gives the skill its own agentic loop: think -> act -> observe.
        """
        if not self._llm_provider:
            return json.dumps({
                "error": True,
                "message": "LLM provider not available for sub-agent execution",
            })

        from k8s_agent.llm.provider import CompletionRequest
        from k8s_agent.llm.prompts import SKILL_AGENT_PROMPT, render_prompt
        from k8s_agent.shared.types import ContentBlock, Message

        # Build system prompt for this skill
        system_prompt = render_prompt(
            SKILL_AGENT_PROMPT,
            SKILL_NAME=self.manifest.name,
            SKILL_DESCRIPTION=self.manifest.description,
            SKILL_INSTRUCTIONS=self.manifest.agent_config.get("system_prompt", "")
            if self.manifest.agent_config
            else "Use your tools to complete the task.",
        )

        messages = [
            Message(
                role="user",
                content=[ContentBlock(type="text", text=task.params.get("task", str(task.params)))],
            )
        ]

        max_iterations = 5  # Sub-agents get fewer iterations
        for iteration in range(max_iterations):
            request = CompletionRequest(
                model=self._llm_provider.config.model,
                system_prompt=system_prompt,
                messages=messages,
                tools=self._tools,
                max_tokens=2048,
            )

            response = await self._llm_provider.complete(request)

            if response.tool_calls:
                # Execute tools and add results
                for tc in response.tool_calls:
                    tool_name = tc["name"]
                    tool_params = tc.get("input", {})
                    result = await self._execute_tool(tool_name, tool_params)

                    messages.append(Message(
                        role="assistant",
                        content=[
                            ContentBlock(
                                type="tool_use",
                                tool_use_id=tc["id"],
                                tool_name=tool_name,
                                tool_input=tool_params,
                            )
                        ],
                    ))
                    messages.append(Message(
                        role="user",
                        content=[
                            ContentBlock(
                                type="tool_result",
                                tool_use_id=tc["id"],
                                content=result,
                            )
                        ],
                    ))
            else:
                # No more tool calls — task complete
                return response.content

        return json.dumps({
            "error": True,
            "message": f"Sub-agent '{self.manifest.name}' reached max iterations",
        })

    async def get_available_tools(self) -> list[dict[str, Any]]:
        """Return this skill's tools."""
        return self._tools

    async def health_check(self) -> dict[str, Any]:
        """Check skill health."""
        base = await super().health_check()
        if hasattr(self.skill, 'health_check'):
            skill_health = await self.skill.health_check()
            base["skill_health"] = skill_health
        base["skill_name"] = self.manifest.name
        base["agent_type"] = self.manifest.agent_type
        return base
