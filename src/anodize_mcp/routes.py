"""Request and response types for custom HTTP routes.

FastMCP's ``@mcp.custom_route`` handlers take a Starlette ``Request`` and return
a Starlette ``Response``. To stay dependency-free these are small stand-ins: the
decorator and the ``handler(request) -> response`` shape match, the request and
response objects are anodize's own. A handler may return a :class:`Response`, a
``(status, body)`` tuple, a ``dict``/``list`` (sent as JSON), a ``str`` (text),
or ``bytes``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs

from .protocol import json_default


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass
class Response:
    status: int = 200
    body: Any = b""
    headers: dict[str, str] = field(default_factory=dict)
    media_type: Optional[str] = None

    def render(self) -> tuple[int, bytes, dict[str, str]]:
        headers = dict(self.headers)
        if isinstance(self.body, (bytes, bytearray)):
            data = bytes(self.body)
            content_type = self.media_type or "application/octet-stream"
        elif isinstance(self.body, str):
            data = self.body.encode("utf-8")
            content_type = self.media_type or "text/plain; charset=utf-8"
        else:
            data = json.dumps(self.body, default=json_default).encode("utf-8")
            content_type = self.media_type or "application/json"
        headers.setdefault("Content-Type", content_type)
        return self.status, data, headers


def coerce_response(value: Any) -> Response:
    """Turn a handler's return value into a :class:`Response`."""
    if isinstance(value, Response):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return Response(status=int(value[0]), body=value[1])
    if value is None:
        return Response(body=b"")
    return Response(body=value)


def parse_query(query_string: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(query_string).items()}
