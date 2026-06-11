"""stdio transport: newline-delimited UTF-8 JSON over stdin/stdout.

The read loop runs on the calling thread and only ever *reads*: it parses each
line and either resolves a client response (unblocking a waiting handler) or
hands the message to a small thread pool. Running handlers off the read loop is
what lets a tool call ``ctx.sample()``/``ctx.elicit()`` mid-execution: the
handler blocks on its worker thread while the read loop stays free to receive
the client's reply.

EBCDIC platforms (z/OS, some IBM systems): this reads and writes the *binary*
buffers of stdin/stdout and does its own UTF-8 encode/decode. That bypasses the
EBCDIC (e.g. IBM-1047) auto-tagging the runtime would otherwise apply to text
streams, keeping the wire format exactly the UTF-8 the MCP spec requires.
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import IO, TYPE_CHECKING, Any, Optional

from ..exceptions import PARSE_ERROR
from ..protocol import json_default, make_error

if TYPE_CHECKING:
    from ..server import AnodizeMCP


def serve_stdio(
    server: AnodizeMCP,
    in_stream: Optional[IO[bytes]] = None,
    out_stream: Optional[IO[bytes]] = None,
    *,
    max_workers: int = 8,
) -> None:
    inp = in_stream if in_stream is not None else sys.stdin.buffer
    out = out_stream if out_stream is not None else sys.stdout.buffer

    write_lock = threading.Lock()

    def send(message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False, default=json_default).encode("utf-8")
        with write_lock:
            out.write(data + b"\n")
            out.flush()

    session = server.new_session(send=send)
    session.transport = "stdio"
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="anodize-stdio")

    def process(message: dict[str, Any]) -> None:
        try:
            response = server.handle_message(message, session)
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            response = make_error(message.get("id"), -32603, f"{type(exc).__name__}: {exc}")
        if response is not None:
            send(response)

    try:
        for raw_line in inp:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                send(make_error(None, PARSE_ERROR, f"parse error: {exc}"))
                continue

            if (
                isinstance(message, dict)
                and message.get("jsonrpc") == "2.0"
                and message.get("method") is None
            ):
                # A client response to a server-initiated request: resolve it on
                # the read thread so the waiting handler unblocks promptly.
                session.resolve_response(message)
                continue

            executor.submit(process, message)
    finally:
        session.fail_pending("stdin closed")
        executor.shutdown(wait=True)
