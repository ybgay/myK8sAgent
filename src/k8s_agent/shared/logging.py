"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog import BoundLogger
from structlog.typing import Processor


def setup_logging(
    level: str = "info",
    output_format: str = "pretty",
    log_file: str | None = None,
) -> None:
    """Configure structured logging for the entire application.

    Args:
        level: Log level (trace, debug, info, warn, error).
        output_format: 'pretty' for colored console or 'json' for machine-readable.
        log_file: Optional file path for log output.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Shared processors
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if output_format == "json":
        processors: list[Processor] = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.rich_traceback,
            )
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(
            file=sys.stderr if log_file is None else open(log_file, "a", encoding="utf-8")
        ),
        cache_logger_on_first_use=True,
    )

    # Also configure standard library logging to use structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=log_level,
    )


def get_logger(name: str | None = None, **context: Any) -> structlog.BoundLogger:
    """Get a structured logger with optional bound context.

    Args:
        name: Logger name (typically __name__).
        **context: Key-value pairs bound to all log messages from this logger.

    Returns:
        A structlog BoundLogger.
    """
    logger = structlog.get_logger(name or "k8s_agent")
    if context:
        logger = logger.bind(**context)
    return logger
