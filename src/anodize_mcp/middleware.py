"""Middleware: a chain of hooks wrapped around request dispatch.

Hook names and the ``(context, call_next)`` shape match FastMCP. Subclass
:class:`Middleware`, override the hooks you need, and register the instance with
``mcp.add_middleware(...)``. Hooks may be ``async def`` or plain ``def``; a plain
hook returns ``call_next(context)`` (a coroutine the chain awaits), an async hook
returns ``await call_next(context)``.

``on_message`` runs for every request. The per-operation hooks (``on_call_tool``
and friends) run nested inside it for the matching method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class MiddlewareContext:
    """What a middleware hook receives about the message in flight."""

    message: Any
    method: Optional[str]
    source: str = "client"
    type: str = "request"
    fastmcp_context: Any = None


# method -> the per-operation hook name, matching FastMCP.
OPERATION_HOOKS = {
    "tools/call": "on_call_tool",
    "tools/list": "on_list_tools",
    "resources/read": "on_read_resource",
    "resources/list": "on_list_resources",
    "resources/templates/list": "on_list_resource_templates",
    "prompts/get": "on_get_prompt",
    "prompts/list": "on_list_prompts",
    "completion/complete": "on_complete",
}


class Middleware:
    """Base class. Override any hook; the default passes through unchanged."""

    async def on_message(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_request(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_notification(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_list_tools(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_read_resource(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_list_resources(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_list_resource_templates(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_get_prompt(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_list_prompts(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)

    async def on_complete(self, context: MiddlewareContext, call_next: Any) -> Any:
        return await call_next(context)
