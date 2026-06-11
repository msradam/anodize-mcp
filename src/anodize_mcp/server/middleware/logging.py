"""Request logging middleware, matching ``fastmcp.server.middleware.logging``.

Logs each message with optional payloads. FastMCP serializes payloads with
``pydantic_core``; this uses the stdlib ``json`` module with a string fallback,
keeping it dependency free.
"""

from __future__ import annotations

import json
import logging
import time
from logging import Logger
from typing import Any, Callable, Optional, Union

from .middleware import CallNext, Middleware, MiddlewareContext

_Scalar = Union[str, int, float]


def default_serializer(data: Any) -> str:
    return json.dumps(data, default=str)


class BaseLoggingMiddleware(Middleware):
    logger: Logger
    log_level: int
    include_payloads: bool
    include_payload_length: bool
    estimate_payload_tokens: bool
    max_payload_length: Optional[int]
    methods: Optional[list[str]]
    structured_logging: bool
    payload_serializer: Optional[Callable[[Any], str]]

    def _serialize_payload(self, context: MiddlewareContext) -> str:
        if not self.payload_serializer:
            return default_serializer(context.message)
        try:
            return self.payload_serializer(context.message)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"Failed to serialize payload due to {exc}: "
                f"{context.type} {context.method} {context.source}."
            )
            return default_serializer(context.message)

    def _format_message(self, message: dict[str, _Scalar]) -> str:
        if self.structured_logging:
            return json.dumps(message)
        return " ".join(f"{k}={v}" for k, v in message.items())

    def _create_before_message(self, context: MiddlewareContext) -> dict[str, _Scalar]:
        message: dict[str, _Scalar] = {
            "event": context.type + "_start",
            "method": context.method or "unknown",
            "source": context.source,
        }
        if self.include_payloads or self.include_payload_length or self.estimate_payload_tokens:
            payload = self._serialize_payload(context)
            if self.include_payload_length or self.estimate_payload_tokens:
                payload_length = len(payload)
                if self.estimate_payload_tokens:
                    message["payload_tokens"] = payload_length // 4
                if self.include_payload_length:
                    message["payload_length"] = payload_length
            if self.max_payload_length and len(payload) > self.max_payload_length:
                payload = payload[: self.max_payload_length] + "..."
            if self.include_payloads:
                message["payload"] = payload
                message["payload_type"] = type(context.message).__name__
        return message

    def _create_error_message(
        self, context: MiddlewareContext, start_time: float, error: Exception
    ) -> dict[str, _Scalar]:
        return {
            "event": context.type + "_error",
            "method": context.method or "unknown",
            "source": context.source,
            "duration_ms": _get_duration_ms(start_time),
            "error": str(error),
        }

    def _create_after_message(
        self, context: MiddlewareContext, start_time: float
    ) -> dict[str, _Scalar]:
        return {
            "event": context.type + "_success",
            "method": context.method or "unknown",
            "source": context.source,
            "duration_ms": _get_duration_ms(start_time),
        }

    def _log_message(self, message: dict[str, _Scalar], log_level: Optional[int] = None) -> None:
        self.logger.log(log_level or self.log_level, self._format_message(message))

    async def on_message(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        if self.methods and context.method not in self.methods:
            return await call_next(context)

        self._log_message(self._create_before_message(context))
        start_time = time.perf_counter()
        try:
            result = await call_next(context)
            self._log_message(self._create_after_message(context, start_time))
            return result
        except Exception as exc:
            self._log_message(self._create_error_message(context, start_time, exc), logging.ERROR)
            raise


class LoggingMiddleware(BaseLoggingMiddleware):
    """Human-readable key=value request logging."""

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        log_level: int = logging.INFO,
        include_payloads: bool = False,
        include_payload_length: bool = False,
        estimate_payload_tokens: bool = False,
        max_payload_length: int = 1000,
        methods: Optional[list[str]] = None,
        payload_serializer: Optional[Callable[[Any], str]] = None,
    ):
        self.logger = logger or logging.getLogger("fastmcp.middleware.logging")
        self.log_level = log_level
        self.include_payloads = include_payloads
        self.include_payload_length = include_payload_length
        self.estimate_payload_tokens = estimate_payload_tokens
        self.max_payload_length = max_payload_length
        self.methods = methods
        self.payload_serializer = payload_serializer
        self.structured_logging = False


class StructuredLoggingMiddleware(BaseLoggingMiddleware):
    """JSON request logging for log aggregation."""

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        log_level: int = logging.INFO,
        include_payloads: bool = False,
        include_payload_length: bool = False,
        estimate_payload_tokens: bool = False,
        methods: Optional[list[str]] = None,
        payload_serializer: Optional[Callable[[Any], str]] = None,
    ):
        self.logger = logger or logging.getLogger("fastmcp.middleware.structured_logging")
        self.log_level = log_level
        self.include_payloads = include_payloads
        self.include_payload_length = include_payload_length
        self.estimate_payload_tokens = estimate_payload_tokens
        self.methods = methods
        self.payload_serializer = payload_serializer
        self.max_payload_length = None
        self.structured_logging = True


def _get_duration_ms(start_time: float, /) -> float:
    return round((time.perf_counter() - start_time) * 1000, 2)
