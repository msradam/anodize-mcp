"""Per-connection session state shared between the dispatcher and a transport.

A session holds two things the dispatcher cares about:

* inbound negotiation state (client info, capabilities, log level,
  subscriptions), and
* the machinery for *outbound* requests: when a handler calls
  ``ctx.sample()``/``ctx.elicit()``/``ctx.list_roots()`` the server sends a
  request to the client and blocks until the client's response arrives. The
  transport feeds those responses back in via :meth:`resolve_response`.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .exceptions import INTERNAL_ERROR, McpError
from .protocol import make_request

# Syslog-style severities, ordered from least to most severe. Used to filter
# log notifications against the level the client requested via logging/setLevel.
LOG_LEVELS: dict[str, int] = {
    "debug": 0,
    "info": 1,
    "notice": 2,
    "warning": 3,
    "error": 4,
    "critical": 5,
    "alert": 6,
    "emergency": 7,
}


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[dict[str, Any]] = None


class Session:
    """Mutable state for one client connection."""

    def __init__(self, send: Optional[Callable[[dict[str, Any]], None]] = None):
        self._send = send
        self.initialized = False
        self.client_info: dict[str, Any] = {}
        self.client_capabilities: dict[str, Any] = {}
        self.protocol_version = ""
        # None means no filtering: every log notification is forwarded until
        # the client narrows it via logging/setLevel, matching FastMCP.
        self.log_level: Optional[str] = None
        # Every session gets an id (FastMCP always returns a string from
        # ctx.session_id); the HTTP transport overwrites it with the wire id.
        self.session_id: Optional[str] = str(uuid.uuid4())
        self.subscriptions: set[str] = set()
        self.state: dict[str, Any] = {}
        self.transport: Optional[str] = None

        self._pending: dict[str, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._id_counter = 0
        self._thread_sink = threading.local()
        self._id_lock = threading.Lock()

    # -- outbound messages ------------------------------------------------

    def push_thread_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Divert this thread's outbound messages to ``sink``.

        The HTTP POST handler uses this so notifications produced while a
        request is being handled travel on that request's own SSE response,
        as FastMCP streams them, rather than racing it on the GET stream.
        """
        self._thread_sink.sink = sink

    def pop_thread_sink(self) -> None:
        self._thread_sink.sink = None

    def send_message(self, message: dict[str, Any]) -> None:
        sink = getattr(self._thread_sink, "sink", None)
        if sink is not None:
            sink(message)
            return
        if self._send is not None:
            self._send(message)

    def _next_request_id(self) -> str:
        with self._id_lock:
            self._id_counter += 1
            return f"srv-{self._id_counter}"

    def send_request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Any:
        """Send a server-to-client request and block until the response arrives.

        Raises :class:`McpError` on a JSON-RPC error, timeout, or closed
        connection.
        """
        if self._send is None:
            raise McpError("no transport available for server-initiated requests")

        request_id = self._next_request_id()
        pending = _Pending()
        with self._pending_lock:
            self._pending[request_id] = pending

        self.send_message(make_request(request_id, method, params))

        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise McpError(
                f"request {method!r} timed out after {timeout}s",
                data={"reason": "timeout", "method": method},
            )

        if pending.error is not None:
            raise McpError(
                pending.error.get("message", "request failed"),
                code=pending.error.get("code", INTERNAL_ERROR),
                data=pending.error.get("data"),
            )
        return pending.result

    def resolve_response(self, message: dict[str, Any]) -> None:
        """Deliver a client response to whichever handler is waiting on it."""
        request_id = message.get("id")
        if not isinstance(request_id, str):
            return
        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        if "error" in message:
            pending.error = message["error"]
        else:
            pending.result = message.get("result")
        pending.event.set()

    def fail_pending(self, reason: str = "connection closed") -> None:
        with self._pending_lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for pending in pendings:
            pending.error = {"code": INTERNAL_ERROR, "message": reason}
            pending.event.set()

    # -- helpers ----------------------------------------------------------

    def should_log(self, level: str) -> bool:
        if self.log_level is None:
            return True
        return LOG_LEVELS.get(level, 1) >= LOG_LEVELS.get(self.log_level, 1)
