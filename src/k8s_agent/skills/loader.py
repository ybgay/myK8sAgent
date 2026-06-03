"""Skill loader — discovers and loads skills from various sources.

Discovery paths (searched in order):
1. Built-in skills: k8s_agent/skills/builtin/*
2. User skills: ~/.k8s-agent/skills/*
3. Project skills: ./.k8s-agent/skills/*
4. Python packages: any package with "k8s-agent-skill-" prefix
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from k8s_agent.skills.interface import ISkill, SkillContext
from k8s_agent.skills.manifest import SkillManifest
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


class SkillLoader:
    """Discovers and loads skill modules from configured paths."""

    def __init__(self, context: SkillContext) -> None:
        self.context = context
        self._loaded_modules: dict[str, Any] = {}

    def discover(self, extra_paths: list[str] | None = None) -> list[tuple[str, SkillManifest]]:
        """Discover all available skills and their manifests.

        Args:
            extra_paths: Additional directories to search.

        Returns:
            List of (skill_path, SkillManifest) tuples.
        """
        discovered: list[tuple[str, SkillManifest]] = []

        # 1. Built-in skills
        builtin_dir = Path(__file__).parent / "builtin"
        discovered.extend(self._scan_directory(builtin_dir, source="builtin"))

        # 2. User and project paths
        search_paths = extra_paths or []
        search_paths.extend([
            os.path.expanduser("~/.k8s-agent/skills"),
            ".k8s-agent/skills",
        ])

        for path_str in search_paths:
            path = Path(path_str).expanduser().resolve()
            if path.exists() and path.is_dir():
                discovered.extend(self._scan_directory(path, source=str(path)))

        # 3. Python packages (k8s-agent-skill-*)
        discovered.extend(self._discover_python_packages())

        logger.info("skill_discovery_complete", found=len(discovered))
        return discovered

    def _scan_directory(
        self, directory: Path, source: str = "unknown"
    ) -> list[tuple[str, SkillManifest]]:
        """Scan a directory for skills (subdirectories with skill.json)."""
        results = []
        if not directory.exists():
            return results

        for item in directory.iterdir():
            if item.is_dir():
                manifest_path = item / "skill.json"
                if manifest_path.exists():
                    try:
                        with open(manifest_path, encoding="utf-8") as f:
                            data = json.load(f)
                        manifest = SkillManifest.from_dict(data)
                        errors = manifest.validate()
                        if errors:
                            logger.warning(
                                "skill_manifest_invalid",
                                path=str(item),
                                errors=errors,
                            )
                            continue
                        results.append((str(item), manifest))
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(
                            "skill_manifest_parse_error",
                            path=str(manifest_path),
                            error=str(e),
                        )

            # Also check for Python skills (single-file skills)
            elif item.suffix == ".py" and item.stem != "__init__":
                # Check for a get_manifest function
                try:
                    spec = importlib.util.spec_from_file_location(
                        f"skill_{item.stem}", str(item)
                    )
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        sys.modules[f"skill_{item.stem}"] = module
                        spec.loader.exec_module(module)
                        if hasattr(module, "get_manifest"):
                            manifest = module.get_manifest()
                            if isinstance(manifest, SkillManifest):
                                results.append((str(item), manifest))
                except Exception as e:
                    logger.warning("skill_file_load_error", path=str(item), error=str(e))

        return results

    def _discover_python_packages(self) -> list[tuple[str, SkillManifest]]:
        """Discover skills installed as Python packages.

        Packages named 'k8s-agent-skill-*' with a 'k8s_agent_skill' entry point
        are automatically discovered.
        """
        results = []
        # Check for packages using importlib.metadata (Python 3.8+)
        try:
            from importlib.metadata import entry_points

            for ep in entry_points(group="k8s_agent.skills"):
                try:
                    skill_module = ep.load()
                    if hasattr(skill_module, "get_manifest"):
                        manifest = skill_module.get_manifest()
                        if isinstance(manifest, SkillManifest):
                            results.append((ep.name, manifest))
                except Exception as e:
                    logger.warning("skill_entry_point_load_error", entry=ep.name, error=str(e))
        except Exception:
            pass

        return results

    async def load(self, skill_path: str, manifest: SkillManifest) -> ISkill:
        """Load a skill from a path and return its ISkill instance.

        Args:
            skill_path: Path to the skill directory or file.
            manifest: The skill's manifest.

        Returns:
            The loaded ISkill instance.

        Raises:
            SkillLoadError: If the skill cannot be loaded.
        """
        path = Path(skill_path)

        # Case 1: Directory skill with index.py
        if path.is_dir():
            init_file = path / "index.py"
            if not init_file.exists():
                init_file = path / "__init__.py"

            if init_file.exists():
                module_name = f"skill_{manifest.name.replace('-', '_')}"
                spec = importlib.util.spec_from_file_location(
                    module_name, str(init_file)
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)

                    skill_instance = self._extract_skill_instance(module, manifest)
                    if skill_instance:
                        await skill_instance.initialize(self.context)
                        logger.info("skill_loaded", name=manifest.name, version=manifest.version)
                        return skill_instance

        # Case 2: Single-file Python skill
        elif path.suffix == ".py":
            module_name = f"skill_{path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, str(path))
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                skill_instance = self._extract_skill_instance(module, manifest)
                if skill_instance:
                    await skill_instance.initialize(self.context)
                    logger.info("skill_loaded", name=manifest.name, version=manifest.version)
                    return skill_instance

        # Case 3: Python module path (e.g., "my_package.my_skill")
        else:
            try:
                module = importlib.import_module(skill_path)
                skill_instance = self._extract_skill_instance(module, manifest)
                if skill_instance:
                    await skill_instance.initialize(self.context)
                    return skill_instance
            except ImportError as e:
                raise ImportError(f"Failed to import skill '{manifest.name}' from {skill_path}: {e}") from e

        raise ImportError(f"Could not load skill '{manifest.name}' from {skill_path}")

    def _extract_skill_instance(self, module: Any, manifest: SkillManifest) -> ISkill | None:
        """Extract an ISkill instance from a Python module.

        Looks for:
        1. A class inheriting from ISkill
        2. A 'create_skill()' function
        3. A 'skill' module-level variable
        """
        # Look for ISkill subclass
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, ISkill)
                and attr is not ISkill
            ):
                return attr()

        # Look for factory function
        if hasattr(module, "create_skill"):
            return module.create_skill()

        # Look for module-level instance
        if hasattr(module, "skill"):
            skill = module.skill
            if isinstance(skill, ISkill):
                return skill

        return None
