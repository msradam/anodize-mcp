"""Client transports, mirroring ``fastmcp.client.transports``.

``FastMCPTransport`` connects in-process to an anodize server; the stdio transport
launches a subprocess. ``_make_transport`` picks one from the target passed to
:class:`~anodize_mcp.client.client.Client`.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from queue import Queue
from typing import Any, Iterator, Optional

from ..protocol import json_default, make_error
from ..transports.memory import SHUTDOWN, _jsonsafe, serve_memory

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
        # A fresh inbox per connection: a second close() after shutdown would
        # otherwise leave a stale SHUTDOWN for the next connection to read.
        self._inbox = Queue()
        threading.Thread(
            target=serve_memory, args=(self._server, self._inbox, outbox), daemon=True
        ).start()

    def send(self, message: dict[str, Any]) -> None:
        # Round-trip through JSON so the in-memory path carries exactly what a
        # wire transport would, in both directions.
        self._inbox.put(_jsonsafe(message))

    def close(self) -> None:
        self._inbox.put(SHUTDOWN)


class StdioTransport:
    """Launch a subprocess and communicate over stdio.

    ``StdioTransport(command, args, env)`` mirrors the FastMCP public API:
    ``command`` is the executable name, ``args`` are the arguments.

    When ``keep_alive=True`` (the default), ``close()`` does not terminate the
    subprocess, so re-entering an ``async with Client(transport)`` block reuses
    the same process. The process is still terminated on garbage collection.
    Set ``keep_alive=False`` to terminate on every ``close()``.
    """

    def __init__(
        self,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        keep_alive: bool = True,
    ):
        self._command = [command, *(args or [])]
        self._env = env
        self._keep_alive = keep_alive
        self._proc: Optional[subprocess.Popen[bytes]] = None

    def start(self, outbox: Queue[Any]) -> None:
        if self._proc is None or self._proc.poll() is not None:
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
        self._proc.stdin.write(json.dumps(message, default=json_default).encode("utf-8") + b"\n")
        self._proc.stdin.flush()

    def close(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(Exception):
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        if not self._keep_alive:
            with contextlib.suppress(Exception):
                self._proc.terminate()

    def __del__(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.terminate()


# Keep the private name as an alias for internal use.
_StdioTransport = StdioTransport


class PythonStdioTransport(StdioTransport):
    """Run a Python script as a stdio subprocess using the current interpreter."""

    def __init__(
        self,
        script: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        keep_alive: bool = False,
    ):
        super().__init__(sys.executable, [script, *(args or [])], env=env, keep_alive=keep_alive)


class NodeStdioTransport(StdioTransport):
    """Run a Node.js script as a stdio subprocess."""

    def __init__(
        self,
        script: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        keep_alive: bool = False,
    ):
        super().__init__("node", [script, *(args or [])], env=env, keep_alive=keep_alive)


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

    def __init__(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
        auth: Optional[str] = None,
    ):
        self._url = url
        self._headers = dict(headers or {})
        if auth is not None:
            self._headers["Authorization"] = f"Bearer {auth}"
        self._timeout = timeout
        self._session_id: Optional[str] = None
        self._outbox: Optional[Queue[Any]] = None
        self._closed = False
        self._sse_started = False

    def start(self, outbox: Queue[Any]) -> None:
        self._outbox = outbox
        # Allow restart after close (Client re-entered).
        self._closed = False
        self._sse_started = False
        self._session_id = None

    def send(self, message: dict[str, Any]) -> None:
        threading.Thread(target=self._post, args=(message,), daemon=True).start()

    def _request_headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept} | self._headers
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, message: dict[str, Any]) -> None:
        if self._closed or self._outbox is None:
            return
        headers = self._request_headers("application/json, text/event-stream")
        headers["Content-Type"] = "application/json"
        data = json.dumps(message, default=json_default).encode("utf-8")

        # Any failure must resolve the pending request, or the caller waits out
        # its full timeout for an answer that can never arrive.
        request_id = message.get("id") if isinstance(message, dict) else None

        def fail(text: str) -> None:
            if request_id is not None and self._outbox is not None:
                self._outbox.put(make_error(request_id, -32000, text))

        resp: Any = None
        for _ in range(3):  # follow a couple of redirects (e.g. /mcp/ to /mcp)
            req = urllib.request.Request(self._url, data=data, headers=headers, method="POST")
            try:
                # A read timeout keeps a misbehaving server from hanging the client.
                resp = urllib.request.urlopen(req, timeout=self._timeout)  # noqa: S310
            except urllib.error.HTTPError as exc:
                # urllib refuses to auto-redirect a POST; do it ourselves and
                # remember the resolved URL for subsequent requests.
                location = exc.headers.get("Location") if exc.headers else None
                if exc.code in (301, 302, 307, 308) and location:
                    self._url = urllib.parse.urljoin(self._url, location)
                    continue
                resp = exc  # a 4xx/5xx may still carry a JSON-RPC error body
            except OSError as exc:
                fail(f"connection failed: {exc}")
                return
            break
        if resp is None:
            fail("too many redirects")
            return
        with resp:
            try:
                status = int(getattr(resp, "status", None) or getattr(resp, "code", 200) or 200)
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                    self._ensure_sse_stream()
                if "text/event-stream" in (resp.headers.get("Content-Type") or ""):
                    for msg in _iter_sse(resp):
                        self._outbox.put(msg)
                    return
                body = resp.read()
                parsed: Any = None
                if body.strip():
                    with contextlib.suppress(ValueError):
                        parsed = json.loads(body)
                if isinstance(parsed, dict) and parsed.get("jsonrpc") == "2.0":
                    self._outbox.put(parsed)
                elif status >= 400:
                    # Not a JSON-RPC reply (e.g. an auth layer's 401); fail fast.
                    detail = parsed.get("error") if isinstance(parsed, dict) else None
                    fail(f"HTTP {status}: {detail or 'request failed'}")
            except Exception as exc:  # noqa: BLE001
                fail(f"transport error: {exc}")

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


def _make_transport(target: Any, env: Optional[dict[str, str]], auth: Optional[str] = None) -> Any:
    if isinstance(target, str) and target.startswith(("http://", "https://")):
        return StreamableHttpTransport(target, auth=auth)
    if hasattr(target, "handle_message") and hasattr(target, "new_session"):
        return FastMCPTransport(target)
    # A .py/.js script path launches a stdio subprocess, as FastMCP infers.
    if isinstance(target, (str, Path)) and str(target).endswith((".py", ".js")):
        path = str(target)
        if path.endswith(".py"):
            return PythonStdioTransport(path, env=env)
        return NodeStdioTransport(path, env=env)
    if isinstance(target, (list, tuple)):
        cmd = [str(x) for x in target]
        return StdioTransport(cmd[0], cmd[1:] or None, env=env, keep_alive=False)
    if hasattr(target, "start") and hasattr(target, "send") and hasattr(target, "close"):
        return target
    raise TypeError(f"cannot build a transport from {target!r}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
