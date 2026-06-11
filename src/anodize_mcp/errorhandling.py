"""Error-handling middleware, matching ``fastmcp.server.middleware.error_handling``.

Catches exceptions, logs them, tracks counts, and transforms non-MCP errors into
:class:`McpError`. Class names and constructor signatures mirror FastMCP.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any, Callable, Optional

from .exceptions import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    RESOURCE_NOT_FOUND,
    McpError,
    NotFoundError,
)
from .middleware import CallNext, Middleware, MiddlewareContext

# JSON-RPC server-defined error range; FastMCP uses these for the cases below.
NOT_FOUND = -32001
SERVER_ERROR = -32000


class ErrorHandlingMiddleware(Middleware):
    """Consistent error logging, counting, and transformation."""

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        include_traceback: bool = False,
        error_callback: Optional[Callable[[Exception, MiddlewareContext], None]] = None,
        transform_errors: bool = True,
    ):
        self.logger = logger or logging.getLogger("fastmcp.errors")
        self.include_traceback = include_traceback
        self.error_callback = error_callback
        self.transform_errors = transform_errors
        self.error_counts: dict[str, int] = {}

    def _log_error(self, error: Exception, context: MiddlewareContext) -> None:
        error_type = type(error).__name__
        method = context.method or "unknown"

        error_key = f"{error_type}:{method}"
        self.error_counts[error_key] = self.error_counts.get(error_key, 0) + 1

        base_message = f"Error in {method}: {error_type}: {error!s}"
        if self.include_traceback:
            self.logger.error(f"{base_message}\n{traceback.format_exc()}")
        else:
            self.logger.error(base_message)

        if self.error_callback:
            try:
                self.error_callback(error, context)
            except Exception as callback_error:  # noqa: BLE001
                self.logger.error(f"Error in error callback: {callback_error}")

    def _transform_error(self, error: Exception, context: MiddlewareContext) -> Exception:
        if isinstance(error, McpError):
            return error
        if not self.transform_errors:
            return error

        error_type = type(error.__cause__) if error.__cause__ else type(error)

        if error_type in (ValueError, TypeError):
            return McpError(f"Invalid params: {error!s}", code=INVALID_PARAMS)
        if error_type in (FileNotFoundError, KeyError, NotFoundError):
            method = context.method or ""
            if method.startswith("resources/"):
                return McpError(f"Resource not found: {error!s}", code=RESOURCE_NOT_FOUND)
            return McpError(f"Not found: {error!s}", code=NOT_FOUND)
        if error_type is PermissionError:
            return McpError(f"Permission denied: {error!s}", code=SERVER_ERROR)
        if error_type in (TimeoutError, asyncio.TimeoutError):
            return McpError(f"Request timeout: {error!s}", code=SERVER_ERROR)
        return McpError(f"Internal error: {error!s}", code=INTERNAL_ERROR)

    async def on_message(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        try:
            return await call_next(context)
        except Exception as error:
            self._log_error(error, context)
            raise self._transform_error(error, context) from error

    def get_error_stats(self) -> dict[str, int]:
        return self.error_counts.copy()


class RetryMiddleware(Middleware):
    """Retry failed requests with exponential backoff."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_multiplier: float = 2.0,
        retry_exceptions: tuple[type[Exception], ...] = (ConnectionError, TimeoutError),
        logger: Optional[logging.Logger] = None,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_multiplier = backoff_multiplier
        self.retry_exceptions = retry_exceptions
        self.logger = logger or logging.getLogger("fastmcp.retry")

    def _should_retry(self, error: Exception) -> bool:
        if isinstance(error, self.retry_exceptions):
            return True
        cause = error.__cause__
        return cause is not None and isinstance(cause, self.retry_exceptions)

    def _calculate_delay(self, attempt: int) -> float:
        return min(self.base_delay * (self.backoff_multiplier**attempt), self.max_delay)

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return await call_next(context)
            except Exception as error:
                last_error = error
                if attempt == self.max_retries or not self._should_retry(error):
                    break
                delay = self._calculate_delay(attempt)
                self.logger.warning(
                    f"Request {context.method} failed (attempt {attempt + 1}/"
                    f"{self.max_retries + 1}): {type(error).__name__}: {error!s}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
        if last_error:
            raise last_error
