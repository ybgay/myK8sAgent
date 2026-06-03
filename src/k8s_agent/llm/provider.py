"""Abstract LLM provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from k8s_agent.shared.types import Message, ToolDefinition


@dataclass
class CompletionRequest:
    """Request for an LLM completion."""

    model: str
    system_prompt: str
    messages: list[Message]
    tools: list[ToolDefinition] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 0.2
    stream: bool = False


@dataclass
class CompletionResponse:
    """Response from an LLM completion."""

    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class CompletionEvent:
    """A streaming completion event."""

    type: str  # "text_delta" | "tool_use_start" | "tool_use_delta" | "tool_use_end" | "message_stop" | "error"
    text: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    input_json_delta: str | None = None
    error: Exception | None = None


class LLMProvider(ABC):
    """Abstract base class for LLM providers.

    Implementations must provide complete() and, optionally, complete_stream().
    """

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Send a completion request and return the response."""

    @abstractmethod
    async def complete_stream(
        self, request: CompletionRequest
    ) -> AsyncIterator[CompletionEvent]:
        """Send a completion request and stream events."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the provider is healthy and reachable."""

    @property
    @abstractmethod
    def model_info(self) -> dict[str, Any]:
        """Return information about the current model."""
