"""DeepSeek V4 Pro provider — OpenAI-compatible API.

DeepSeek API: https://api.deepseek.com/v1
Model: deepseek-chat (V4 Pro)

Usage:
    provider = DeepSeekProvider(
        api_key="sk-xxx",
        model="deepseek-chat",    # V4 Pro
    )
    # Or set env: DEEPSEEK_API_KEY=sk-xxx
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import AsyncOpenAI
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
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


class DeepSeekProvider(LLMProvider):
    """DeepSeek V4 Pro LLM provider using OpenAI-compatible API.

    DeepSeek models:
    - deepseek-chat: V4 Pro (latest, recommended)
    - deepseek-reasoner: DeepSeek-R1 (reasoning model)

    Features:
    - Exponential backoff retry on transient errors
    - Streaming support
    - Tool/function calling support
    - Custom API base URL (for proxies or self-hosted)
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com/v1",
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> None:
        """Initialize DeepSeek provider.

        Args:
            api_key: DeepSeek API key. Falls back to DEEPSEEK_API_KEY env var.
            model: Model name. "deepseek-chat" for V4 Pro, "deepseek-reasoner" for R1.
            base_url: API base URL. Default is DeepSeek official.
                      Can be changed for proxies: "https://your-proxy.com/v1"
            max_tokens: Default max output tokens.
            temperature: Default temperature (0.0-2.0).
        """
        import os

        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self._model = model
        self._base_url = base_url
        self._max_tokens = max_tokens
        self._temperature = temperature

        # Build a config object compatible with what the orchestrator expects
        self.config = LLMConfig(
            provider="deepseek",
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            api_key=self.api_key,
        )

        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self._base_url,
        )

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Non-streaming completion."""
        messages = self._build_messages(request)
        tools = self._convert_tools_to_openai(request.tools)

        kwargs: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": messages,
            "max_tokens": request.max_tokens or self._max_tokens,
            "temperature": request.temperature if request.temperature is not None else self._temperature,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            response = await self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]

            text = choice.message.content or ""
            tool_calls = []
            if choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    tool_calls.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": json.loads(tc.function.arguments) if tc.function.arguments else {},
                    })

            return CompletionResponse(
                content=text,
                tool_calls=tool_calls,
                stop_reason=choice.finish_reason or "stop",
                usage={
                    "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "output_tokens": response.usage.completion_tokens if response.usage else 0,
                },
            )
        except Exception as e:
            logger.error("deepseek_complete_error", error=str(e))
            raise

    async def complete_stream(
        self, request: CompletionRequest
    ) -> AsyncIterator[CompletionEvent]:
        """Streaming completion."""
        messages = self._build_messages(request)
        tools = self._convert_tools_to_openai(request.tools)

        kwargs: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": messages,
            "max_tokens": request.max_tokens or self._max_tokens,
            "temperature": request.temperature if request.temperature is not None else self._temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools

        try:
            tool_call_buffer: dict[int, dict[str, Any]] = {}

            logger.debug("deepseek_stream_request", model=kwargs.get("model"), base_url=self._base_url)
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # Text delta
                if delta.content:
                    yield CompletionEvent(type="text_delta", text=delta.content)

                # Tool call deltas
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_call_buffer:
                            tool_call_buffer[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "arguments": "",
                            }

                        if tc_delta.id:
                            tool_call_buffer[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_call_buffer[idx]["name"] = tc_delta.function.name
                                yield CompletionEvent(
                                    type="tool_use_start",
                                    tool_use_id=tool_call_buffer[idx]["id"],
                                    tool_name=tc_delta.function.name,
                                )
                            if tc_delta.function.arguments:
                                tool_call_buffer[idx]["arguments"] += tc_delta.function.arguments
                                yield CompletionEvent(
                                    type="tool_use_delta",
                                    tool_use_id=tool_call_buffer[idx]["id"],
                                    input_json_delta=tc_delta.function.arguments,
                                )

                # Check finish reason for this chunk
                if chunk.choices[0].finish_reason:
                    # Emit tool_use_end for buffered tool calls
                    for tc_data in tool_call_buffer.values():
                        if tc_data["name"]:
                            try:
                                parsed_input = json.loads(tc_data["arguments"])
                            except json.JSONDecodeError:
                                parsed_input = {}
                            yield CompletionEvent(
                                type="tool_use_end",
                                tool_use_id=tc_data["id"],
                                tool_name=tc_data["name"],
                                tool_input=parsed_input,
                            )

            yield CompletionEvent(type="message_stop")

        except Exception as e:
            error_detail = f"{type(e).__name__}: {e}"
            # Try to get more detail from OpenAI API error
            if hasattr(e, 'body'):
                error_detail += f" | body={e.body}"
            if hasattr(e, 'response'):
                try:
                    error_detail += f" | status={e.response.status_code}"
                except Exception:
                    pass
            logger.error("deepseek_stream_error", error=error_detail, base_url=self._base_url, model=kwargs.get("model"))
            yield CompletionEvent(type="error", error=e)

    async def health_check(self) -> bool:
        """Check if DeepSeek API is reachable."""
        try:
            response = await self._client.models.list()
            return len(response.data) > 0
        except Exception:
            return False

    @property
    def model_info(self) -> dict[str, Any]:
        return {
            "provider": "deepseek",
            "model": self._model,
            "base_url": self._base_url,
            "max_tokens": self._max_tokens,
        }

    # ------------------------------------------------------------------
    # Message / Tool conversion (OpenAI format)
    # ------------------------------------------------------------------

    def _build_messages(self, request: CompletionRequest) -> list[dict[str, Any]]:
        """Build OpenAI-format messages including system prompt."""
        messages: list[dict[str, Any]] = []

        # System prompt as first message
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})

        # Convert conversation messages
        for msg in request.messages:
            if isinstance(msg.content, str):
                messages.append({"role": msg.role, "content": msg.content})
            else:
                # Multi-block content
                if msg.role == "assistant":
                    parts: list[dict[str, Any]] = []
                    tool_calls: list[dict[str, Any]] = []
                    for block in msg.content:
                        if block.type == "text" and block.text:
                            parts.append({"type": "text", "text": block.text})
                        elif block.type == "tool_use":
                            tool_calls.append({
                                "id": block.tool_use_id,
                                "type": "function",
                                "function": {
                                    "name": block.tool_name,
                                    "arguments": json.dumps(block.tool_input or {}),
                                },
                            })
                    assistant_msg: dict[str, Any] = {"role": "assistant"}
                    if parts:
                        assistant_msg["content"] = parts if len(parts) > 1 else parts[0]["text"]
                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls
                    messages.append(assistant_msg)

                elif msg.role == "user":
                    # Could be tool results
                    for block in msg.content:
                        if block.type == "tool_result":
                            messages.append({
                                "role": "tool",
                                "tool_call_id": block.tool_use_id,
                                "content": block.content or "",
                            })
                        elif block.type == "text" and block.text:
                            messages.append({"role": "user", "content": block.text})

        return messages

    def _convert_tools_to_openai(
        self, tools: list[Any]
    ) -> list[dict[str, Any]]:
        """Convert internal tool definitions to OpenAI function format."""
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                result.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {}),
                    },
                })
            elif hasattr(tool, "name"):
                result.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                })
        return result
