"""Protocol-level constants and JSON-RPC envelope helpers."""

from __future__ import annotations

from typing import Any, Optional, Union

# The protocol revision this server implements. Sent back verbatim when the
# client requests it, otherwise the latest supported version is offered.
LATEST_PROTOCOL_VERSION = "2025-06-18"

# Supported protocol revisions, newest first.
SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

JSONRPC_VERSION = "2.0"

# A JSON-RPC id is a string, number, or null.
RequestId = Union[str, int, None]


def is_request(message: dict[str, Any]) -> bool:
    return "method" in message and "id" in message


def is_notification(message: dict[str, Any]) -> bool:
    return "method" in message and "id" not in message


def is_response(message: dict[str, Any]) -> bool:
    return "method" not in message and ("result" in message or "error" in message)


def make_response(request_id: RequestId, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def make_error(request_id: RequestId, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def make_notification(method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def make_request(
    request_id: RequestId, method: str, params: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg
