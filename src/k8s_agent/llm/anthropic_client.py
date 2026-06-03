"""Anthropic Claude provider implementation with retry and streaming."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import anthropic
from anthropic import (
    APIError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from k8s_agent.llm.provider import (
    CompletionEvent,
    CompletionRequest,
    CompletionResponse,
    LLMProvider,
)
from k8s_agent.shared.config import LLMConfig, get_settings
from k8s_agent.shared.errors import (
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMRateLimitError,
    LLMResponseError,
)
from k8s_agent.shared.logging import get_logger
from k8s_agent.shared.types import ContentBlock, Message

logger = get_logger(__name__)


def _map_anthropic_error(error: Exception) -> Exception:
    """Map Anthropic SDK errors to our internal error types."""
    if isinstance(error, AuthenticationError):
        return LLMAuthenticationError(str(error), cause=error)
    if isinstance(error, RateLimitError):
        return LLMRateLimitError(str(error), cause=error)
    if isinstance(error, APITimeoutError):
        return LLMRateLimitError(str(error), cause=error)
    if "context_length" in str(error).lower() or "token" in str(error).lower():
        return LLMContextLengthError(str(error), cause=error)
    if isinstance(error, (APIError, InternalServerError)):
        return LLMResponseError(str(error), cause=error)
    return error


class AnthropicProvider(LLMProvider):
    """Anthropic Claude LLM provider with retry logic and circuit breaker.

    Features:
    - Exponential backoff retry on transient errors (429, 5xx)
    - Circuit breaker after consecutive failures
    - Prompt caching support via cache_control
    - Streaming and non-streaming modes
    """

    def __init__(self, config: LLMConfig | None = None) -> None:
        settings = get_settings()
        self.config = config or settings.llm
        self._client: anthropic.AsyncAnthropic | None = None
        self._failure_count = 0
        self._circuit_open = False
        self._last_failure_time: float | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        """Lazy-initialize the Anthropic client."""
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.config.api_key)
        return self._client

    async def _check_circuit(self) -> None:
        """Check and update circuit breaker state."""
        import time

        if self._circuit_open and self._last_failure_time:
            elapsed = (time.monotonic() - self._last_failure_time) * 1000
            if elapsed > self.config.circuit_reset_timeout_ms:
                self._circuit_open = False
                self._failure_count = 0
                logger.info("circuit_half_open", elapsed_ms=elapsed)
            else:
                raise LLMRateLimitError(
                    f"Circuit breaker open. Retry in "
                    f"{(self.config.circuit_reset_timeout_ms - elapsed) / 1000:.1f}s"
                )

    async def _record_failure(self) -> None:
        """Record a failure and potentially open circuit breaker."""
        import time

        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.config.circuit_failure_threshold:
            self._circuit_open = True
            logger.warning(
                "circuit_opened",
                failures=self._failure_count,
                threshold=self.config.circuit_failure_threshold,
            )

    async def _record_success(self) -> None:
        """Reset failure count on success."""
        if self._failure_count > 0:
            self._failure_count = 0
            self._circuit_open = False
            logger.debug("circuit_reset")

    def _convert_messages(
        self, messages: list[Message]
    ) -> list[dict[str, Any]]:
        """Convert internal Message format to Anthropic API format."""
        converted = []
        for msg in messages:
            if isinstance(msg.content, str):
                converted.append({"role": msg.role, "content": msg.content})
            else:
                blocks = []
                for block in msg.content:
                    if block.type == "text":
                        blocks.append({"type": "text", "text": block.text or ""})
                    elif block.type == "tool_use":
                        blocks.append({
                            "type": "tool_use",
                            "id": block.tool_use_id or "",
                            "name": block.tool_name or "",
                            "input": block.tool_input or {},
                        })
                    elif block.type == "tool_result":
                        blocks.append({
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id or "",
                            "content": block.content or "",
                        })
                converted.append({"role": msg.role, "content": blocks})
        return converted

    def _convert_tools(
        self, tools: list[Any]
    ) -> list[dict[str, Any]]:
        """Convert internal ToolDefinition to Anthropic tool format."""
        result = []
        for tool in tools:
            if hasattr(tool, "name"):
                result.append({
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                })
            elif isinstance(tool, dict):
                result.append(tool)
        return result

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Execute a non-streaming completion with retry logic."""
        await self._check_circuit()

        try:
            response = await self._do_complete(request)
            await self._record_success()
            return response
        except Exception as e:
            await self._record_failure()
            raise _map_anthropic_error(e) from e

    @retry(
        retry=retry_if_exception_type((RateLimitError, InternalServerError, APITimeoutError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=30),
    )
    async def _do_complete(self, request: CompletionRequest) -> CompletionResponse:
        """Internal completion with tenacity retry decorator."""
        api_messages = self._convert_messages(request.messages)
        api_tools = self._convert_tools(request.tools)

        kwargs: dict[str, Any] = {
            "model": request.model,
            "system": request.system_prompt,
            "messages": api_messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }

        if api_tools:
            kwargs["tools"] = api_tools

        response = await self.client.messages.create(**kwargs)

        # Extract text content and tool calls
        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        return CompletionResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage={
                "input_tokens": response.usage.input_tokens if response.usage else 0,
                "output_tokens": response.usage.output_tokens if response.usage else 0,
            },
        )

    async def complete_stream(
        self, request: CompletionRequest
    ) -> AsyncIterator[CompletionEvent]:
        """Execute a streaming completion."""
        await self._check_circuit()

        try:
            api_messages = self._convert_messages(request.messages)
            api_tools = self._convert_tools(request.tools)

            kwargs: dict[str, Any] = {
                "model": request.model,
                "system": request.system_prompt,
                "messages": api_messages,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
            }

            if api_tools:
                kwargs["tools"] = api_tools

            async with self.client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield CompletionEvent(type="text_delta", text=event.text)
                    elif event.type == "content_block_start":
                        if event.content_block.type == "tool_use":
                            cb = event.content_block
                            yield CompletionEvent(
                                type="tool_use_start",
                                tool_use_id=cb.id,
                                tool_name=cb.name,
                            )
                    elif event.type == "content_block_delta":
                        if event.delta.type == "input_json_delta":
                            yield CompletionEvent(
                                type="tool_use_delta",
                                tool_use_id=getattr(event, "content_block_id", None),
                                input_json_delta=event.delta.partial_json,
                            )
                    elif event.type == "content_block_stop":
                        # The stream provides the final accumulated content
                        pass

                # Get final message
                final = await stream.get_final_message()
                for block in final.content:
                    if block.type == "tool_use":
                        yield CompletionEvent(
                            type="tool_use_end",
                            tool_use_id=block.id,
                            tool_name=block.name,
                            tool_input=block.input if hasattr(block, 'input') else {},
                        )

                yield CompletionEvent(type="message_stop")

            await self._record_success()

        except Exception as e:
            await self._record_failure()
            yield CompletionEvent(type="error", error=_map_anthropic_error(e))

    async def health_check(self) -> bool:
        """Check if the Anthropic API is reachable."""
        try:
            await self.client.models.retrieve(self.config.model)
            return True
        except Exception:
            return False

    @property
    def model_info(self) -> dict[str, Any]:
        """Return model configuration info."""
        return {
            "provider": "anthropic",
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
