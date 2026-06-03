"""Built-in skills for k8s-agent."""

from k8s_agent.skills.builtin.troubleshooting import TroubleshootingSkill
from k8s_agent.skills.builtin.deployment_analyzer import DeploymentAnalyzerSkill
from k8s_agent.skills.builtin.cost_optimizer import CostOptimizerSkill

__all__ = ["TroubleshootingSkill", "DeploymentAnalyzerSkill", "CostOptimizerSkill"]

BUILTIN_SKILLS = [TroubleshootingSkill, DeploymentAnalyzerSkill, CostOptimizerSkill]
