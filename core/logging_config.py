"""
Structured logging for the pipeline.

Provides a get_logger() that returns a structlog-compatible logger with
consistent schema. Supports both console output and the WebSocket callback
pattern used by the UI.

Usage:
    from core.logging_config import get_logger
    log = get_logger("training")
    log.info("Starting epoch", epoch=1, lr=2e-4)

WebSocket bridge:
    from core.logging_config import get_logger, ws_callback_factory

    async def my_ws_log(text, level):
        await broadcast({"type": "log", "text": text, "level": level})

    log = get_logger("export", callback=ws_callback_factory(my_ws_log))
"""

import logging
import sys
from typing import Any, Callable, Optional, Protocol

import structlog


class LogCallback(Protocol):
    """Signature expected by the UI's WebSocket fan-out."""

    def __call__(self, msg: str, level: str = "info") -> Any: ...


def configure_logging(*, json_output: bool = False) -> None:
    """Configure structlog processors and stdlib integration.

    Call once at application startup (e.g. in app.py or pipeline.py __main__).
    Subsequent calls are idempotent.

    Args:
        json_output: If True, render as JSON lines (for production log
                     aggregation). Otherwise render coloured console output.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def get_logger(
    name: str,
    *,
    callback: Optional[LogCallback] = None,
) -> structlog.stdlib.BoundLogger:
    """Return a named structlog logger.

    Args:
        name: Logger name (typically the stage or module name).
        callback: Optional synchronous callback that mirrors every log
                  message to the UI's WebSocket fan-out. The callback
                  receives ``(msg, level)`` and can be sync or async-wrapped.

    Returns:
        A bound structlog logger.
    """
    log: structlog.stdlib.BoundLogger = structlog.get_logger(name)

    if callback is not None:
        log = log.bind(_ws_callback=callback)

    return log


def ws_callback_factory(
    async_log_fn: Callable[[str, str], Any],
) -> LogCallback:
    """Wrap an async WebSocket log function into a sync LogCallback.

    The returned callback is safe to pass as ``callback=`` to get_logger().
    It schedules the async broadcast on the running event loop without
    blocking the caller.

    Args:
        async_log_fn: Coroutine with signature ``async def(text, level)``.
    """
    import asyncio

    def _sync_bridge(msg: str, level: str = "info") -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(async_log_fn(msg, level))
        except RuntimeError:
            # No running loop — fall back to printing.
            print(f"[{level.upper():>7}] {msg}")

    return _sync_bridge
