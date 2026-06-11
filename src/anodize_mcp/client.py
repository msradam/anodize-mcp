"""An async MCP client, shaped like FastMCP's.

``Client(server)`` connects to an :class:`~anodize_mcp.AnodizeMCP` instance in
process (the FastMCP-style test connection); ``Client(["python", "srv.py"])``
launches a subprocess and speaks stdio. The client declares ``sampling`` /
``elicitation`` / ``roots`` capabilities when the matching handler is supplied
and answers those server-initiated requests.

    async with Client(mcp) as client:
        tools = await client.list_tools()
        result = await client.call_tool("add", {"a": 1, "b": 2})
        print(result.data)
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import subprocess
import threading
from queue import Queue
from typing import Any, Callable, Optional, Union

from .attrdict import wrap as _wrap
from .protocol import (
    LATEST_PROTOCOL_VERSION,
    make_error,
    make_notification,
    make_request,
    make_response,
)
from .transports.memory import SHUTDOWN, serve_memory


class ClientError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class CallToolResult:
    """The result of ``call_tool``; field names follow FastMCP."""

    def __init__(self, raw: dict[str, Any]):
        self.content: list[Any] = _wrap(raw.get("content", []))
        self.structured_content: Optional[Any] = _wrap(raw.get("structuredContent"))
        self.is_error: bool = raw.get("isError", False)
        self.meta: Optional[Any] = _wrap(raw.get("_meta"))
        self._wrapped: bool = bool((raw.get("_meta") or {}).get("fastmcp", {}).get("wrap_result"))

    @property
    def data(self) -> Any:
        # A non-object return is carried as {"result": value} and flagged in
        # _meta; unwrap it back to the original value, matching FastMCP's .data.
        sc = self.structured_content
        if isinstance(sc, dict) and self._wrapped:
            return sc.get("result")
        return sc

    @property
    def text(self) -> Optional[str]:
        for block in self.content:
            if block.get("type") == "text":
                return block.get("text")
        return None


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


class Client:
    def __init__(
        self,
        target: Any = None,
        *,
        transport: Any = None,
        sampling_handler: Optional[Callable[..., Any]] = None,
        elicitation_handler: Optional[Callable[..., Any]] = None,
        roots: Optional[Union[list[dict[str, Any]], Callable[[], Any]]] = None,
        log_handler: Optional[Callable[[dict[str, Any]], Any]] = None,
        progress_handler: Optional[Callable[[dict[str, Any]], Any]] = None,
        client_info: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
        env: Optional[dict[str, str]] = None,
        auto_initialize: bool = True,
    ):
        self._auto_initialize = auto_initialize
        self._transport = _make_transport(target if transport is None else transport, env)
        self._sampling_handler = sampling_handler
        self._elicitation_handler = elicitation_handler
        self._roots = roots
        self._log_handler = log_handler
        self._progress_handler = progress_handler
        self._client_info = client_info or {"name": "anodize-client", "version": "0.4.0"}
        self._timeout = timeout
        self._outbox: Queue[Any] = Queue()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._req_id = 0
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.initialize_result: Optional[dict[str, Any]] = None

    async def __aenter__(self) -> Client:
        self._loop = asyncio.get_event_loop()
        self._transport.start(self._outbox)
        self._reader_task = asyncio.create_task(self._read_loop())
        if self._auto_initialize:
            await self._initialize()
        return self

    async def initialize(self) -> Any:
        """Run the initialize handshake explicitly (for ``auto_initialize=False``)."""
        if self.initialize_result is None:
            await self._initialize()
        return self.initialize_result

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self._transport.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task

    # -- message loop -----------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._loop is not None
        while True:
            message = await self._loop.run_in_executor(None, self._outbox.get)
            if message is SHUTDOWN:
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ClientError("connection closed"))
                self._pending.clear()
                return
            await self._dispatch_incoming(message)

    async def _dispatch_incoming(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            request_id = message.get("id")
            future = self._pending.pop(request_id, None) if isinstance(request_id, int) else None
            if future is not None and not future.done():
                future.set_result(message)
        elif "id" in message:
            await self._handle_server_request(message)
        else:
            await self._handle_notification(method, message.get("params") or {})

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        try:
            if method == "sampling/createMessage" and self._sampling_handler is not None:
                result = _sampling_result(await _call(self._sampling_handler, params))
            elif method == "elicitation/create" and self._elicitation_handler is not None:
                result = _elicit_result(await _call(self._elicitation_handler, params))
            elif method == "roots/list":
                result = {"roots": self._roots_list()}
            else:
                self._transport.send(make_error(request_id, -32601, f"unsupported: {method}"))
                return
            self._transport.send(make_response(request_id, result))
        except Exception as exc:  # noqa: BLE001
            self._transport.send(make_error(request_id, -32603, str(exc)))

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/message" and self._log_handler is not None:
            await _call(self._log_handler, params)
        elif method == "notifications/progress" and self._progress_handler is not None:
            await _call(self._progress_handler, params)

    def _roots_list(self) -> list[dict[str, Any]]:
        roots = self._roots() if callable(self._roots) else self._roots
        return list(roots or [])

    # -- requests ---------------------------------------------------------

    async def _request(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        assert self._loop is not None
        self._req_id += 1
        request_id = self._req_id
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending[request_id] = future
        self._transport.send(make_request(request_id, method, params))
        try:
            response = await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ClientError(f"request {method!r} timed out") from exc
        if "error" in response:
            error = response["error"]
            raise ClientError(error.get("message", "error"), error.get("code"), error.get("data"))
        return response["result"]

    async def _notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        self._transport.send(make_notification(method, params))

    async def _initialize(self) -> None:
        capabilities: dict[str, Any] = {}
        if self._sampling_handler is not None:
            capabilities["sampling"] = {}
        if self._elicitation_handler is not None:
            capabilities["elicitation"] = {}
        if self._roots is not None:
            capabilities["roots"] = {"listChanged": False}
        self.initialize_result = _wrap(
            await self._request(
                "initialize",
                {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": capabilities,
                    "clientInfo": self._client_info,
                },
            )
        )
        await self._notify("notifications/initialized")

    async def _list_all(
        self, method: str, key: str, max_pages: Optional[int] = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        pages = 0
        while True:
            result = await self._request(method, {"cursor": cursor} if cursor else {})
            items.extend(result.get(key, []))
            cursor = result.get("nextCursor")
            pages += 1
            if not cursor or (max_pages is not None and pages >= max_pages):
                return [_wrap(item) for item in items]

    async def _list_page(self, method: str, cursor: Optional[str]) -> Any:
        raw = await self._request(method, {"cursor": cursor} if cursor else {})
        raw.setdefault("nextCursor", None)
        return _wrap(raw)

    # -- public API -------------------------------------------------------

    def is_connected(self) -> bool:
        return self._reader_task is not None and not self._reader_task.done()

    async def ping(self) -> bool:
        await self._request("ping")
        return True

    @staticmethod
    def _tool_params(
        name: str, arguments: Optional[dict[str, Any]], meta: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if meta is not None:
            params["_meta"] = meta
        return params

    async def list_tools(self, *, max_pages: Optional[int] = None) -> list[dict[str, Any]]:
        return await self._list_all("tools/list", "tools", max_pages)

    async def list_tools_mcp(self, cursor: Optional[str] = None) -> Any:
        return await self._list_page("tools/list", cursor)

    async def call_tool(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        raise_on_error: bool = True,
        meta: Optional[dict[str, Any]] = None,
    ) -> CallToolResult:
        result = CallToolResult(
            await self._request("tools/call", self._tool_params(name, arguments, meta))
        )
        if result.is_error and raise_on_error:
            raise ClientError(result.text or f"Tool {name!r} returned an error")
        return result

    async def call_tool_mcp(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        meta: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Call a tool and return the raw result without raising on ``isError``."""
        return _wrap(await self._request("tools/call", self._tool_params(name, arguments, meta)))

    async def list_resources(self, *, max_pages: Optional[int] = None) -> list[dict[str, Any]]:
        return await self._list_all("resources/list", "resources", max_pages)

    async def list_resources_mcp(self, cursor: Optional[str] = None) -> Any:
        return await self._list_page("resources/list", cursor)

    async def list_resource_templates(
        self, *, max_pages: Optional[int] = None
    ) -> list[dict[str, Any]]:
        return await self._list_all("resources/templates/list", "resourceTemplates", max_pages)

    async def list_resource_templates_mcp(self, cursor: Optional[str] = None) -> Any:
        return await self._list_page("resources/templates/list", cursor)

    async def read_resource(self, uri: str) -> list[dict[str, Any]]:
        result = await self._request("resources/read", {"uri": uri})
        return [_wrap(item) for item in result["contents"]]

    async def read_resource_mcp(self, uri: str) -> Any:
        return _wrap(await self._request("resources/read", {"uri": uri}))

    async def list_prompts(self, *, max_pages: Optional[int] = None) -> list[dict[str, Any]]:
        return await self._list_all("prompts/list", "prompts", max_pages)

    async def list_prompts_mcp(self, cursor: Optional[str] = None) -> Any:
        return await self._list_page("prompts/list", cursor)

    async def get_prompt(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        return _wrap(
            await self._request("prompts/get", {"name": name, "arguments": arguments or {}})
        )

    async def get_prompt_mcp(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        return _wrap(
            await self._request("prompts/get", {"name": name, "arguments": arguments or {}})
        )

    async def complete(
        self,
        ref: dict[str, Any],
        argument: dict[str, Any],
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"ref": ref, "argument": argument}
        if context is not None:
            params["context"] = {"arguments": context}
        result = await self._request("completion/complete", params)
        return _wrap(result["completion"])

    async def set_logging_level(self, level: str) -> None:
        await self._request("logging/setLevel", {"level": level})


async def _call(handler: Callable[..., Any], params: dict[str, Any]) -> Any:
    out = handler(params)
    if inspect.iscoroutine(out):
        out = await out
    return out


def _sampling_result(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {
            "role": "assistant",
            "content": {"type": "text", "text": value},
            "model": "anodize-client",
        }
    return value


def _elicit_result(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"action": value}
    return value
