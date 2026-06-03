"""MCP Server middleware — error handling, access control, audit logging."""

from __future__ import annotations

import json
import time
from functools import wraps
from typing import Any, Callable

from kubernetes.client.exceptions import ApiException

from k8s_agent.shared.config import MCPServerConfig, get_settings
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


def create_access_control(config: MCPServerConfig | None = None) -> Callable[[str], bool]:
    """Create an access control function that checks if a tool is allowed.

    Returns a function that takes a tool name and returns whether it's permitted.
    """
    cfg = config or get_settings().mcp_server

    if not cfg.access_control_enabled:
        return lambda _: True

    def is_allowed(tool_name: str) -> bool:
        # Check deny list first (takes precedence)
        for denied in cfg.denied_tools:
            if _match_pattern(tool_name, denied):
                logger.warning("tool_blocked_by_deny_list", tool=tool_name)
                return False

        # Check allow list
        if "*" in cfg.allowed_tools:
            return True
        for allowed in cfg.allowed_tools:
            if _match_pattern(tool_name, allowed):
                return True

        logger.warning("tool_not_in_allow_list", tool=tool_name)
        return False

    return is_allowed


def _match_pattern(name: str, pattern: str) -> bool:
    """Match a tool name against a pattern (supports * wildcard)."""
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


def wrap_tool_with_error_handling(
    handler: Callable, tool_name: str
) -> Callable:
    """Wrap a tool handler with standardized error handling and logging.

    Args:
        handler: The async tool handler function.
        tool_name: The tool's registered name.

    Returns:
        Wrapped async handler.
    """

    @wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        start_time = time.monotonic()
        logger.info("tool_call_start", tool=tool_name, args=str(kwargs)[:200])

        try:
            result = await handler(*args, **kwargs)
            elapsed = (time.monotonic() - start_time) * 1000
            logger.info("tool_call_success", tool=tool_name, elapsed_ms=round(elapsed, 1))
            return result
        except ApiException as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.error(
                "tool_call_k8s_error",
                tool=tool_name,
                status=e.status,
                reason=e.reason,
                elapsed_ms=round(elapsed, 1),
            )
            error_body = {}
            try:
                error_body = json.loads(e.body) if e.body else {}
            except (json.JSONDecodeError, AttributeError):
                pass
            return json.dumps({
                "error": True,
                "tool": tool_name,
                "status": e.status,
                "reason": e.reason,
                "message": error_body.get("message", str(e)),
            }, indent=2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.error(
                "tool_call_unexpected_error",
                tool=tool_name,
                error=str(e),
                elapsed_ms=round(elapsed, 1),
                exc_info=True,
            )
            return json.dumps({
                "error": True,
                "tool": tool_name,
                "message": str(e),
                "type": type(e).__name__,
            }, indent=2)

    return wrapper
