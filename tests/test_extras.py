import contextlib
import json
import threading
import unittest
import urllib.request
import warnings
from http.server import ThreadingHTTPServer

from anodize_mcp import AnodizeMCP, Context, Middleware, Response
from anodize_mcp.transports.http import _make_handler, _Manager
from test_features import init_session, request


class LifespanTest(unittest.TestCase):
    def test_sync_lifespan_exposed_to_handler(self):
        entered, exited = [], []

        @contextlib.contextmanager
        def lifespan(server):
            entered.append(server.name)
            yield {"db": "pool"}
            exited.append(True)

        mcp = AnodizeMCP("ls", lifespan=lifespan)

        @mcp.tool
        def use(ctx: Context) -> str:
            return ctx.lifespan_context["db"]

        mcp._enter_lifespan()
        self.assertEqual(entered, ["ls"])
        session, _ = init_session(mcp)
        result = request(mcp, session, "tools/call", {"name": "use", "arguments": {}})
        self.assertEqual(result["result"]["content"][0]["text"], "pool")
        mcp._exit_lifespan()
        self.assertEqual(exited, [True])

    def test_async_lifespan(self):
        @contextlib.asynccontextmanager
        async def lifespan(server):
            yield {"async": True}

        mcp = AnodizeMCP("als", lifespan=lifespan)
        mcp._enter_lifespan()
        self.assertEqual(mcp._lifespan_state, {"async": True})
        mcp._exit_lifespan()


class MiddlewareTest(unittest.TestCase):
    def test_hooks_run_in_order(self):
        seen = []

        class MW(Middleware):
            def __init__(self, tag):
                self.tag = tag

            async def on_message(self, ctx, call_next):
                seen.append((self.tag, "msg", ctx.method))
                return await call_next(ctx)

            async def on_call_tool(self, ctx, call_next):
                seen.append((self.tag, "tool", ctx.method))
                return await call_next(ctx)

        mcp = AnodizeMCP("mw")
        mcp.add_middleware(MW("a"))
        mcp.add_middleware(MW("b"))

        @mcp.tool
        def echo(text: str) -> str:
            return text

        session, _ = init_session(mcp)
        seen.clear()  # drop the initialize message, which also flows through
        result = request(mcp, session, "tools/call", {"name": "echo", "arguments": {"text": "x"}})
        self.assertEqual(result["result"]["content"][0]["text"], "x")
        # Per-middleware composition, as FastMCP orders it: every hook of the
        # first middleware wraps every hook of the second.
        self.assertEqual(
            seen,
            [
                ("a", "msg", "tools/call"),
                ("a", "tool", "tools/call"),
                ("b", "msg", "tools/call"),
                ("b", "tool", "tools/call"),
            ],
        )

    def test_fastmcp_middleware_shapes(self):
        observed = {}

        class Shapes(Middleware):
            async def on_call_tool(self, ctx, call_next):
                observed["name"] = ctx.message.name
                observed["has_timestamp"] = ctx.timestamp is not None
                result = await call_next(ctx.copy())
                observed["result_text"] = result.content[0].text
                return result

            async def on_list_tools(self, ctx, call_next):
                tools = await call_next(ctx)
                return [t for t in tools if t.name != "hidden"]

        mcp = AnodizeMCP("shape")
        mcp.add_middleware(Shapes())

        @mcp.tool
        def echo(text: str) -> str:
            return text

        @mcp.tool
        def hidden() -> str:
            return "h"

        session, _ = init_session(mcp)
        listing = request(mcp, session, "tools/list", {})
        self.assertEqual([t["name"] for t in listing["result"]["tools"]], ["echo"])
        result = request(mcp, session, "tools/call", {"name": "echo", "arguments": {"text": "x"}})
        self.assertEqual(result["result"]["content"][0]["text"], "x")
        self.assertEqual(observed["name"], "echo")
        self.assertTrue(observed["has_timestamp"])
        self.assertEqual(observed["result_text"], "x")

    def test_middleware_can_short_circuit(self):
        class Block(Middleware):
            async def on_call_tool(self, ctx, call_next):
                return {"content": [{"type": "text", "text": "blocked"}], "isError": True}

        mcp = AnodizeMCP("mw")
        mcp.add_middleware(Block())

        @mcp.tool
        def echo(text: str) -> str:
            return text

        session, _ = init_session(mcp)
        result = request(mcp, session, "tools/call", {"name": "echo", "arguments": {"text": "x"}})
        self.assertTrue(result["result"]["isError"])
        self.assertEqual(result["result"]["content"][0]["text"], "blocked")


class IntrospectionTest(unittest.TestCase):
    def _server(self):
        mcp = AnodizeMCP("i")

        @mcp.tool
        def add(a: int, b: int) -> int:
            return a + b

        @mcp.prompt
        def ask(topic: str) -> str:
            return f"about {topic}"

        @mcp.resource("data://x")
        def res() -> str:
            return "v"

        return mcp

    def test_list_and_get(self):
        mcp = self._server()
        self.assertEqual([t["name"] for t in mcp.list_tools()], ["add"])
        self.assertEqual([r["uri"] for r in mcp.list_resources()], ["data://x"])
        self.assertEqual([p["name"] for p in mcp.list_prompts()], ["ask"])
        self.assertEqual(mcp.get_tool("add").name, "add")
        self.assertIsNone(mcp.get_tool("nope"))

    def test_call_tool_and_render_prompt_in_process(self):
        mcp = self._server()
        self.assertEqual(mcp.call_tool("add", {"a": 2, "b": 3})["structuredContent"], {"result": 5})
        rendered = mcp.render_prompt("ask", {"topic": "z"})
        self.assertEqual(rendered["messages"][0]["content"]["text"], "about z")

    def test_disable_enable(self):
        mcp = self._server()
        session, _ = init_session(mcp)
        mcp.disable_tool("add")
        self.assertEqual(request(mcp, session, "tools/list", {})["result"]["tools"], [])
        resp = request(mcp, session, "tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}})
        self.assertTrue(resp["result"]["isError"])
        mcp.enable_tool("add")
        self.assertEqual(len(request(mcp, session, "tools/list", {})["result"]["tools"]), 1)


class ConstructorFlagTest(unittest.TestCase):
    def test_name_defaults(self):
        # FastMCP allows FastMCP() with no name.
        self.assertEqual(AnodizeMCP().name, "AnodizeMCP")

    def test_on_duplicate_error(self):
        mcp = AnodizeMCP("d", on_duplicate="error")
        mcp.tool(name="x")(lambda: "1")
        with self.assertRaises(ValueError):
            mcp.tool(name="x")(lambda: "2")

    def test_on_duplicate_warn(self):
        mcp = AnodizeMCP("d", on_duplicate="warn")
        mcp.tool(name="x")(lambda: "1")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            mcp.tool(name="x")(lambda: "2")
        self.assertTrue(any("already registered" in str(w.message) for w in caught))

    def test_mask_error_details(self):
        mcp = AnodizeMCP("m", mask_error_details=True)

        @mcp.tool
        def crash() -> str:
            raise RuntimeError("secret detail")

        session, _ = init_session(mcp)
        result = request(mcp, session, "tools/call", {"name": "crash", "arguments": {}})
        self.assertEqual(result["result"]["content"][0]["text"], "Error calling tool 'crash'")

    def test_metadata_in_server_info(self):
        mcp = AnodizeMCP("m", icons=[{"src": "a.png"}], website_url="https://example.com")
        session = mcp.new_session()
        info = request(
            mcp,
            session,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
        )["result"]["serverInfo"]
        self.assertEqual(info["icons"], [{"src": "a.png"}])
        self.assertEqual(info["websiteUrl"], "https://example.com")


class ContextExtrasTest(unittest.TestCase):
    def test_send_notification_and_request_context(self):
        mcp = AnodizeMCP("c", lifespan=lambda s: contextlib.nullcontext({"k": "v"}))
        mcp._enter_lifespan()

        captured = []

        @mcp.tool
        def notify(ctx: Context) -> str:
            ctx.send_notification("notifications/custom", {"hello": "world"})
            rc = ctx.request_context
            return f"{rc.lifespan_context['k']}/{ctx.transport}/{ctx.fastmcp.name}"

        session = mcp.new_session(send=captured.append)
        session.transport = "stdio"
        result = request(mcp, session, "tools/call", {"name": "notify", "arguments": {}})
        self.assertEqual(result["result"]["content"][0]["text"], "v/stdio/c")
        self.assertTrue(any(m.get("method") == "notifications/custom" for m in captured))


class CustomRouteHttpTest(unittest.TestCase):
    def setUp(self):
        mcp = AnodizeMCP("r")

        @mcp.tool
        def echo(text: str) -> str:
            return text

        @mcp.custom_route("/health", methods=["GET"])
        def health(request):
            return {"status": "ok"}

        @mcp.custom_route("/echo", methods=["POST"])
        def echo_route(request):
            return Response(201, {"got": request.json()})

        self.manager = _Manager(server=mcp, endpoint="/mcp", allowed_origins=None, stateless=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.manager))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_health_route(self):
        r = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5)
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.read()), {"status": "ok"})

    def test_post_route_with_body(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/echo",
            data=json.dumps({"n": 1}).encode(),
            method="POST",
        )
        r = urllib.request.urlopen(req, timeout=5)
        self.assertEqual(r.status, 201)
        self.assertEqual(json.loads(r.read()), {"got": {"n": 1}})

    def test_unknown_route_404(self):
        import urllib.error

        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/nope", timeout=5)
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
