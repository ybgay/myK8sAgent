"""LLM integration layer — provider abstraction, Anthropic and DeepSeek adapters."""

from k8s_agent.llm.provider import LLMProvider, CompletionRequest, CompletionResponse, CompletionEvent
from k8s_agent.llm.anthropic_client import AnthropicProvider
from k8s_agent.llm.deepseek_client import DeepSeekProvider
from k8s_agent.llm.conversation import ConversationManager
from k8s_agent.llm.prompts import (
    MASTER_AGENT_PROMPT,
    K8S_AGENT_PROMPT,
    SKILL_AGENT_PROMPT,
    render_prompt,
)

__all__ = [
    "LLMProvider",
    "CompletionRequest",
    "CompletionResponse",
    "CompletionEvent",
    "AnthropicProvider",
    "DeepSeekProvider",
    "ConversationManager",
    "MASTER_AGENT_PROMPT",
    "K8S_AGENT_PROMPT",
    "SKILL_AGENT_PROMPT",
    "render_prompt",
]

# Provider factory
def create_provider(provider_type: str = "auto", **kwargs):
    """Create an LLM provider based on configuration or explicit type.

    Args:
        provider_type: "anthropic", "deepseek", or "auto" (reads from config/env).
        **kwargs: Passed through to the provider constructor.

    Returns:
        An LLMProvider instance.
    """
    import os
    from k8s_agent.shared.config import get_settings

    settings = get_settings()

    if provider_type == "auto":
        # Prefer env var, then settings (which reads .env file), then default
        provider_type = os.environ.get("LLM_PROVIDER") or settings.llm.provider or "anthropic"

    if provider_type == "deepseek":
        # Prefer explicit kwargs, then env var, then settings (which reads .env file)
        api_key = kwargs.pop("api_key", None) or os.environ.get("DEEPSEEK_API_KEY") or ""
        model = kwargs.pop("model", None) or os.environ.get("DEEPSEEK_MODEL") or settings.llm.model or "deepseek-chat"
        base_url = kwargs.pop("base_url", None) or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
        return DeepSeekProvider(
            api_key=api_key,
            model=model,
            base_url=base_url,
            **kwargs,
        )
    else:
        # Default: Anthropic
        return AnthropicProvider(**kwargs)

