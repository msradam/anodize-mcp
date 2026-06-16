"""Keep-alive ping middleware.

Sends a ``notifications/message`` to every connected session on a configurable
interval so SSE/HTTP connections are not dropped by proxies or load balancers
that close idle connections.
"""

from __future__ import annotations

import contextlib
import threading
import weakref
from typing import Any

from ...protocol import make_notification
from ...session import Session
from .middleware import CallNext, Middleware, MiddlewareContext


class PingMiddleware(Middleware):
    """Periodically ping connected sessions to keep SSE connections alive.

    Args:
        interval_ms: Time between pings in milliseconds. Defaults to 30 000.
    """

    def __init__(self, interval_ms: int = 30000):
        self.interval_ms = interval_ms
        self._sessions: weakref.WeakSet[Session] = weakref.WeakSet()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="anodize-ping")
        self._thread.start()

    def _run(self) -> None:
        interval_s = self.interval_ms / 1000.0
        while not self._stop.wait(interval_s):
            ping = make_notification(
                "notifications/message",
                {
                    "level": "debug",
                    "data": {"msg": "ping"},
                },
            )
            for session in list(self._sessions):
                with contextlib.suppress(Exception):
                    session.send_message(ping)

    def stop(self) -> None:
        """Stop the background ping thread."""
        self._stop.set()

    async def on_initialize(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        result = await call_next(context)
        ctx = context.fastmcp_context
        if ctx is not None:
            session = getattr(ctx, "session", None) or getattr(ctx, "_session", None)
            if isinstance(session, Session):
                self._sessions.add(session)
        return result
