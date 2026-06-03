"""Typed message bus for inter-agent communication.

Supports:
- Publish/subscribe with wildcard topic matching
- Request-response pattern via correlation IDs
- Observer pattern (monitoring without affecting agent behavior)
"""

from __future__ import annotations

import asyncio
import fnmatch
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from k8s_agent.shared.logging import get_logger
from k8s_agent.shared.types import TaskProgress, TaskRequest, TaskResult

logger = get_logger(__name__)

# Type for message handlers
Handler = Callable[..., Awaitable[Any]]
MessageCallback = Callable[[str, Any], Awaitable[None]]


@dataclass
class MessageBus:
    """Typed publish/subscribe message bus with wildcard topic matching.

    Topics use dot-notation: "task.k8s.create_pod", "agent.k8s.heartbeat", etc.
    Wildcards: "*" matches any single segment, "**" matches any remaining segments.

    Usage:
        bus = MessageBus()

        @bus.on("task.k8s.*")
        async def handle_k8s_task(topic, msg):
            print(f"K8s task: {msg}")

        await bus.publish("task.k8s.create_pod", request)
    """

    _subscriptions: dict[str, list[MessageCallback]] = field(default_factory=dict)
    _pending_requests: dict[str, asyncio.Future] = field(default_factory=dict)
    _observers: list[MessageCallback] = field(default_factory=list)

    def on(self, pattern: str) -> Callable[[MessageCallback], MessageCallback]:
        """Decorator to subscribe a handler to a topic pattern.

        Args:
            pattern: Topic pattern with optional wildcards (* and **).

        Returns:
            Decorator function.
        """
        def decorator(handler: MessageCallback) -> MessageCallback:
            self.subscribe(pattern, handler)
            return handler
        return decorator

    def subscribe(self, pattern: str, handler: MessageCallback) -> None:
        """Subscribe a handler to a topic pattern.

        Args:
            pattern: Topic pattern (e.g. "task.*.completed").
            handler: Async callback receiving (topic, message).
        """
        if pattern not in self._subscriptions:
            self._subscriptions[pattern] = []
        self._subscriptions[pattern].append(handler)
        logger.debug("message_bus_subscribe", pattern=pattern)

    def unsubscribe(self, pattern: str, handler: MessageCallback) -> None:
        """Remove a subscription."""
        if pattern in self._subscriptions:
            self._subscriptions[pattern] = [
                h for h in self._subscriptions[pattern] if h != handler
            ]

    def observe(self, handler: MessageCallback) -> None:
        """Add an observer that receives ALL messages (for logging, monitoring).

        Observers do not affect message processing.
        """
        self._observers.append(handler)

    async def publish(self, topic: str, message: Any) -> None:
        """Publish a message to all matching subscribers.

        Args:
            topic: The message topic.
            message: The message payload.
        """
        logger.debug("message_bus_publish", topic=topic)

        # Notify observers
        for observer in self._observers:
            try:
                await observer(topic, message)
            except Exception as e:
                logger.error("observer_error", topic=topic, error=str(e))

        # Find matching subscriptions
        matching_handlers = []
        for pattern, handlers in self._subscriptions.items():
            if _topic_matches(topic, pattern):
                matching_handlers.extend(handlers)

        # Broadcast to all matching handlers concurrently
        if matching_handlers:
            tasks = []
            for handler in matching_handlers:
                tasks.append(asyncio.create_task(handler(topic, message)))
            # Don't wait for handlers — fire and forget
            # (use request-response pattern if you need results)

    async def request(
        self, topic: str, message: Any, timeout: float = 30.0
    ) -> Any:
        """Publish a request and wait for a response.

        Uses correlation ID to match request to response.

        Args:
            topic: The request topic.
            message: The request payload (should have a task_id/correlation_id).
            timeout: Timeout in seconds.

        Returns:
            The response message.

        Raises:
            asyncio.TimeoutError: If no response within timeout.
        """
        # Use task_id from message if available, else generate one
        corr_id = getattr(message, "task_id", None) or f"req-{uuid.uuid4().hex[:8]}"
        response_topic = f"{topic}.response.{corr_id}"

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[corr_id] = future

        # Subscribe to response
        async def response_handler(_topic: str, msg: Any) -> None:
            if corr_id in self._pending_requests:
                fut = self._pending_requests.pop(corr_id, None)
                if fut and not fut.done():
                    fut.set_result(msg)

        self.subscribe(response_topic, response_handler)

        try:
            await self.publish(topic, message)
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(corr_id, None)
            raise
        finally:
            self.unsubscribe(response_topic, response_handler)

    def clear(self) -> None:
        """Clear all subscriptions and pending requests."""
        self._subscriptions.clear()
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()
        self._observers.clear()


def _topic_matches(topic: str, pattern: str) -> bool:
    """Check if a topic matches a pattern with wildcards.

    Args:
        topic: The actual topic (e.g. "task.k8s.create_pod").
        pattern: The pattern (e.g. "task.k8s.*" or "task.**").

    Returns:
        True if the topic matches the pattern.
    """
    return fnmatch.fnmatch(topic, pattern)
