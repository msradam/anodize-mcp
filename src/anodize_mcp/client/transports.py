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
from queue import Queue
from typing import Any, Optional

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


def _make_transport(target: Any, env: Optional[dict[str, str]]) -> Any:
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
