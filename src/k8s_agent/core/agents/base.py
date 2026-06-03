"""Base agent class — provides common lifecycle and tool execution."""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

from k8s_agent.core.communication.message_bus import MessageBus
from k8s_agent.core.communication.context import SharedContext
from k8s_agent.shared.logging import get_logger
from k8s_agent.shared.types import (
    AgentCapability,
    AgentIdentity,
    AgentStatus,
    TaskRequest,
    TaskResult,
    TaskStatus,
    ToolResult,
)


class BaseAgent(ABC):
    """Abstract base for all agents.

    Provides:
    - Identity and capability registration
    - Message bus integration for task delegation
    - Tool execution lifecycle
    - Health monitoring
    """

    def __init__(
        self,
        agent_type: str,
        name: str,
        message_bus: MessageBus,
        context: SharedContext,
        capabilities: list[AgentCapability] | None = None,
    ) -> None:
        self.identity = AgentIdentity(
            agent_type=agent_type,
            name=name,
            capabilities=capabilities or [],
        )
        self.message_bus = message_bus
        self.context = context
        self.logger = get_logger(f"agent.{agent_type}.{name}")

        # Register task handler on the message bus
        topic = f"agent.{agent_type}.*"
        self.message_bus.on(topic)(self._handle_message)

        # Heartbeat
        self._heartbeat_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def execute_task(self, task: TaskRequest) -> TaskResult:
        """Execute a task and return the result.

        Subclasses must implement this.
        """

    @abstractmethod
    async def get_available_tools(self) -> list[dict[str, Any]]:
        """Return the list of tools this agent can use.

        Tools are returned in Anthropic-compatible format:
        {name, description, input_schema}
        """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize the agent. Called once before any tasks."""
        self.identity.status = AgentStatus.READY
        self.logger.info("agent_initialized", agent_id=self.identity.agent_id)

        # Announce presence on the bus
        await self.message_bus.publish(
            "system.agent.announce",
            {
                "agent_id": self.identity.agent_id,
                "agent_type": self.identity.agent_type,
                "name": self.identity.name,
                "capabilities": [c.model_dump() for c in self.identity.capabilities],
                "status": self.identity.status.value,
            },
        )

    async def start_heartbeat(self, interval: float = 10.0) -> None:
        """Start periodic heartbeat messages."""
        async def heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(interval)
                await self.message_bus.publish(
                    f"agent.{self.identity.agent_type}.heartbeat",
                    {
                        "agent_id": self.identity.agent_id,
                        "name": self.identity.name,
                        "status": self.identity.status.value,
                    },
                )
        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    async def shutdown(self) -> None:
        """Gracefully shut down the agent."""
        self.identity.status = AgentStatus.IDLE
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self.logger.info("agent_shutdown", agent_id=self.identity.agent_id)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, topic: str, message: Any) -> None:
        """Handle incoming messages on subscribed topics."""
        if isinstance(message, TaskRequest):
            self.identity.status = AgentStatus.BUSY
            start_time = time.monotonic()

            try:
                # Check timeout
                if message.timeout_ms:
                    result = await asyncio.wait_for(
                        self.execute_task(message),
                        timeout=message.timeout_ms / 1000,
                    )
                else:
                    result = await self.execute_task(message)

                result.duration_ms = (time.monotonic() - start_time) * 1000
                result.task_id = message.task_id

            except asyncio.TimeoutError:
                result = TaskResult(
                    task_id=message.task_id,
                    status=TaskStatus.FAILED,
                    error=f"Task timed out after {message.timeout_ms}ms",
                )
            except Exception as e:
                self.logger.error("task_execution_error", error=str(e), exc_info=True)
                result = TaskResult(
                    task_id=message.task_id,
                    status=TaskStatus.FAILED,
                    error=str(e),
                )

            self.identity.status = AgentStatus.READY

            # Publish result back
            await self.message_bus.publish(
                f"{topic}.result.{message.task_id}",
                result,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_task_result(
        self,
        task_id: str,
        success: bool,
        output: Any = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskResult:
        """Build a TaskResult with standard fields."""
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.SUCCESS if success else TaskStatus.FAILED,
            output=output,
            error=error,
            metadata=metadata or {},
        )

    async def health_check(self) -> dict[str, Any]:
        """Check agent health."""
        return {
            "agent_id": self.identity.agent_id,
            "name": self.identity.name,
            "status": self.identity.status.value,
            "healthy": self.identity.status != AgentStatus.ERROR,
        }
