"""Pluggable skill system for k8s-agent."""

from k8s_agent.skills.interface import ISkill, SkillContext, SkillTool, SkillToolContext
from k8s_agent.skills.manifest import SkillManifest
from k8s_agent.skills.loader import SkillLoader
from k8s_agent.skills.registry import SkillRegistry

__all__ = [
    "ISkill",
    "SkillContext",
    "SkillTool",
    "SkillToolContext",
    "SkillManifest",
    "SkillLoader",
    "SkillRegistry",
]
