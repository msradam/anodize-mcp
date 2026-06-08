"""Streamable HTTP transport built on the standard-library HTTP server.

Implements the single-endpoint Streamable HTTP transport from the MCP spec:

* ``POST`` carries one JSON-RPC message. Requests get a single
  ``application/json`` response; notifications and responses get ``202``.
* ``GET`` opens a ``text/event-stream`` (SSE) channel for server-initiated
  notifications (logging, progress).
* ``DELETE`` terminates a session.

Sessions are tracked with the ``Mcp-Session-Id`` header, assigned at
``initialize`` time. ``Origin`` is validated to prevent DNS-rebinding attacks,
and the server binds to localhost by default.

No third-party web framework is involved: this uses ``http.server`` and
``socketserver`` from the standard library.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from ..exceptions import INVALID_PARAMS, PARSE_ERROR
from ..protocol import (
    SUPPORTED_PROTOCOL_VERSIONS,
    is_request,
    json_default,
    make_error,
)
from ..session import Session

if TYPE_CHECKING:
    from ..server import AnodizeMCP

_DEFAULT_LOCAL_ORIGINS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_SSE_KEEPALIVE_SECONDS = 15.0


class _HttpSession:
    """A managed session: core protocol state plus an outbound notification queue."""

    def __init__(self, session_id: str, server: AnodizeMCP):
        self.id = session_id
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1024)
        self.core: Session = server.new_session(send=self._enqueue)
        self.core.session_id = session_id

    def _enqueue(self, message: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            # Drop the oldest message to make room rather than block a handler.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(message)
            except queue.Empty:
                pass


class _Manager:
    def __init__(
        self,
        server: AnodizeMCP,
        endpoint: str,
        allowed_origins: Optional[set[str]],
        stateless: bool,
    ):
        self.server = server
        self.endpoint = endpoint
        self.allowed_origins = allowed_origins
        self.stateless = stateless
        self.sessions: dict[str, _HttpSession] = {}
        self._lock = threading.Lock()

    def create_session(self) -> _HttpSession:
        session_id = secrets.token_hex(16)
        session = _HttpSession(session_id, self.server)
        with self._lock:
            self.sessions[session_id] = session
        return session

    def get_session(self, session_id: Optional[str]) -> Optional[_HttpSession]:
        if session_id is None:
            return None
        with self._lock:
            return self.sessions.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            return self.sessions.pop(session_id, None) is not None

    def origin_allowed(self, origin: Optional[str]) -> bool:
        if self.allowed_origins == {"*"}:
            return True
        if origin is None:
            return True  # non-browser clients omit Origin
        host = urlsplit(origin).hostname or origin
        if self.allowed_origins is not None:
            return host in self.allowed_origins or origin in self.allowed_origins
        return host in _DEFAULT_LOCAL_ORIGINS


def _make_handler(manager: _Manager) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # Silence the default stderr request logging; servers can log themselves.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        # -- helpers ------------------------------------------------------

        def _send_json(
            self, status: int, body: dict[str, Any], extra_headers: Optional[dict[str, str]] = None
        ) -> None:
            data = json.dumps(body, ensure_ascii=False, default=json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _send_status(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _bad_origin(self) -> bool:
            if not manager.origin_allowed(self.headers.get("Origin")):
                self._send_status(HTTPStatus.FORBIDDEN)
                return True
            return False

        def _protocol_version_ok(self) -> bool:
            version = self.headers.get("MCP-Protocol-Version")
            if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
                self._send_status(HTTPStatus.BAD_REQUEST)
                return False
            return True

        def _path_ok(self) -> bool:
            return urlsplit(self.path).path == manager.endpoint

        # -- verbs --------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            if not self._path_ok():
                self._send_status(HTTPStatus.NOT_FOUND)
                return
            if self._bad_origin() or not self._protocol_version_ok():
                return

            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                message = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST, make_error(None, PARSE_ERROR, f"parse error: {exc}")
                )
                return

            if not isinstance(message, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    make_error(None, INVALID_PARAMS, "batch requests are not supported"),
                )
                return

            method = message.get("method")
            is_initialize = method == "initialize"
            session_id = self.headers.get("Mcp-Session-Id")

            if manager.stateless or is_initialize:
                http_session: _HttpSession = manager.create_session()
            else:
                existing = manager.get_session(session_id)
                if existing is None:
                    status = HTTPStatus.NOT_FOUND if session_id else HTTPStatus.BAD_REQUEST
                    self._send_json(
                        status,
                        make_error(
                            message.get("id"),
                            INVALID_PARAMS,
                            "missing or unknown Mcp-Session-Id",
                        ),
                    )
                    return
                http_session = existing

            # No coarse per-session lock here: a handler may block waiting for a
            # client reply that arrives on a *different* POST, which must be free
            # to run concurrently and resolve the waiter.
            response = manager.server.handle_message(message, http_session.core)

            if not is_request(message):
                # Notification, or a client response to a server-initiated
                # request (already routed to the waiting handler): nothing to
                # return beyond an acknowledgement.
                self._send_status(HTTPStatus.ACCEPTED)
                return

            extra = {"Mcp-Session-Id": http_session.id} if is_initialize else None
            if response is None:
                self._send_status(HTTPStatus.ACCEPTED)
            else:
                self._send_json(HTTPStatus.OK, response, extra_headers=extra)

        def do_GET(self) -> None:  # noqa: N802
            if not self._path_ok():
                self._send_status(HTTPStatus.NOT_FOUND)
                return
            if self._bad_origin() or not self._protocol_version_ok():
                return

            accept = self.headers.get("Accept", "")
            if "text/event-stream" not in accept:
                self._send_status(HTTPStatus.METHOD_NOT_ALLOWED)
                return

            http_session = manager.get_session(self.headers.get("Mcp-Session-Id"))
            if http_session is None and not manager.stateless:
                self._send_status(HTTPStatus.BAD_REQUEST)
                return

            self._stream_sse(http_session)

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._path_ok():
                self._send_status(HTTPStatus.NOT_FOUND)
                return
            session_id = self.headers.get("Mcp-Session-Id")
            if session_id and manager.delete_session(session_id):
                self._send_status(HTTPStatus.OK)
            else:
                self._send_status(HTTPStatus.NOT_FOUND)

        # -- SSE ----------------------------------------------------------

        def _stream_sse(self, http_session: Optional[_HttpSession]) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            if http_session is None:
                # Stateless mode has no per-session queue; keep the stream open
                # only long enough to satisfy the client, then close.
                return

            try:
                while True:
                    try:
                        message = http_session.queue.get(timeout=_SSE_KEEPALIVE_SECONDS)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    payload = json.dumps(message, ensure_ascii=False, default=json_default)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


def serve_http(
    server: AnodizeMCP,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    endpoint: str = "/mcp",
    allowed_origins: Optional[set[str]] = None,
    stateless: bool = False,
) -> None:
    """Run the Streamable HTTP transport until interrupted.

    ``allowed_origins`` defaults to localhost-only Origin validation; pass a set
    of hostnames to allow, or ``{"*"}`` to disable the check. ``stateless=True``
    skips ``Mcp-Session-Id`` tracking (each POST is independent; no SSE
    notifications).
    """
    manager = _Manager(
        server=server,
        endpoint=endpoint,
        allowed_origins=allowed_origins,
        stateless=stateless,
    )
    handler_cls = _make_handler(manager)
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
