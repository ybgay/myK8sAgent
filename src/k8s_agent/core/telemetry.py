"""Telemetry and observability for the k8sAgent multi-agent system.

Provides:
- Unified event schema for all agent lifecycle, task, tool-call, and LLM events
- TelemetryCollector singleton with async event buffering
- Pluggable backends: log, file (JSONL), WebSocket, AgentSight
- Trace context propagation (trace_id / span_id / parent_span_id)
- Lightweight rule engine for anomaly detection
- AgentSight-compatible event format for Linux production monitoring

Usage:
    from k8s_agent.core.telemetry import telemetry, TelemetryEvent

    await telemetry.emit(TelemetryEvent(
        event_type="agent_lifecycle",
        sub_type="agent_started",
        agent_id="agent-abc123",
        agent_type="k8s",
        payload={"status": "ready"},
    ))
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


# ============================================================================
# Event Schema
# ============================================================================


class EventType(str, Enum):
    """Top-level event categories."""
    AGENT_LIFECYCLE = "agent_lifecycle"
    TASK_LIFECYCLE = "task_lifecycle"
    TOOL_CALL = "tool_call"
    MESSAGE_BUS = "message_bus"
    LLM_CALL = "llm_call"
    WORKFLOW = "workflow"
    ALERT = "alert"


class EventSubType(str, Enum):
    """Sub-types for each event category."""
    # Agent lifecycle
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"
    AGENT_ERROR = "agent_error"
    AGENT_HEARTBEAT = "agent_heartbeat"
    AGENT_CREATED = "agent_created"

    # Task lifecycle
    TASK_CREATED = "task_created"
    TASK_DISPATCHED = "task_dispatched"
    TASK_RUNNING = "task_running"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_TIMEOUT = "task_timeout"
    TASK_CANCELLED = "task_cancelled"

    # Tool call
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_RESULT = "tool_call_result"

    # Message bus
    MESSAGE_PUBLISHED = "message_published"
    MESSAGE_RECEIVED = "message_received"
    REQUEST_SENT = "request_sent"
    RESPONSE_RECEIVED = "response_received"

    # LLM call
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    LLM_STREAM_DELTA = "llm_stream_delta"
    LLM_TOKEN_USAGE = "llm_token_usage"

    # Workflow
    WORKFLOW_START = "workflow_start"
    WORKFLOW_END = "workflow_end"
    DECOMPOSITION_RESULT = "decomposition_result"
    SYNTHESIS_RESULT = "synthesis_result"


@dataclass
class TelemetryEvent:
    """A single telemetry event with trace context.

    All events carry trace_id/span_id/parent_span_id for end-to-end
    tracing across multiple agents and message bus hops.
    """

    event_type: str  # EventType value
    sub_type: str  # EventSubType value

    # Identity
    agent_id: str = ""
    agent_type: str = ""  # "master" | "k8s" | "skill"

    # Trace context (propagated across agents)
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""

    # Timing
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0

    # Payload
    payload: dict[str, Any] = field(default_factory=dict)

    # Correlation
    task_id: str = ""
    tool_call_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a flat dict for JSON output."""
        return {
            "event_id": f"evt-{uuid.uuid4().hex[:12]}",
            "event_type": self.event_type,
            "sub_type": self.sub_type,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "timestamp": self.timestamp,
            "timestamp_iso": _iso_time(self.timestamp),
            "duration_ms": self.duration_ms,
            "task_id": self.task_id,
            "tool_call_id": self.tool_call_id,
            "payload": self.payload,
        }

    @classmethod
    def new_span(cls, parent: TelemetryEvent | None = None) -> dict[str, str]:
        """Create trace context fields for a new span.

        Args:
            parent: Optional parent event to inherit trace_id from.

        Returns:
            Dict with trace_id, span_id, parent_span_id.
        """
        span_id = f"span-{uuid.uuid4().hex[:12]}"
        if parent:
            return {
                "trace_id": parent.trace_id,
                "span_id": span_id,
                "parent_span_id": parent.span_id,
            }
        return {
            "trace_id": f"trace-{uuid.uuid4().hex[:16]}",
            "span_id": span_id,
            "parent_span_id": "",
        }


def _iso_time(ts: float) -> str:
    """Convert a unix timestamp to ISO 8601 string."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ============================================================================
# Alert / Anomaly Detection
# ============================================================================


@dataclass
class AlertRule:
    """A rule that fires an alert when its condition is met."""

    name: str
    description: str
    severity: str  # "info" | "warning" | "error"
    condition: str  # human-readable condition description
    cooldown_seconds: float = 30.0  # suppress repeat alerts within this window

    # Internal tracking
    _last_fired: float = 0.0
    _fire_count: int = 0

    def should_fire(self) -> bool:
        """Check cooldown — returns True if enough time has passed."""
        return (time.time() - self._last_fired) > self.cooldown_seconds

    def mark_fired(self) -> None:
        """Record that this rule has fired."""
        self._last_fired = time.time()
        self._fire_count += 1


class RuleEngine:
    """Lightweight rule engine for detecting agent anomalies.

    Tracks state across events and fires alerts when conditions are met.
    Alerts are themselves TelemetryEvents (event_type="alert").
    """

    def __init__(self) -> None:
        self._rules: dict[str, AlertRule] = {
            "reasoning_loop": AlertRule(
                name="reasoning_loop",
                description="Agent is stuck in a reasoning loop — repeated tool calls without progress",
                severity="warning",
                condition="Same agent makes 5+ tool calls without returning a final response",
            ),
            "task_timeout": AlertRule(
                name="task_timeout",
                description="Task exceeded its configured timeout",
                severity="error",
                condition="Task duration > timeout_ms",
            ),
            "tool_failure_rate": AlertRule(
                name="tool_failure_rate",
                description="High tool call failure rate in recent window",
                severity="warning",
                condition=">30% tool calls failed in last 5 minutes",
            ),
            "llm_error": AlertRule(
                name="llm_error",
                description="LLM provider returned errors",
                severity="error",
                condition="3+ consecutive LLM errors or empty responses",
            ),
            "agent_heartbeat_lost": AlertRule(
                name="agent_heartbeat_lost",
                description="Agent has stopped sending heartbeats",
                severity="error",
                condition="No heartbeat received for >30 seconds",
            ),
            "message_backlog": AlertRule(
                name="message_backlog",
                description="MessageBus has unhandled messages piling up",
                severity="warning",
                condition="Pending messages exceed threshold",
            ),
        }

        # State for rule evaluation
        self._consecutive_tool_calls: dict[str, int] = {}  # agent_id -> count
        self._consecutive_llm_errors: int = 0
        self._tool_results_window: list[dict[str, Any]] = []  # last N tool results
        self._last_heartbeat: dict[str, float] = {}  # agent_id -> timestamp
        self._pending_message_count: int = 0

        # Callback for emitted alerts (set by TelemetryCollector)
        self._alert_callback: Any = None

    def set_alert_callback(self, callback: Any) -> None:
        """Set the callback to invoke when an alert fires."""
        self._alert_callback = callback

    async def evaluate(self, event: TelemetryEvent) -> list[TelemetryEvent]:
        """Evaluate all rules against a new event.

        Args:
            event: The latest telemetry event.

        Returns:
            List of alert events (empty if no rules triggered).
        """
        alerts: list[TelemetryEvent] = []

        # Rule 1: Reasoning loop detection
        alert = await self._check_reasoning_loop(event)
        if alert:
            alerts.append(alert)

        # Rule 2: Task timeout
        alert = self._check_task_timeout(event)
        if alert:
            alerts.append(alert)

        # Rule 3: Tool failure rate
        alert = self._check_tool_failure_rate(event)
        if alert:
            alerts.append(alert)

        # Rule 4: LLM errors
        alert = self._check_llm_errors(event)
        if alert:
            alerts.append(alert)

        # Rule 5: Heartbeat lost
        alert = self._check_heartbeat_lost(event)
        if alert:
            alerts.append(alert)

        return alerts

    async def _check_reasoning_loop(self, event: TelemetryEvent) -> TelemetryEvent | None:
        """Track consecutive tool calls per agent without a final text response."""
        if event.sub_type == EventSubType.TOOL_CALL_START:
            agent_key = event.agent_id or event.agent_type
            self._consecutive_tool_calls[agent_key] = (
                self._consecutive_tool_calls.get(agent_key, 0) + 1
            )
            if self._consecutive_tool_calls.get(agent_key, 0) >= 5:
                rule = self._rules["reasoning_loop"]
                if rule.should_fire():
                    rule.mark_fired()
                    return _make_alert(event, rule)
        elif event.sub_type == EventSubType.WORKFLOW_END:
            # Reset on workflow end
            self._consecutive_tool_calls.clear()
        return None

    def _check_task_timeout(self, event: TelemetryEvent) -> TelemetryEvent | None:
        """Check if a task has timed out."""
        if event.sub_type in (EventSubType.TASK_TIMEOUT, EventSubType.TASK_FAILED):
            if "timeout" in str(event.payload.get("error", "")).lower():
                rule = self._rules["task_timeout"]
                if rule.should_fire():
                    rule.mark_fired()
                    return _make_alert(event, rule)
        return None

    def _check_tool_failure_rate(self, event: TelemetryEvent) -> TelemetryEvent | None:
        """Track tool call failure rate in a sliding window."""
        if event.sub_type == EventSubType.TOOL_CALL_RESULT:
            self._tool_results_window.append({
                "ts": event.timestamp,
                "is_error": event.payload.get("is_error", False),
            })
            # Keep only last 5 minutes
            cutoff = event.timestamp - 300
            self._tool_results_window = [
                r for r in self._tool_results_window if r["ts"] > cutoff
            ]
            # Check rate if we have enough samples
            total = len(self._tool_results_window)
            if total >= 5:
                failures = sum(1 for r in self._tool_results_window if r["is_error"])
                if failures / total > 0.3:
                    rule = self._rules["tool_failure_rate"]
                    if rule.should_fire():
                        rule.mark_fired()
                        alert = _make_alert(event, rule)
                        alert.payload["failure_rate"] = f"{failures}/{total} ({failures/total:.0%})"
                        return alert
        return None

    def _check_llm_errors(self, event: TelemetryEvent) -> TelemetryEvent | None:
        """Track consecutive LLM errors."""
        if event.sub_type == EventSubType.LLM_RESPONSE:
            is_error = event.payload.get("is_error", False)
            is_empty = not event.payload.get("content") and not event.payload.get("tool_calls")
            if is_error or is_empty:
                self._consecutive_llm_errors += 1
                if self._consecutive_llm_errors >= 3:
                    rule = self._rules["llm_error"]
                    if rule.should_fire():
                        rule.mark_fired()
                        return _make_alert(event, rule)
            else:
                self._consecutive_llm_errors = 0
        return None

    def _check_heartbeat_lost(self, event: TelemetryEvent) -> TelemetryEvent | None:
        """Check for missing heartbeats."""
        if event.sub_type == EventSubType.AGENT_HEARTBEAT:
            self._last_heartbeat[event.agent_id] = event.timestamp
        elif event.sub_type == EventSubType.MESSAGE_PUBLISHED:
            # Opportunistic check — if any agent hasn't sent heartbeat in 30s
            now = event.timestamp
            for agent_id, last_ts in list(self._last_heartbeat.items()):
                if now - last_ts > 30:
                    rule = self._rules["agent_heartbeat_lost"]
                    if rule.should_fire():
                        rule.mark_fired()
                        alert = _make_alert(event, rule)
                        alert.payload["lost_agent_id"] = agent_id
                        alert.payload["seconds_since_heartbeat"] = round(now - last_ts, 1)
                        return alert
        return None


def _make_alert(event: TelemetryEvent, rule: AlertRule) -> TelemetryEvent:
    """Create an alert event from a rule and triggering event."""
    return TelemetryEvent(
        event_type=EventType.ALERT,
        sub_type=rule.name,
        agent_id=event.agent_id,
        agent_type=event.agent_type,
        trace_id=event.trace_id,
        span_id=event.span_id,
        payload={
            "rule": rule.name,
            "description": rule.description,
            "severity": rule.severity,
            "condition": rule.condition,
            "fire_count": rule._fire_count,
            "trigger_event_type": event.event_type,
            "trigger_sub_type": event.sub_type,
        },
    )


# ============================================================================
# Backends
# ============================================================================


class TelemetryBackend(ABC):
    """Abstract backend for persisting / forwarding telemetry events."""

    @abstractmethod
    async def emit(self, event: TelemetryEvent) -> None:
        """Emit a single telemetry event."""

    @abstractmethod
    async def flush(self) -> None:
        """Flush any buffered events."""

    async def close(self) -> None:
        """Close the backend and release resources."""
        await self.flush()


class LogBackend(TelemetryBackend):
    """Write events to structlog."""

    def __init__(self, logger_name: str = "telemetry") -> None:
        self._logger = get_logger(logger_name)

    async def emit(self, event: TelemetryEvent) -> None:
        """Emit event as structured log."""
        data = event.to_dict()
        level = "warning" if event.event_type == EventType.ALERT else "info"
        log_method = getattr(self._logger, level, self._logger.info)
        log_method(
            event.event_type,
            sub_type=event.sub_type,
            agent_id=event.agent_id,
            trace_id=event.trace_id,
            duration_ms=event.duration_ms,
            payload=event.payload,
        )

    async def flush(self) -> None:
        pass  # structlog is unbuffered


class FileBackend(TelemetryBackend):
    """Write events to a JSON Lines file (AgentSight-compatible format).

    Each line is a JSON object — easy to parse with jq, import into
    AgentSight, or stream to log aggregation systems.
    """

    def __init__(self, file_path: str | Path, buffer_size: int = 50) -> None:
        self._path = Path(file_path).expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._buffer: list[str] = []
        self._buffer_size = buffer_size
        self._lock = asyncio.Lock()

    async def emit(self, event: TelemetryEvent) -> None:
        """Buffer event and flush when buffer is full."""
        async with self._lock:
            self._buffer.append(json.dumps(event.to_dict(), default=str, ensure_ascii=False))
            if len(self._buffer) >= self._buffer_size:
                await self._flush_locked()

    async def flush(self) -> None:
        """Flush remaining buffered events."""
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        """Write buffer to file. Must hold self._lock."""
        if not self._buffer:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                for line in self._buffer:
                    f.write(line + "\n")
            self._buffer.clear()
        except OSError as e:
            logger.error("telemetry_file_write_error", path=str(self._path), error=str(e))


class WebSocketBackend(TelemetryBackend):
    """Push events to connected WebSocket clients.

    The TelemetryCollector sets the _broadcast callback after construction
    because the WebSocket connections are managed by the FastAPI layer.
    """

    def __init__(self) -> None:
        self._broadcast: Any = None  # async callable(event_dict) -> None
        self._connected: bool = False

    def set_broadcast(self, broadcast_func: Any) -> None:
        """Set the broadcast function from the WebSocket layer."""
        self._broadcast = broadcast_func
        self._connected = broadcast_func is not None

    async def emit(self, event: TelemetryEvent) -> None:
        """Push event to WebSocket clients."""
        if self._broadcast:
            try:
                await self._broadcast(event.to_dict())
            except Exception as e:
                logger.debug("telemetry_ws_emit_error", error=str(e))

    async def flush(self) -> None:
        pass


class AgentSightBackend(TelemetryBackend):
    """Forward events to an AgentSight instance (Linux / eBPF monitoring).

    AgentSight runs a dashboard at http://127.0.0.1:7395 by default.
    This backend POSTs structured events to AgentSight's HTTP API
    so they appear alongside eBPF-captured events.

    On non-Linux platforms this backend is a no-op.
    """

    def __init__(self, agent_sight_url: str = "http://127.0.0.1:7395") -> None:
        self._url = agent_sight_url.rstrip("/")
        self._available = False
        self._client: Any = None
        self._buffer: list[dict[str, Any]] = []
        self._buffer_size = 20

    async def _ensure_client(self) -> bool:
        """Lazily create HTTP client. Returns True if available."""
        if self._client is not None:
            return self._available
        try:
            import httpx
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
            # Check if AgentSight is reachable
            resp = await self._client.get(f"{self._url}/health")
            self._available = resp.status_code == 200
            if self._available:
                logger.info("agentsight_connected", url=self._url)
            else:
                logger.debug("agentsight_not_available", url=self._url, status=resp.status_code)
        except Exception:
            self._available = False
            logger.debug("agentsight_not_available", url=self._url)
        return self._available

    async def emit(self, event: TelemetryEvent) -> None:
        """Forward event to AgentSight."""
        if not await self._ensure_client():
            return
        self._buffer.append(event.to_dict())
        if len(self._buffer) >= self._buffer_size:
            await self.flush()

    async def flush(self) -> None:
        """Send buffered events to AgentSight."""
        if not self._buffer or not self._client:
            return
        try:
            await self._client.post(
                f"{self._url}/api/events",
                json={"events": self._buffer},
            )
            self._buffer.clear()
        except Exception as e:
            logger.debug("agentsight_emit_error", error=str(e))

    async def close(self) -> None:
        """Close HTTP client."""
        await self.flush()
        if self._client:
            await self._client.aclose()
            self._client = None
            self._available = False


# ============================================================================
# Telemetry Collector (Singleton)
# ============================================================================


@dataclass
class TelemetryCollector:
    """Central telemetry collector — singleton per process.

    Buffers events in an async queue and dispatches to configured backends.
    Also runs the rule engine for anomaly detection.

    Usage:
        from k8s_agent.core.telemetry import telemetry

        await telemetry.init(backends=["log", "file", "websocket"])
        await telemetry.emit(TelemetryEvent(...))
    """

    backends: list[TelemetryBackend] = field(default_factory=list)
    rule_engine: RuleEngine = field(default_factory=RuleEngine)
    _queue: asyncio.Queue[TelemetryEvent | None] = field(default_factory=asyncio.Queue)
    _worker_task: asyncio.Task | None = None
    _started: bool = False
    _event_count: int = 0
    _alert_history: list[dict[str, Any]] = field(default_factory=list)

    # Trace storage for API queries
    _traces: dict[str, dict[str, Any]] = field(default_factory=dict)
    _recent_events: list[dict[str, Any]] = field(default_factory=list)  # ring buffer
    _max_recent_events: int = 500

    async def init(
        self,
        backends: list[str] | None = None,
        file_path: str | None = None,
        agent_sight_url: str = "http://127.0.0.1:7395",
    ) -> None:
        """Initialize the telemetry collector with the specified backends.

        Args:
            backends: List of backend names: "log", "file", "websocket", "agentsight".
            file_path: Path for the file backend.
            agent_sight_url: URL of the AgentSight instance.
        """
        if self._started:
            return

        backend_names = backends or ["log"]

        for name in backend_names:
            if name == "log":
                self.backends.append(LogBackend())
            elif name == "file":
                path = file_path or os.path.expanduser("~/.k8s-agent/traces/events.jsonl")
                self.backends.append(FileBackend(path))
            elif name == "websocket":
                self.backends.append(WebSocketBackend())
            elif name == "agentsight":
                self.backends.append(AgentSightBackend(agent_sight_url))

        # Wire rule engine alert callback
        self.rule_engine.set_alert_callback(self._on_alert)

        self._worker_task = asyncio.create_task(self._worker_loop())
        self._started = True
        logger.info(
            "telemetry_initialized",
            backends=backend_names,
            backend_count=len(self.backends),
        )

    async def emit(self, event: TelemetryEvent) -> None:
        """Queue an event for async processing.

        This is the primary API — all agent code calls this.
        """
        if not self._started:
            return
        await self._queue.put(event)

    async def emit_batch(self, events: list[TelemetryEvent]) -> None:
        """Queue multiple events at once."""
        for event in events:
            await self._queue.put(event)

    async def flush(self) -> None:
        """Wait for all queued events to be processed."""
        # Send a sentinel and wait for it to be processed
        sentinel = TelemetryEvent(
            event_type="__flush__",
            sub_type="__flush__",
        )
        await self._queue.put(sentinel)
        # Give worker a chance to process
        await asyncio.sleep(0.1)

    async def shutdown(self) -> None:
        """Gracefully shut down the telemetry system."""
        if not self._started:
            return
        self._started = False

        # Signal worker to stop
        await self._queue.put(None)

        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        # Close all backends
        for backend in self.backends:
            try:
                await backend.close()
            except Exception as e:
                logger.error("telemetry_backend_close_error", error=str(e))

        logger.info("telemetry_shutdown", total_events=self._event_count)

    async def _worker_loop(self) -> None:
        """Main worker — dequeues events and dispatches to backends."""
        while True:
            try:
                event = await self._queue.get()
            except RuntimeError:
                break  # Queue closed

            if event is None:
                break  # Shutdown signal

            # Skip sentinel
            if event.event_type == "__flush__":
                continue

            self._event_count += 1

            # Store in recent events ring buffer
            event_dict = event.to_dict()
            self._recent_events.append(event_dict)
            if len(self._recent_events) > self._max_recent_events:
                self._recent_events = self._recent_events[-self._max_recent_events:]

            # Update trace storage
            if event.trace_id:
                if event.trace_id not in self._traces:
                    self._traces[event.trace_id] = {
                        "trace_id": event.trace_id,
                        "start_time": event.timestamp,
                        "events": [],
                    }
                self._traces[event.trace_id]["events"].append(event_dict)
                self._traces[event.trace_id]["end_time"] = event.timestamp

            # Dispatch to backends
            for backend in self.backends:
                try:
                    await backend.emit(event)
                except Exception as e:
                    logger.debug("telemetry_backend_emit_error", backend=type(backend).__name__, error=str(e))

            # Run rule engine
            try:
                alerts = await self.rule_engine.evaluate(event)
                for alert in alerts:
                    # Also emit alert events through backends
                    for backend in self.backends:
                        try:
                            await backend.emit(alert)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("rule_engine_error", error=str(e))

            self._queue.task_done()

    async def _on_alert(self, alert: TelemetryEvent) -> None:
        """Called by RuleEngine when an alert fires."""
        self._alert_history.append(alert.to_dict())
        # Keep only last 100 alerts
        if len(self._alert_history) > 100:
            self._alert_history = self._alert_history[-100:]

    # ------------------------------------------------------------------
    # API helpers for the Web UI
    # ------------------------------------------------------------------

    def get_recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent events for the monitor API."""
        return self._recent_events[-limit:]

    def get_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent trace summaries."""
        traces = sorted(
            self._traces.values(),
            key=lambda t: t.get("start_time", 0),
            reverse=True,
        )
        return traces[:limit]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Get a single trace by ID."""
        return self._traces.get(trace_id)

    def get_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent alerts."""
        return self._alert_history[-limit:]

    def get_stats(self) -> dict[str, Any]:
        """Get aggregate statistics."""
        # Count by event type
        type_counts: dict[str, int] = {}
        for evt in self._recent_events:
            t = evt.get("event_type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1

        # Calculate average task duration
        task_durations = [
            e.get("duration_ms", 0)
            for e in self._recent_events
            if e.get("event_type") == "task_lifecycle" and e.get("duration_ms", 0) > 0
        ]
        avg_task_duration = (
            sum(task_durations) / len(task_durations) if task_durations else 0
        )

        # Count active agents
        active_agents: set[str] = set()
        for e in self._recent_events[-200:]:
            if e.get("agent_id") and e.get("sub_type") == "agent_heartbeat":
                active_agents.add(e["agent_id"])

        return {
            "total_events": self._event_count,
            "recent_event_count": len(self._recent_events),
            "trace_count": len(self._traces),
            "alert_count": len(self._alert_history),
            "active_agents": len(active_agents),
            "event_type_distribution": type_counts,
            "avg_task_duration_ms": round(avg_task_duration, 1),
            "backends": [type(b).__name__ for b in self.backends],
            "started": self._started,
        }

    @property
    def ws_backend(self) -> WebSocketBackend | None:
        """Get the WebSocket backend if configured."""
        for b in self.backends:
            if isinstance(b, WebSocketBackend):
                return b
        return None


# ============================================================================
# Global singleton
# ============================================================================

# The single process-wide telemetry collector instance.
# Call telemetry.init() once at startup, then use telemetry.emit() everywhere.
telemetry = TelemetryCollector()
