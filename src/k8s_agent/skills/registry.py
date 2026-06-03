"""Skill registry — runtime registry of loaded skills with tool aggregation."""

from __future__ import annotations

from typing import Any

from k8s_agent.skills.interface import ISkill, SkillTool
from k8s_agent.skills.manifest import SkillManifest
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


class SkillRegistry:
    """Central registry for loaded skills.

    Provides:
    - Registration and lookup by skill name
    - Aggregation of all tools from all skills
    - Tool execution routing
    - Health monitoring
    """

    def __init__(self) -> None:
        self._skills: dict[str, ISkill] = {}
        self._manifests: dict[str, SkillManifest] = {}

    def register(self, skill: ISkill) -> None:
        """Register a loaded skill.

        Args:
            skill: The ISkill instance to register.
        """
        manifest = skill.get_manifest()
        name = manifest.name

        if name in self._skills:
            logger.warning("skill_already_registered", name=name)
            return

        self._skills[name] = skill
        self._manifests[name] = manifest
        logger.info("skill_registered", name=name, version=manifest.version)

    def unregister(self, name: str) -> bool:
        """Remove a skill from the registry.

        Args:
            name: Skill name to remove.

        Returns:
            True if the skill was registered.
        """
        if name in self._skills:
            del self._skills[name]
            del self._manifests[name]
            logger.info("skill_unregistered", name=name)
            return True
        return False

    def get(self, name: str) -> ISkill | None:
        """Get a skill by name."""
        return self._skills.get(name)

    def get_manifest(self, name: str) -> SkillManifest | None:
        """Get a skill's manifest by name."""
        return self._manifests.get(name)

    def list_skills(self) -> list[dict[str, Any]]:
        """List all registered skills with basic info."""
        return [
            {
                "name": m.name,
                "version": m.version,
                "description": m.description,
                "agent_type": m.agent_type,
                "capabilities": m.capabilities,
                "tool_count": len(self._skills[m.name].get_tools()),
            }
            for m in self._manifests.values()
        ]

    def get_all_tools(self) -> list[dict[str, Any]]:
        """Get all tools from all registered skills.

        Returns:
            List of tools in Anthropic-compatible format.
        """
        all_tools = []
        for name, skill in self._skills.items():
            for tool in skill.get_tools():
                all_tools.append({
                    "name": f"skill/{name}/{tool.name}",
                    "description": f"[Skill: {name}] {tool.description}",
                    "input_schema": tool.input_schema,
                    "_skill": name,
                    "_tool": tool.name,
                })
        return all_tools

    async def execute_tool(
        self, skill_name: str, tool_name: str, params: dict[str, Any]
    ) -> str:
        """Execute a tool from a specific skill.

        Args:
            skill_name: Name of the skill.
            tool_name: Name of the tool.
            params: Tool parameters.

        Returns:
            Tool execution result as string.
        """
        skill = self._skills.get(skill_name)
        if not skill:
            return f'{{"error": true, "message": "Skill \\"{skill_name}\\" not found"}}'

        return await skill.execute_tool(tool_name, params)

    def find_tool(self, tool_name: str) -> tuple[ISkill, SkillTool] | None:
        """Find a tool by name across all skills.

        Args:
            tool_name: The tool name to find.

        Returns:
            (skill, tool) tuple or None.
        """
        for skill in self._skills.values():
            for tool in skill.get_tools():
                if tool.name == tool_name:
                    return skill, tool
        return None

    async def health_check_all(self) -> dict[str, Any]:
        """Check health of all registered skills."""
        results = {}
        for name, skill in self._skills.items():
            try:
                health = await skill.health_check()
                results[name] = health
            except Exception as e:
                results[name] = {"healthy": False, "message": str(e)}
        return results

    async def activate_all(self) -> None:
        """Activate all registered skills."""
        for name, skill in self._skills.items():
            try:
                await skill.activate()
                logger.debug("skill_activated", name=name)
            except Exception as e:
                logger.error("skill_activate_error", name=name, error=str(e))

    async def deactivate_all(self) -> None:
        """Deactivate all registered skills."""
        for name, skill in self._skills.items():
            try:
                await skill.deactivate()
            except Exception as e:
                logger.error("skill_deactivate_error", name=name, error=str(e))

    async def cleanup_all(self) -> None:
        """Clean up all registered skills."""
        for name, skill in self._skills.items():
            try:
                await skill.cleanup()
            except Exception as e:
                logger.error("skill_cleanup_error", name=name, error=str(e))
        self._skills.clear()
        self._manifests.clear()
