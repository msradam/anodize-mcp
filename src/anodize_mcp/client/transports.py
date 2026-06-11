"""Client transports, mirroring ``fastmcp.client.transports``.

``FastMCPTransport`` connects in-process to an anodize server; the stdio transport
launches a subprocess. ``_make_transport`` picks one from the target passed to
:class:`~anodize_mcp.client.client.Client`.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import urllib.error
import urllib.request
from queue import Queue
from typing import Any, Iterator, Optional

from ..transports.memory import SHUTDOWN, serve_memory

# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class FastMCPTransport:
    """In-memory transport that connects a client directly to a server.

    Named to match ``fastmcp.client.transports.FastMCPTransport`` so FastMCP
    code that wraps a server (``Client(transport=FastMCPTransport(server))``)
    is a drop-in.
    """

    def __init__(self, server: Any):
        self._server = server
        self._inbox: Queue[Any] = Queue()

    def start(self, outbox: Queue[Any]) -> None:
        threading.Thread(
            target=serve_memory, args=(self._server, self._inbox, outbox), daemon=True
        ).start()

    def send(self, message: dict[str, Any]) -> None:
        self._inbox.put(message)

    def close(self) -> None:
        self._inbox.put(SHUTDOWN)


class _StdioTransport:
    def __init__(self, command: list[str], env: Optional[dict[str, str]] = None):
        self._command = command
        self._env = env
        self._proc: Optional[subprocess.Popen[bytes]] = None

    def start(self, outbox: Queue[Any]) -> None:
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=self._env,
        )
        threading.Thread(target=self._read, args=(outbox,), daemon=True).start()

    def _read(self, outbox: Queue[Any]) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = raw.strip()
            if line:
                with contextlib.suppress(ValueError):
                    outbox.put(json.loads(line.decode("utf-8")))
        outbox.put(SHUTDOWN)

    def send(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self._proc.stdin.flush()

    def close(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(Exception):
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        with contextlib.suppress(Exception):
            self._proc.terminate()


def _iter_sse(stream: Any) -> Iterator[dict[str, Any]]:
    """Yield JSON payloads from a text/event-stream response, one per SSE event."""
    data: list[str] = []
    for raw in stream:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line == "":
            if data:
                with contextlib.suppress(ValueError):
                    yield json.loads("\n".join(data))
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        # Lines starting with ":" (comments) and other fields are ignored.


class StreamableHttpTransport:
    """Speak MCP Streamable HTTP to a server URL, using only the standard library.

    Mirrors ``fastmcp.client.transports.StreamableHttpTransport``: POST each
    message, read the JSON or ``text/event-stream`` reply, carry the
    ``Mcp-Session-Id`` the server assigns, and open a GET stream for
    server-initiated messages (progress, sampling, elicitation).
    """

    def __init__(self, url: str, headers: Optional[dict[str, str]] = None, timeout: float = 30.0):
        self._url = url
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._session_id: Optional[str] = None
        self._outbox: Optional[Queue[Any]] = None
        self._closed = False
        self._sse_started = False

    def start(self, outbox: Queue[Any]) -> None:
        self._outbox = outbox

    def send(self, message: dict[str, Any]) -> None:
        threading.Thread(target=self._post, args=(message,), daemon=True).start()

    def _request_headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, **self._headers}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, message: dict[str, Any]) -> None:
        if self._closed or self._outbox is None:
            return
        headers = self._request_headers("application/json, text/event-stream")
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self._url, data=json.dumps(message).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            # A read timeout keeps a misbehaving server from hanging the client.
            resp: Any = urllib.request.urlopen(req, timeout=self._timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            resp = exc  # 4xx/5xx still carry a JSON-RPC error body
        except OSError:
            return
        with contextlib.suppress(Exception), resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
                self._ensure_sse_stream()
            if "text/event-stream" in (resp.headers.get("Content-Type") or ""):
                for msg in _iter_sse(resp):
                    self._outbox.put(msg)
            else:
                body = resp.read()
                if body.strip():
                    with contextlib.suppress(ValueError):
                        self._outbox.put(json.loads(body))

    def _ensure_sse_stream(self) -> None:
        if self._sse_started:
            return
        self._sse_started = True
        threading.Thread(target=self._run_sse, daemon=True).start()

    def _run_sse(self) -> None:
        req = urllib.request.Request(
            self._url, headers=self._request_headers("text/event-stream"), method="GET"
        )
        try:
            resp = urllib.request.urlopen(req)  # noqa: S310 - caller-supplied MCP endpoint
        except (urllib.error.HTTPError, OSError):
            return
        with contextlib.suppress(Exception), resp:
            for msg in _iter_sse(resp):
                if self._closed or self._outbox is None:
                    return
                self._outbox.put(msg)

    def close(self) -> None:
        self._closed = True
        if self._outbox is not None:
            self._outbox.put(SHUTDOWN)


def _make_transport(target: Any, env: Optional[dict[str, str]]) -> Any:
    if isinstance(target, str) and target.startswith(("http://", "https://")):
        return StreamableHttpTransport(target)
    if hasattr(target, "handle_message") and hasattr(target, "new_session"):
        return FastMCPTransport(target)
    if isinstance(target, (list, tuple)):
        return _StdioTransport([str(x) for x in target], env=env)
    if hasattr(target, "start") and hasattr(target, "send") and hasattr(target, "close"):
        return target
    raise TypeError(f"cannot build a transport from {target!r}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
