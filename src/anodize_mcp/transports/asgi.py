"""An ASGI 3 application for the Streamable HTTP transport.

This app is what runs under uvicorn (or any ASGI server) so production concerns
(keep-alive and read timeouts, body limits, graceful shutdown, signal handling)
are handled by the server rather than reimplemented. The stdlib ``http.server``
transport remains a fallback. It reuses the same session manager, auth, custom
routes, and SSE machinery as the stdlib handler.

Request handlers run in a thread (``run_in_executor``) because a tool may block
on a server-initiated request (``ctx.sample``); keeping that off the event loop
lets the client's reply arrive on another request and unblock it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import queue
from typing import TYPE_CHECKING, Any, Callable

from ..auth import _CURRENT_TOKEN, authorize_request
from ..exceptions import INVALID_PARAMS, PARSE_ERROR
from ..protocol import SUPPORTED_PROTOCOL_VERSIONS, is_request, json_default, make_error
from ..routes import Request, coerce_response, parse_query
from .http import _HttpSession, _Manager

if TYPE_CHECKING:
    from ..server import AnodizeMCP

_SSE_KEEPALIVE_SECONDS = 15.0


def make_asgi_app(
    server: AnodizeMCP,
    *,
    endpoint: str = "/mcp",
    allowed_origins: set[str] | None = None,
    stateless: bool = False,
) -> Callable[..., Any]:
    """Build the ASGI application for ``server``."""
    manager = _Manager(server, endpoint, allowed_origins, stateless)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await _lifespan(server, receive, send)
        elif scope["type"] == "http":
            await _http(server, manager, scope, receive, send)

    return app


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


async def _lifespan(server: AnodizeMCP, receive: Any, send: Any) -> None:
    loop = asyncio.get_event_loop()
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                await loop.run_in_executor(None, server._enter_lifespan)
                await send({"type": "lifespan.startup.complete"})
            except Exception as exc:  # noqa: BLE001
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
        elif message["type"] == "lifespan.shutdown":
            try:
                await loop.run_in_executor(None, server._exit_lifespan)
            finally:
                await send({"type": "lifespan.shutdown.complete"})
            return


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


async def _read_body(receive: Any) -> bytes:
    body = b""
    while True:
        event = await receive()
        body += event.get("body", b"")
        if not event.get("more_body"):
            return body


async def _send_json(
    send: Any, status: int, body: dict[str, Any], extra: dict[str, str] | None = None
) -> None:
    data = json.dumps(body, ensure_ascii=False, default=json_default).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]
    for key, value in (extra or {}).items():
        headers.append((key.encode("latin-1"), value.encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": data})


async def _send_status(send: Any, status: int) -> None:
    await send(
        {"type": "http.response.start", "status": status, "headers": [(b"content-length", b"0")]}
    )
    await send({"type": "http.response.body", "body": b""})


def _dispatch(server: AnodizeMCP, message: dict[str, Any], core: Any, access: Any) -> Any:
    token = _CURRENT_TOKEN.set(access)
    try:
        return server.handle_message(message, core)
    finally:
        _CURRENT_TOKEN.reset(token)


async def _http(
    server: AnodizeMCP, manager: _Manager, scope: dict[str, Any], receive: Any, send: Any
) -> None:
    method = scope["method"]
    path = scope["path"]
    headers = _headers(scope)

    if path != manager.endpoint:
        # FastMCP (Starlette redirect_slashes) 307-redirects /mcp/ to /mcp.
        if path == manager.endpoint + "/":
            await send(
                {
                    "type": "http.response.start",
                    "status": 307,
                    "headers": [(b"location", manager.endpoint.encode("ascii"))],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await _custom_route(server, method, path, scope, receive, send, headers)
        return

    if not manager.origin_allowed(headers.get("origin")):
        await _send_status(send, 403)
        return
    version = headers.get("mcp-protocol-version")
    if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
        await _send_status(send, 400)
        return

    outcome, value = authorize_request(server.auth, headers.get("authorization"))
    if outcome != "ok":
        if outcome == "forbidden":
            await _send_json(send, 403, {"error": "insufficient_scope", "required_scopes": value})
            return
        detail = "missing bearer token" if outcome == "missing" else "invalid_token"
        challenge = "Bearer" if outcome == "missing" else 'Bearer error="invalid_token"'
        await _send_json(send, 401, {"error": detail}, {"WWW-Authenticate": challenge})
        return

    if method == "POST":
        await _post(server, manager, scope, receive, send, headers, value)
    elif method == "GET":
        await _get_sse(manager, receive, send, headers)
    elif method == "DELETE":
        session_id = headers.get("mcp-session-id")
        if session_id and manager.delete_session(session_id):
            await _send_status(send, 200)
        else:
            await _send_status(send, 404)
    else:
        await _send_status(send, 405)


async def _custom_route(
    server: AnodizeMCP,
    method: str,
    path: str,
    scope: dict[str, Any],
    receive: Any,
    send: Any,
    headers: dict[str, str],
) -> None:
    handler = server.find_route(method, path)
    if handler is None:
        await _send_status(send, 404)
        return
    request = Request(
        method=method,
        path=path,
        headers=headers,
        query=parse_query(scope.get("query_string", b"").decode("latin-1")),
        body=await _read_body(receive),
    )
    try:
        result = handler(request)
        if inspect.iscoroutine(result):
            result = await result
        status, data, response_headers = coerce_response(result).render()
    except Exception as exc:  # noqa: BLE001
        status = 500
        data = json.dumps({"error": str(exc)}).encode("utf-8")
        response_headers = {"Content-Type": "application/json"}
    out = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in response_headers.items()]
    out.append((b"content-length", str(len(data)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": out})
    await send({"type": "http.response.body", "body": data})


async def _post(
    server: AnodizeMCP,
    manager: _Manager,
    scope: dict[str, Any],
    receive: Any,
    send: Any,
    headers: dict[str, str],
    access: Any,
) -> None:
    raw = await _read_body(receive)
    try:
        message = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        await _send_json(send, 400, make_error(None, PARSE_ERROR, f"parse error: {exc}"))
        return
    if not isinstance(message, dict):
        await _send_json(
            send, 400, make_error(None, INVALID_PARAMS, "batch requests are not supported")
        )
        return

    is_initialize = message.get("method") == "initialize"
    session_id = headers.get("mcp-session-id")
    if manager.stateless or is_initialize:
        http_session: _HttpSession = manager.create_session()
    else:
        existing = manager.get_session(session_id)
        if existing is None:
            status = 404 if session_id else 400
            await _send_json(
                send,
                status,
                make_error(message.get("id"), INVALID_PARAMS, "missing or unknown Mcp-Session-Id"),
            )
            return
        http_session = existing

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None, _dispatch, server, message, http_session.core, access
    )

    if not is_request(message):
        await _send_status(send, 202)
        return
    extra = {"Mcp-Session-Id": http_session.id} if is_initialize else None
    if response is None:
        await _send_status(send, 202)
    else:
        await _send_json(send, 200, response, extra)


async def _get_sse(manager: _Manager, receive: Any, send: Any, headers: dict[str, str]) -> None:
    if "text/event-stream" not in headers.get("accept", ""):
        await _send_status(send, 405)
        return
    http_session = manager.get_session(headers.get("mcp-session-id"))
    if http_session is None and not manager.stateless:
        await _send_status(send, 400)
        return

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream"), (b"cache-control", b"no-cache")],
        }
    )
    if http_session is None:
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        return

    loop = asyncio.get_event_loop()
    disconnected = asyncio.Event()

    async def watch() -> None:
        try:
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    disconnected.set()
                    return
        except Exception:  # noqa: BLE001
            disconnected.set()

    watcher = asyncio.create_task(watch())
    try:
        while not disconnected.is_set():
            try:
                message = await loop.run_in_executor(
                    None, http_session.queue.get, True, _SSE_KEEPALIVE_SECONDS
                )
            except queue.Empty:
                payload = b": keepalive\n\n"
            else:
                payload = f"data: {json.dumps(message, ensure_ascii=False, default=json_default)}\n\n".encode()
            try:
                await send({"type": "http.response.body", "body": payload, "more_body": True})
            except Exception:  # noqa: BLE001
                break
    finally:
        watcher.cancel()
