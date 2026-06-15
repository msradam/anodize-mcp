import importlib.util
import json
import socket
import threading
import time
import unittest
import unittest.mock
from dataclasses import dataclass

from anodize_mcp import AnodizeMCP, Context, Response, StaticTokenVerifier

_HAS_UVICORN = importlib.util.find_spec("uvicorn") is not None
_HAS_HTTPX = importlib.util.find_spec("httpx") is not None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def build_server() -> AnodizeMCP:
    import contextlib

    @contextlib.contextmanager
    def lifespan(server):
        yield {"db": "ready"}

    mcp = AnodizeMCP("asgi-demo", lifespan=lifespan)

    @mcp.tool
    def add(a: int, b: int, ctx: Context) -> int:
        assert ctx.lifespan_context == {"db": "ready"}
        assert ctx.transport == "http"
        return a + b

    @dataclass
    class W:
        temp: float

    @mcp.tool
    def weather() -> W:
        return W(21.5)

    @mcp.tool
    def progressive(ctx: Context) -> str:
        ctx.report_progress(1, total=1, message="step")
        return "done"

    @mcp.custom_route("/health", methods=["GET"])
    def health(request):
        return {"status": "ok"}

    @mcp.custom_route("/echo", methods=["POST"])
    def echo(request):
        return Response(201, {"got": request.json()})

    return mcp


def rpc(response):
    """Parse a JSON-RPC POST reply, JSON or SSE-framed."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        events = [
            json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")
        ]
        return events[-1] if events else None
    return response.json()


@unittest.skipUnless(_HAS_UVICORN and _HAS_HTTPX, "uvicorn and httpx required")
class AsgiUvicornTest(unittest.TestCase):
    def _serve(self, mcp, stateless=True):
        import uvicorn

        port = free_port()
        app = mcp.asgi_app(stateless=stateless)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        self.addCleanup(self._stop, server, thread)
        return f"http://127.0.0.1:{port}", server

    def _stop(self, server, thread):
        server.should_exit = True
        thread.join(timeout=5)

    def _client(self):
        import httpx

        return httpx.Client(
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
        )

    def test_core_endpoints(self):
        base, _ = self._serve(build_server())
        with self._client() as c:
            init = c.post(
                base + "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {},
                    },
                },
            )
            self.assertEqual(init.status_code, 200)
            self.assertEqual(rpc(init)["result"]["serverInfo"]["name"], "asgi-demo")

            call = c.post(
                base + "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
                },
            )
            self.assertEqual(rpc(call)["result"]["structuredContent"], {"result": 5})

            weather = c.post(
                base + "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "weather", "arguments": {}},
                },
            )
            self.assertEqual(rpc(weather)["result"]["structuredContent"], {"temp": 21.5})

    def test_custom_routes(self):
        base, _ = self._serve(build_server())
        with self._client() as c:
            self.assertEqual(c.get(base + "/health").json(), {"status": "ok"})
            r = c.post(base + "/echo", json={"n": 1})
            self.assertEqual(r.status_code, 201)
            self.assertEqual(r.json(), {"got": {"n": 1}})
            self.assertEqual(c.get(base + "/nope").status_code, 404)

    def test_origin_and_protocol_checks(self):
        base, _ = self._serve(build_server())
        with self._client() as c:
            self.assertEqual(
                c.post(
                    base + "/mcp",
                    headers={"Origin": "http://evil.com"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                ).status_code,
                403,
            )
            self.assertEqual(
                c.post(
                    base + "/mcp",
                    headers={"MCP-Protocol-Version": "1999-01-01"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                ).status_code,
                400,
            )

    def test_auth(self):
        mcp = AnodizeMCP("a", auth=StaticTokenVerifier({"good": {"scopes": ["x"]}}))

        @mcp.tool
        def t() -> str:
            return "ok"

        base, _ = self._serve(mcp)
        with self._client() as c:
            unauth = c.post(base + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            self.assertEqual(unauth.status_code, 401)
            self.assertIn("WWW-Authenticate", unauth.headers)
            ok = c.post(
                base + "/mcp",
                headers={"Authorization": "Bearer good"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            self.assertEqual(ok.status_code, 200)

    def test_stateful_session_and_delete(self):
        base, _ = self._serve(build_server(), stateless=False)
        with self._client() as c:
            init = c.post(
                base + "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {},
                    },
                },
            )
            sid = init.headers["Mcp-Session-Id"]
            call = c.post(
                base + "/mcp",
                headers={"Mcp-Session-Id": sid},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "add", "arguments": {"a": 1, "b": 1}},
                },
            )
            self.assertEqual(call.status_code, 200)
            self.assertEqual(
                c.request("DELETE", base + "/mcp", headers={"Mcp-Session-Id": sid}).status_code, 200
            )

    def test_sse_delivers_progress_notification(self):
        import httpx

        base, _ = self._serve(build_server(), stateless=False)
        with self._client() as c:
            init = c.post(
                base + "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {},
                    },
                },
            )
            sid = init.headers["Mcp-Session-Id"]

        call_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "progressive",
                "arguments": {},
                "_meta": {"progressToken": "p1"},
            },
        }

        # An SSE-accepting POST streams progress ahead of the result on its
        # own response, as FastMCP does.
        with self._client() as c:
            call = c.post(base + "/mcp", headers={"Mcp-Session-Id": sid}, json=call_body)
        self.assertIn("text/event-stream", call.headers["content-type"])
        events = [
            json.loads(line[5:]) for line in call.text.splitlines() if line.startswith("data:")
        ]
        self.assertEqual(events[0].get("method"), "notifications/progress")
        self.assertEqual(events[-1].get("id"), 2)

        # A JSON-only POST routes progress to the standalone GET stream.
        get_events = []

        def read_sse():
            with (
                httpx.Client() as sc,
                sc.stream(
                    "GET",
                    base + "/mcp",
                    headers={"Accept": "text/event-stream", "Mcp-Session-Id": sid},
                    timeout=5,
                ) as resp,
            ):
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        get_events.append(line)
                        return

        reader = threading.Thread(target=read_sse, daemon=True)
        reader.start()
        time.sleep(0.3)  # let the stream attach
        with httpx.Client(headers={"Accept": "application/json"}) as c:
            c.post(base + "/mcp", headers={"Mcp-Session-Id": sid}, json=call_body | {"id": 3})
        reader.join(timeout=4)
        self.assertTrue(
            any("progress" in e for e in get_events), f"no progress event in {get_events}"
        )


class HttpAppAliasTest(unittest.TestCase):
    def test_http_app_matches_asgi_app(self):
        mcp = build_server()
        self.assertTrue(callable(mcp.http_app()))
        self.assertTrue(callable(mcp.http_app(path="/x", stateless=True)))

    def test_http_app_accepts_stateless_http(self):
        mcp = build_server()
        self.assertTrue(callable(mcp.http_app(stateless_http=True)))

    def test_http_app_exposes_lifespan(self):
        import asyncio

        app = build_server().http_app()
        self.assertTrue(hasattr(app, "lifespan"))

        async def drive():
            async with app.lifespan(app):
                pass

        asyncio.run(drive())


class MountRootPathTest(unittest.TestCase):
    """When mounted, root_path carries the prefix; matching must strip it."""

    def _request(self, app, scope, body=b""):
        import asyncio

        async def run():
            sent = []
            received = [
                {"type": "http.request", "body": body, "more_body": False},
            ]

            async def receive():
                return received.pop(0)

            async def send(msg):
                sent.append(msg)

            await app(scope, receive, send)
            return sent

        return asyncio.run(run())

    def test_mounted_request_routes_via_root_path(self):
        app = build_server().http_app(path="/")  # external /mcp/ via Mount("/mcp")
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
            }
        ).encode()
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp/",
            "root_path": "/mcp",
            "headers": [
                (b"content-type", b"application/json"),
                (b"accept", b"application/json, text/event-stream"),
            ],
        }
        sent = self._request(app, scope, body)
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertEqual(start["status"], 200)

    def test_mounted_trailing_slash_redirect_keeps_prefix(self):
        app = build_server().http_app(path="/mcp")
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/mcp/",
            "root_path": "/api",
            "headers": [],
        }
        sent = self._request(app, scope)
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertEqual(start["status"], 307)
        location = dict(start["headers"])[b"location"]
        self.assertEqual(location, b"/api/mcp")


class MiddlewareTest(unittest.TestCase):
    def test_wrapping_order_and_reach_base(self):
        import asyncio

        from anodize_mcp.transports.asgi import _apply_middleware

        order = []

        def mk(name):
            def factory(app):
                async def mw(scope, receive, send):
                    order.append(name)
                    await app(scope, receive, send)

                return mw

            return factory

        reached = []

        async def base(scope, receive, send):
            reached.append(True)

        wrapped = _apply_middleware(base, [mk("A"), mk("B")])
        asyncio.run(wrapped({"type": "http"}, None, None))
        self.assertEqual(order, ["A", "B"])
        self.assertEqual(reached, [True])

    def test_cls_args_kwargs_tuple_form(self):
        from anodize_mcp.transports.asgi import _apply_middleware

        class Dummy:
            def __init__(self, app, **kw):
                self.app = app
                self.kw = kw

        async def base(scope, receive, send):
            pass

        wrapped = _apply_middleware(base, [(Dummy, (), {"foo": 1})])
        self.assertIsInstance(wrapped, Dummy)
        self.assertEqual(wrapped.kw, {"foo": 1})


@unittest.skipUnless(_HAS_UVICORN, "uvicorn required")
class RunHttpTlsTest(unittest.TestCase):
    def test_uvicorn_config_reaches_config(self):
        import uvicorn

        captured = {}

        class FakeConfig:
            def __init__(self, app, **kwargs):
                captured.update(kwargs)

        class FakeServer:
            def __init__(self, config):
                pass

            def run(self):
                pass

        with (
            unittest.mock.patch.object(uvicorn, "Config", FakeConfig),
            unittest.mock.patch.object(uvicorn, "Server", FakeServer),
        ):
            build_server().run_http(
                port=free_port(),
                uvicorn_config={"ssl_keyfile": "key.pem", "ssl_certfile": "cert.pem"},
            )

        self.assertEqual(captured.get("ssl_keyfile"), "key.pem")
        self.assertEqual(captured.get("ssl_certfile"), "cert.pem")


class RunHttpFallbackGuardTest(unittest.TestCase):
    def test_uvicorn_config_without_uvicorn_raises(self):
        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name, *args, **kwargs):
            if name == "uvicorn":
                return None
            return real_find_spec(name, *args, **kwargs)

        for kwargs in (
            {"uvicorn_config": {"ssl_keyfile": "key.pem"}},
            {"middleware": [lambda a: a]},
        ):
            with (
                unittest.mock.patch.object(importlib.util, "find_spec", fake_find_spec),
                self.assertRaises(RuntimeError),
            ):
                build_server().run_http(port=free_port(), **kwargs)


if __name__ == "__main__":
    unittest.main()
