"""Protocol-level constants and JSON-RPC envelope helpers."""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import decimal
import enum
import uuid
from typing import Any, Optional, Union


def _iso_duration(td: _dt.timedelta) -> str:
    """ISO 8601 duration, the form pydantic serializes timedeltas to."""
    sign = "-" if td.total_seconds() < 0 else ""
    td = abs(td)
    seconds = td.seconds + td.microseconds / 1_000_000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    out = f"{sign}P"
    if td.days:
        out += f"{td.days}D"
    if hours or minutes or secs or not td.days:
        out += "T"
        if hours:
            out += f"{int(hours)}H"
        if minutes:
            out += f"{int(minutes)}M"
        if secs or (not hours and not minutes and not td.days):
            text = f"{secs:.6f}".rstrip("0").rstrip(".")
            out += f"{text}S"
    return out


def json_default(obj: Any) -> Any:
    """``json.dumps(default=...)`` hook so no value can crash the encoder.

    Handler return values (and structured content) may contain types JSON does
    not know: bytes, datetimes, decimals, UUIDs, sets, enums, dataclasses. Map
    each to a JSON-friendly form; fall back to ``str`` so serialization never
    raises and a malformed value degrades to text instead of killing the reply.
    """
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        # Pydantic writes UTC offsets as Z; match it for byte-level parity.
        iso = obj.isoformat()
        return iso[:-6] + "Z" if iso.endswith("+00:00") else iso
    if isinstance(obj, _dt.timedelta):
        return _iso_duration(obj)
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, enum.Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    # A pydantic-style model (the server's own choice) serializes via its own dump.
    dump = getattr(obj, "model_dump", None)
    if callable(dump):  # pydantic v2
        return dump(mode="json")
    if hasattr(obj, "__fields__") and callable(getattr(obj, "dict", None)):  # pydantic v1
        return obj.dict()
    return str(obj)


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
