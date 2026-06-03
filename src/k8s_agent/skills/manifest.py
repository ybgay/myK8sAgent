"""Skill manifest — metadata definition for skills."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillManifest:
    """Metadata manifest describing a skill.

    This is used for discovery, validation, dependency resolution,
    and agent configuration.

    Fields are compatible with the shared SkillManifest Pydantic model.
    """

    name: str  # Unique identifier, e.g. "k8s-troubleshooting"
    version: str = "1.0.0"
    description: str = ""
    author: str = "unknown"
    license: str | None = None
    keywords: list[str] = field(default_factory=list)
    agent_type: str = "tool-only"  # "tool-only" | "sub-agent"
    requires: dict[str, list[str]] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    agent_config: dict | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "license": self.license,
            "keywords": self.keywords,
            "agent_type": self.agent_type,
            "requires": self.requires,
            "capabilities": self.capabilities,
            "agent_config": self.agent_config,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillManifest":
        """Create from a dictionary (e.g., from skill.json)."""
        return cls(
            name=data["name"],
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            author=data.get("author", "unknown"),
            license=data.get("license"),
            keywords=data.get("keywords", []),
            agent_type=data.get("agent_type", "tool-only"),
            requires=data.get("requires", {}),
            capabilities=data.get("capabilities", []),
            agent_config=data.get("agent_config"),
        )

    def validate(self) -> list[str]:
        """Validate the manifest. Returns a list of validation errors (empty = valid)."""
        errors = []
        if not self.name:
            errors.append("name is required")
        if not self.version:
            errors.append("version is required")
        if self.agent_type not in ("tool-only", "sub-agent"):
            errors.append(f"agent_type must be 'tool-only' or 'sub-agent', got '{self.agent_type}'")
        if self.agent_type == "sub-agent" and not self.agent_config:
            errors.append("agent_type 'sub-agent' requires agent_config with system_prompt")
        if self.agent_type == "sub-agent" and self.agent_config:
            if "system_prompt" not in self.agent_config:
                errors.append("sub-agent agent_config must include 'system_prompt'")
        return errors
