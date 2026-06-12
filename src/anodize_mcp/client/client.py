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
import dataclasses
import inspect
import json
import logging
import warnings
from queue import Queue
from typing import Any, Callable, Optional, Union

from ..attrdict import wrap as _wrap
from ..exceptions import INTERNAL_ERROR, McpError
from ..protocol import (
    LATEST_PROTOCOL_VERSION,
    json_default,
    make_error,
    make_notification,
    make_request,
    make_response,
)
from ..transports.memory import SHUTDOWN
from .transports import _make_transport

logger = logging.getLogger(__name__)


class ClientError(McpError):
    """A client-side request failure.

    Subclasses :class:`McpError` so it is catchable as the SDK's ``McpError`` and
    exposes the structured ``.error`` (code/message/data).
    """

    def __init__(self, message: str, code: Optional[int] = None, data: Any = None):
        super().__init__(message, code=code if code is not None else INTERNAL_ERROR, data=data)


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
        timeout: Optional[float] = None,
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
        # progressToken -> handler, for tool calls that report progress.
        self._progress_handlers: dict[str, Callable[..., Any]] = {}
        self._req_id = 0
        self._progress_seq = 0
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._entered = 0
        self.initialize_result: Optional[dict[str, Any]] = None

    async def __aenter__(self) -> Client:
        # Reference-count nested `async with`; only the outermost opens/closes.
        self._entered += 1
        if self._entered > 1:
            return self
        self._loop = asyncio.get_event_loop()
        self._transport.start(self._outbox)
        self._reader_task = asyncio.create_task(self._read_loop())
        if self._auto_initialize:
            # __aexit__ never runs when __aenter__ raises; close here or the
            # transport (and the executor thread reading it) leaks and blocks
            # event-loop shutdown.
            try:
                await self._initialize()
            except BaseException:
                await self.close()
                raise
        return self

    async def initialize(self) -> Any:
        """Run the initialize handshake explicitly (for ``auto_initialize=False``)."""
        if self.initialize_result is None:
            await self._initialize()
        return self.initialize_result

    async def __aexit__(self, *exc: Any) -> None:
        if self._entered > 0:
            self._entered -= 1
        if self._entered == 0:
            await self.close()

    async def close(self) -> None:
        self._entered = 0
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
            # A failing notification handler must not kill the session;
            # FastMCP logs callback errors and keeps reading.
            try:
                await self._dispatch_incoming(message)
            except Exception:
                logger.exception("error handling incoming message")

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
                result = _sampling_result(await _call_sampling(self._sampling_handler, params))
            elif method == "elicitation/create" and self._elicitation_handler is not None:
                result = _elicit_result(
                    await _call_elicitation(self._elicitation_handler, params), params
                )
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
            # Wrapped so FastMCP-style handlers reading .level/.logger/.data work.
            await _call(self._log_handler, _wrap(params))
        elif method == "notifications/progress":
            token = params.get("progressToken")
            handler = self._progress_handlers.get(token) if token is not None else None
            handler = handler or self._progress_handler
            if handler is not None:
                await _call_progress(handler, params)

    def _roots_list(self) -> list[dict[str, Any]]:
        roots = self._roots() if callable(self._roots) else self._roots
        return list(roots or [])

    # -- requests ---------------------------------------------------------

    async def _request(
        self, method: str, params: Optional[dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> Any:
        if not self.is_connected():
            # Without this a request after close() waits forever on a reader
            # that no longer exists; FastMCP raises the same way.
            raise RuntimeError(
                "Client is not connected. Use the 'async with client:' context manager first."
            )
        assert self._loop is not None
        self._req_id += 1
        request_id = self._req_id
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending[request_id] = future
        self._transport.send(make_request(request_id, method, params))
        # No client-level timeout by default, matching FastMCP; transports
        # still bound their own reads.
        chosen = timeout if timeout is not None else self._timeout
        try:
            response = await future if chosen is None else await asyncio.wait_for(future, chosen)
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
        seen: set[str] = set()
        pages = 0
        while True:
            result = await self._request(method, {"cursor": cursor} if cursor else {})
            items.extend(result.get(key, []))
            cursor = result.get("nextCursor")
            pages += 1
            if not cursor or (max_pages is not None and pages >= max_pages):
                return [_wrap(item) for item in items]
            # A server that repeats a cursor would loop forever; stop and warn, as FastMCP does.
            if cursor in seen:
                warnings.warn(
                    f"{method} returned a repeated pagination cursor; stopping", stacklevel=2
                )
                return [_wrap(item) for item in items]
            seen.add(cursor)

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

    async def _send_tool_call(
        self,
        name: str,
        arguments: Optional[dict[str, Any]],
        *,
        meta: Optional[dict[str, Any]],
        timeout: Optional[float],
        progress_handler: Optional[Callable[..., Any]],
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        handler = progress_handler or self._progress_handler
        token: Optional[str] = None
        merged_meta = dict(meta) if meta else {}
        if handler is not None:
            # The server only reports progress when a progressToken is supplied;
            # generate one and route notifications back to the handler.
            self._progress_seq += 1
            token = f"p{self._progress_seq}"
            merged_meta["progressToken"] = token
            self._progress_handlers[token] = handler
        if merged_meta:
            params["_meta"] = merged_meta
        try:
            return await self._request("tools/call", params, timeout)
        finally:
            if token is not None:
                self._progress_handlers.pop(token, None)

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
        timeout: Optional[float] = None,
        progress_handler: Optional[Callable[..., Any]] = None,
    ) -> CallToolResult:
        result = CallToolResult(
            await self._send_tool_call(
                name, arguments, meta=meta, timeout=timeout, progress_handler=progress_handler
            )
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
        timeout: Optional[float] = None,
        progress_handler: Optional[Callable[..., Any]] = None,
    ) -> Any:
        """Call a tool and return the raw result without raising on ``isError``."""
        return _wrap(
            await self._send_tool_call(
                name, arguments, meta=meta, timeout=timeout, progress_handler=progress_handler
            )
        )

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
            await self._request(
                "prompts/get", {"name": name, "arguments": _stringify_args(arguments)}
            )
        )

    async def get_prompt_mcp(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        return _wrap(
            await self._request(
                "prompts/get", {"name": name, "arguments": _stringify_args(arguments)}
            )
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


def _stringify_args(arguments: Optional[dict[str, Any]]) -> dict[str, str]:
    """JSON-stringify non-string prompt arguments; MCP carries them as strings."""
    return {
        k: v if isinstance(v, str) else json.dumps(v, default=json_default)
        for k, v in (arguments or {}).items()
    }


async def _call(handler: Callable[..., Any], params: dict[str, Any]) -> Any:
    out = handler(params)
    if inspect.iscoroutine(out):
        out = await out
    return out


def _positional_count(fn: Callable[..., Any]) -> int:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return 1
    # *args accepts everything; treat it as the full FastMCP signature.
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return 99
    kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    return sum(1 for p in params if p.kind in kinds)


async def _call_progress(handler: Callable[..., Any], params: dict[str, Any]) -> Any:
    """Invoke a progress handler, supporting both anodize's ``handler(params)``
    and FastMCP's ``handler(progress, total, message)`` signatures."""
    n = _positional_count(handler)
    if n >= 3:
        out = handler(params.get("progress"), params.get("total"), params.get("message"))
    elif n == 2:
        out = handler(params.get("progress"), params.get("total"))
    else:
        out = handler(_wrap(params))
    if inspect.iscoroutine(out):
        out = await out
    return out


async def _call_sampling(handler: Callable[..., Any], params: dict[str, Any]) -> Any:
    """Invoke a sampling handler, supporting both anodize's ``handler(params)`` and
    FastMCP's ``handler(messages, params, context)`` signatures."""
    wrapped = _wrap(params)  # so handlers reading params.systemPrompt etc. work
    messages = _wrap(params.get("messages", []))
    n = _positional_count(handler)
    if n >= 3:
        out = handler(messages, wrapped, None)
    elif n == 2:
        out = handler(messages, wrapped)
    else:
        out = handler(wrapped)
    if inspect.iscoroutine(out):
        out = await out
    return out


class _ElicitResponse:
    """Permissive stand-in for FastMCP's generated elicitation response type.

    A FastMCP elicitation handler builds its reply by calling the response type
    with field values; capture those and surface them as an ``accept`` result.
    """

    def __init__(self, **fields: Any):
        self.__dict__.update(fields)
        self._fields = fields


async def _call_elicitation(handler: Callable[..., Any], params: dict[str, Any]) -> Any:
    """Invoke an elicitation handler, supporting anodize's ``handler(params)`` and
    FastMCP's ``handler(message, response_type, params, context)`` signatures."""
    wrapped = _wrap(params)
    n = _positional_count(handler)
    if n >= 4:
        out = handler(params.get("message"), _ElicitResponse, wrapped, None)
    elif n == 2:
        out = handler(params.get("message"), wrapped)
    else:
        out = handler(wrapped)
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


_ELICIT_ACTIONS = ("accept", "decline", "cancel")


def _elicit_result(value: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Build the wire result from a handler return, accepting both conventions.

    anodize convention: a bare action string or a raw protocol dict with an
    ``action`` key passes through. FastMCP convention: any other return is an
    accept whose content is the value (dataclasses and pydantic models are
    serialized; a scalar is wrapped as ``{"value": ...}`` when the requested
    schema is the scalar shorthand, as FastMCP's client does).
    """
    if isinstance(value, _ElicitResponse):
        return {"action": "accept", "content": value._fields}
    if isinstance(value, str) and value in _ELICIT_ACTIONS:
        return {"action": value}
    if isinstance(value, dict):
        if value.get("action") in _ELICIT_ACTIONS:
            return value
        return {"action": "accept", "content": value}
    if value is None:
        return {"action": "accept"}
    if hasattr(value, "action"):  # an ElicitResult, either anodize's or FastMCP's
        out: dict[str, Any] = {"action": value.action}
        content = getattr(value, "content", None)
        if content is None:
            content = getattr(value, "data", None)
        if content is not None:
            out["content"] = (
                content if isinstance(content, dict) else _elicit_content(content, params)
            )
        return out
    return {"action": "accept", "content": _elicit_content(value, params)}


def _elicit_content(value: Any, params: dict[str, Any]) -> dict[str, Any]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    schema = params.get("requestedSchema") or {}
    if set(schema.get("properties", {}).keys()) == {"value"}:
        return {"value": value}
    raise ValueError(f"elicitation responses must be a JSON object; received {value!r}")
