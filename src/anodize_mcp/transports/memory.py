"""In-memory server loop over a pair of queues.

This is the stdio loop with the byte framing removed: the client and server
exchange dict messages directly through two queues. It backs ``Client(server)``,
the FastMCP-style in-process test connection. Messages are round-tripped through
JSON so the in-memory path sees the same serialization (bytes to base64,
datetimes to ISO) the wire transports do.
"""

from __future__ import annotations

import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from typing import TYPE_CHECKING, Any

from ..protocol import json_default, make_error

if TYPE_CHECKING:
    from ..server import AnodizeMCP

SHUTDOWN = object()


def _jsonsafe(message: dict[str, Any]) -> Any:
    return json.loads(json.dumps(message, default=json_default))


def serve_memory(
    server: AnodizeMCP,
    inbox: Queue[Any],
    outbox: Queue[Any],
    *,
    # Matches anyio's default thread limiter, which FastMCP runs sync tools on.
    max_workers: int = 40,
) -> None:
    """Read client messages from ``inbox``, write server messages to ``outbox``."""
    session = server.new_session(send=lambda m: outbox.put(_jsonsafe(m)))
    session.transport = "memory"
    # FastMCP runs the lifespan for in-memory connections too; refcounted so
    # concurrent clients share one entry.
    owns_lifespan = server._acquire_lifespan()
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="anodize-memory")

    def process(message: dict[str, Any]) -> None:
        try:
            response = server.handle_message(message, session)
        except Exception as exc:  # noqa: BLE001
            response = make_error(message.get("id"), -32603, f"{type(exc).__name__}: {exc}")
        if response is not None:
            outbox.put(_jsonsafe(response))

    try:
        while True:
            message = inbox.get()
            if message is SHUTDOWN:
                break
            if (
                isinstance(message, dict)
                and message.get("jsonrpc") == "2.0"
                and message.get("method") is None
            ):
                session.resolve_response(message)
                continue
            executor.submit(contextvars.copy_context().run, process, message)
    finally:
        session.fail_pending("in-memory transport closed")
        executor.shutdown(wait=False)
        if owns_lifespan:
            server._release_lifespan()
        outbox.put(SHUTDOWN)
