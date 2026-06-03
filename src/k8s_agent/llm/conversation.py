"""Conversation manager with token counting and history compaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from k8s_agent.shared.types import ContentBlock, Message


@dataclass
class ConversationManager:
    """Manages conversation history with automatic compaction.

    Tracks messages, tool calls, and token usage to stay within model limits.
    Supports conversation compaction (summarization) when context gets too long.
    """

    messages: list[Message] = field(default_factory=list)
    max_messages: int = 100
    system_prompt: str = ""
    _estimated_tokens: int = 0

    def add_user_message(self, content: str) -> None:
        """Add a user message to the conversation."""
        self.messages.append(
            Message(
                role="user",
                content=[
                    ContentBlock(type="text", text=content)
                ],
            )
        )

    def add_assistant_message(self, content: str) -> None:
        """Add an assistant text message."""
        self.messages.append(
            Message(
                role="assistant",
                content=[
                    ContentBlock(type="text", text=content)
                ],
            )
        )

    def add_tool_use(self, tool_use_id: str, tool_name: str, tool_input: dict[str, Any]) -> None:
        """Add an assistant tool_use block (usually combined with text)."""
        self.messages.append(
            Message(
                role="assistant",
                content=[
                    ContentBlock(
                        type="tool_use",
                        tool_use_id=tool_use_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )
                ],
            )
        )

    def add_tool_result(self, tool_use_id: str, result: str) -> None:
        """Add a tool result as a user message."""
        self.messages.append(
            Message(
                role="user",
                content=[
                    ContentBlock(
                        type="tool_result",
                        tool_use_id=tool_use_id,
                        content=result,
                    )
                ],
            )
        )

    def add_full_assistant_turn(
        self,
        text: str,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        """Add a complete assistant turn with text and tool calls."""
        blocks: list[ContentBlock] = []
        if text:
            blocks.append(ContentBlock(type="text", text=text))
        for tc in tool_calls:
            blocks.append(
                ContentBlock(
                    type="tool_use",
                    tool_use_id=tc.get("id", ""),
                    tool_name=tc.get("name", ""),
                    tool_input=tc.get("input", {}),
                )
            )
        self.messages.append(Message(role="assistant", content=blocks))

    def add_tool_results_batch(
        self, results: list[tuple[str, str]]
    ) -> None:
        """Add multiple tool results at once.

        Args:
            results: List of (tool_use_id, result_text) tuples.
        """
        blocks = [
            ContentBlock(type="tool_result", tool_use_id=tuid, content=text)
            for tuid, text in results
        ]
        self.messages.append(Message(role="user", content=blocks))

    def estimate_tokens(self) -> int:
        """Rough token estimation (4 chars ≈ 1 token for English text)."""
        total = len(self.system_prompt) // 4
        for msg in self.messages:
            if isinstance(msg.content, str):
                total += len(msg.content) // 4
            else:
                for block in msg.content:
                    text = block.text or block.content or ""
                    total += len(text) // 4
                    if block.tool_input:
                        total += len(str(block.tool_input)) // 4
        self._estimated_tokens = total
        return total

    def needs_compaction(self, max_tokens: int = 100_000) -> bool:
        """Check if conversation should be compacted."""
        return self.estimate_tokens() > max_tokens * 0.8

    def compact(self, keep_last: int = 10) -> str:
        """Remove old messages, keeping recent ones and returning a summary.

        In a full implementation, this would use the LLM to summarize
        removed messages. For now, it truncates.

        Args:
            keep_last: Number of most recent messages to preserve.

        Returns:
            Summary text of removed messages.
        """
        if len(self.messages) <= keep_last:
            return ""

        removed = self.messages[:-keep_last]
        self.messages = self.messages[-keep_last:]

        summary = f"[Compacted {len(removed)} earlier messages]"
        return summary

    def to_api_messages(self) -> list[dict[str, Any]]:
        """Convert messages to Anthropic API-compatible format."""
        result = []
        for msg in self.messages:
            if isinstance(msg.content, str):
                result.append({"role": msg.role, "content": msg.content})
            else:
                blocks = []
                for block in msg.content:
                    if block.type == "text":
                        blocks.append({"type": "text", "text": block.text or ""})
                    elif block.type == "tool_use":
                        blocks.append({
                            "type": "tool_use",
                            "id": block.tool_use_id,
                            "name": block.tool_name,
                            "input": block.tool_input or {},
                        })
                    elif block.type == "tool_result":
                        blocks.append({
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content or "",
                        })
                result.append({"role": msg.role, "content": blocks})
        return result

    def clear(self) -> None:
        """Clear conversation history."""
        self.messages.clear()
        self._estimated_tokens = 0

    def __len__(self) -> int:
        return len(self.messages)

    def get_last_n_messages(self, n: int) -> list[Message]:
        """Get the last N messages."""
        return self.messages[-n:] if n > 0 else []

    def to_dict(self) -> dict[str, Any]:
        """Serialize conversation to a dict."""
        return {
            "messages": [m.model_dump() for m in self.messages],
            "estimated_tokens": self._estimated_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationManager":
        """Deserialize conversation from a dict."""
        mgr = cls()
        mgr.messages = [Message(**m) for m in data.get("messages", [])]
        mgr._estimated_tokens = data.get("estimated_tokens", 0)
        return mgr
