"""Exceptions and JSON-RPC error codes used across the package."""

from __future__ import annotations

from typing import Any, Optional

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP-specific code for a resource that could not be found.
RESOURCE_NOT_FOUND = -32002


class McpError(Exception):
    """An error that maps to a JSON-RPC error response.

    Raise this (or a subclass) from anywhere in request handling to send a
    structured JSON-RPC error back to the client.
    """

    def __init__(self, message: str, code: int = INTERNAL_ERROR, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


class InvalidParams(McpError):
    def __init__(self, message: str, data: Any = None):
        super().__init__(message, code=INVALID_PARAMS, data=data)


class MethodNotFound(McpError):
    def __init__(self, message: str, data: Any = None):
        super().__init__(message, code=METHOD_NOT_FOUND, data=data)


class ToolError(Exception):
    """Raised by a tool to signal a recoverable, user-facing failure.

    The dispatcher turns this into a tool result with ``isError: true`` rather
    than a protocol-level JSON-RPC error, which is what the MCP spec recommends
    for business-logic failures.
    """

    def __init__(self, message: str, *, details: Optional[Any] = None):
        super().__init__(message)
        self.message = message
        self.details = details


class ResourceError(ToolError):
    """Like :class:`ToolError` but for resource reads."""
