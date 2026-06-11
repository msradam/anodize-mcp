"""Timing middleware, matching ``fastmcp.server.middleware.timing``.

Logs request durations. Class names, constructor signatures, and the
``fastmcp.timing`` logger names mirror FastMCP so its tests run unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .middleware import CallNext, Middleware, MiddlewareContext


class TimingMiddleware(Middleware):
    """Log the execution time of each request."""

    def __init__(self, logger: Optional[logging.Logger] = None, log_level: int = logging.INFO):
        self.logger = logger or logging.getLogger("fastmcp.timing")
        self.log_level = log_level

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        method = context.method or "unknown"
        start_time = time.perf_counter()
        try:
            result = await call_next(context)
            duration_ms = (time.perf_counter() - start_time) * 1000
            self.logger.log(self.log_level, f"Request {method} completed in {duration_ms:.2f}ms")
            return result
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            self.logger.log(
                self.log_level, f"Request {method} failed after {duration_ms:.2f}ms: {exc}"
            )
            raise


class DetailedTimingMiddleware(Middleware):
    """Per-operation timing for tools, resources, and prompts."""

    def __init__(self, logger: Optional[logging.Logger] = None, log_level: int = logging.INFO):
        self.logger = logger or logging.getLogger("fastmcp.timing.detailed")
        self.log_level = log_level

    async def _time_operation(
        self, context: MiddlewareContext, call_next: CallNext, operation_name: str
    ) -> Any:
        start_time = time.perf_counter()
        try:
            result = await call_next(context)
            duration_ms = (time.perf_counter() - start_time) * 1000
            self.logger.log(self.log_level, f"{operation_name} completed in {duration_ms:.2f}ms")
            return result
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            self.logger.log(
                self.log_level, f"{operation_name} failed after {duration_ms:.2f}ms: {exc}"
            )
            raise

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        tool_name = getattr(context.message, "name", "unknown")
        return await self._time_operation(context, call_next, f"Tool {tool_name!r}")

    async def on_read_resource(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        resource_uri = getattr(context.message, "uri", "unknown")
        return await self._time_operation(context, call_next, f"Resource {resource_uri!r}")

    async def on_get_prompt(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        prompt_name = getattr(context.message, "name", "unknown")
        return await self._time_operation(context, call_next, f"Prompt {prompt_name!r}")

    async def on_list_tools(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        return await self._time_operation(context, call_next, "List tools")

    async def on_list_resources(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        return await self._time_operation(context, call_next, "List resources")

    async def on_list_resource_templates(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        return await self._time_operation(context, call_next, "List resource templates")

    async def on_list_prompts(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        return await self._time_operation(context, call_next, "List prompts")
