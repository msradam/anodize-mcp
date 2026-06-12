import queue
import threading
import unittest
from dataclasses import dataclass

from anodize_mcp import Anodize, CompletionResult, Context
from anodize_mcp.exceptions import McpError


def init_session(mcp, capabilities=None):
    outgoing = queue.Queue()
    session = mcp.new_session(send=outgoing.put)
    mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": capabilities or {},
                "clientInfo": {"name": "test"},
            },
        },
        session,
    )
    session.initialized = True
    return session, outgoing


def request(mcp, session, method, params=None, _id=1):
    msg = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        msg["params"] = params
    return mcp.handle_message(msg, session)


class ServerDefaultsTest(unittest.TestCase):
    def test_no_pagination_by_default(self):
        mcp = Anodize("p")
        for i in range(120):
            mcp.add_tool(lambda: "x", name=f"t{i}")
        session, _ = init_session(mcp)
        result = request(mcp, session, "tools/list")["result"]
        self.assertEqual(len(result["tools"]), 120)
        self.assertNotIn("nextCursor", result)

    def test_capabilities_always_advertised(self):
        mcp = Anodize("empty")
        session, _ = init_session(mcp)
        outgoing = queue.Queue()
        fresh = mcp.new_session(send=outgoing.put)
        resp = mcp.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
            },
            fresh,
        )
        caps = resp["result"]["capabilities"]
        for key in ("tools", "resources", "prompts", "logging"):
            self.assertIn(key, caps)

    def test_unknown_tool_is_tool_error_result(self):
        mcp = Anodize("u")
        session, _ = init_session(mcp)
        resp = request(mcp, session, "tools/call", {"name": "nope", "arguments": {}})
        result = resp["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "Unknown tool: 'nope'")

    def test_error_text_matches_fastmcp(self):
        for masked, expected in (
            (True, "Error calling tool 'boom'"),
            (False, "Error calling tool 'boom': bad"),
        ):
            mcp = Anodize("e", mask_error_details=masked)

            @mcp.tool
            def boom() -> str:
                raise ValueError("bad")

            session, _ = init_session(mcp)
            resp = request(mcp, session, "tools/call", {"name": "boom", "arguments": {}})
            self.assertEqual(resp["result"]["content"][0]["text"], expected)

    def test_session_id_is_a_string(self):
        mcp = Anodize("s")
        session = mcp.new_session(send=lambda m: None)
        self.assertIsInstance(session.session_id, str)
        self.assertTrue(session.session_id)

    def test_debug_logs_forwarded_by_default(self):
        mcp = Anodize("l")

        @mcp.tool
        def noisy(ctx: Context) -> str:
            ctx.debug("dbg")
            return "ok"

        session, outgoing = init_session(mcp)
        request(mcp, session, "tools/call", {"name": "noisy", "arguments": {}})
        notifications = []
        while not outgoing.empty():
            notifications.append(outgoing.get())
        levels = [
            n["params"]["level"]
            for n in notifications
            if n.get("method") == "notifications/message"
        ]
        self.assertIn("debug", levels)


class PaginationTest(unittest.TestCase):
    def test_cursor_pages(self):
        mcp = Anodize("p", page_size=2)
        for i in range(5):
            mcp.tool(name=f"t{i}")(lambda: "x")
        session, _ = init_session(mcp)

        page1 = request(mcp, session, "tools/list", {})["result"]
        self.assertEqual([t["name"] for t in page1["tools"]], ["t0", "t1"])
        self.assertIn("nextCursor", page1)

        page2 = request(mcp, session, "tools/list", {"cursor": page1["nextCursor"]})["result"]
        self.assertEqual([t["name"] for t in page2["tools"]], ["t2", "t3"])

        page3 = request(mcp, session, "tools/list", {"cursor": page2["nextCursor"]})["result"]
        self.assertEqual([t["name"] for t in page3["tools"]], ["t4"])
        self.assertNotIn("nextCursor", page3)

    def test_invalid_cursor(self):
        mcp = Anodize("p", page_size=2)
        mcp.tool(name="a")(lambda: "x")
        session, _ = init_session(mcp)
        resp = request(mcp, session, "tools/list", {"cursor": "@@@not-base64@@@"})
        self.assertEqual(resp["error"]["code"], -32602)


class CompletionTest(unittest.TestCase):
    def _server(self):
        mcp = Anodize("c")

        @mcp.prompt
        def greet(language: str) -> str:
            return language

        @mcp.complete_prompt("greet")
        def complete_two(argument, value):
            return [w for w in ("english", "spanish", "swedish") if w.startswith(value)]

        @mcp.resource("doc://{name}")
        def doc(name: str) -> str:
            return name

        @mcp.complete_resource("doc://{name}")
        def complete_three(argument, value, context):
            return CompletionResult(values=["readme", "license"], total=2, has_more=False)

        return mcp

    def test_prompt_completion(self):
        mcp = self._server()
        session, _ = init_session(mcp)
        resp = request(
            mcp,
            session,
            "completion/complete",
            {
                "ref": {"type": "ref/prompt", "name": "greet"},
                "argument": {"name": "language", "value": "s"},
            },
        )["result"]
        self.assertEqual(resp["completion"]["values"], ["spanish", "swedish"])
        self.assertFalse(resp["completion"]["hasMore"])

    def test_resource_completion_with_context_arg(self):
        mcp = self._server()
        session, _ = init_session(mcp)
        resp = request(
            mcp,
            session,
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "doc://{name}"},
                "argument": {"name": "name", "value": ""},
                "context": {"arguments": {}},
            },
        )["result"]
        self.assertEqual(resp["completion"]["values"], ["readme", "license"])
        self.assertEqual(resp["completion"]["total"], 2)

    def test_truncation_sets_total_and_hasmore(self):
        mcp = Anodize("c")

        @mcp.prompt
        def greet(language: str) -> str:
            return language

        @mcp.complete_prompt("greet")
        def complete_many(argument, value):
            return [f"v{i}" for i in range(250)]

        session, _ = init_session(mcp)
        comp = request(
            mcp,
            session,
            "completion/complete",
            {
                "ref": {"type": "ref/prompt", "name": "greet"},
                "argument": {"name": "language", "value": ""},
            },
        )["result"]["completion"]
        self.assertEqual(len(comp["values"]), 100)
        self.assertTrue(comp["hasMore"])
        self.assertEqual(comp["total"], 250)

    def test_unknown_ref_empty(self):
        mcp = self._server()
        session, _ = init_session(mcp)
        resp = request(
            mcp,
            session,
            "completion/complete",
            {"ref": {"type": "ref/prompt", "name": "nope"}, "argument": {"name": "x", "value": ""}},
        )["result"]
        self.assertEqual(resp["completion"]["values"], [])

    def test_completions_capability_declared(self):
        mcp = self._server()
        session, _ = init_session(mcp)
        caps = request(
            mcp,
            session,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
        )["result"]["capabilities"]
        self.assertIn("completions", caps)


class SubscriptionTest(unittest.TestCase):
    def test_subscribe_and_notify(self):
        mcp = Anodize("s")

        @mcp.resource("data://x")
        def x() -> str:
            return "v"

        session, outgoing = init_session(mcp)
        request(mcp, session, "resources/subscribe", {"uri": "data://x"})
        self.assertIn("data://x", session.subscriptions)

        mcp.notify_resource_updated("data://x")
        note = outgoing.get(timeout=1)
        self.assertEqual(note["method"], "notifications/resources/updated")
        self.assertEqual(note["params"]["uri"], "data://x")

    def test_unsubscribe_stops_notifications(self):
        mcp = Anodize("s")

        @mcp.resource("data://x")
        def x() -> str:
            return "v"

        session, outgoing = init_session(mcp)
        request(mcp, session, "resources/subscribe", {"uri": "data://x"})
        request(mcp, session, "resources/unsubscribe", {"uri": "data://x"})
        mcp.notify_resource_updated("data://x")
        self.assertTrue(outgoing.empty())


class ListChangedTest(unittest.TestCase):
    def test_remove_tool_emits_notification(self):
        mcp = Anodize("l")
        mcp.tool(name="gone")(lambda: "x")
        session, outgoing = init_session(mcp)
        mcp.remove_tool("gone")
        note = outgoing.get(timeout=1)
        self.assertEqual(note["method"], "notifications/tools/list_changed")

    def test_capabilities_advertise_listchanged(self):
        mcp = Anodize("l")
        mcp.tool(name="a")(lambda: "x")

        @mcp.resource("r://x")
        def r() -> str:
            return "x"

        session, _ = init_session(mcp)
        caps = mcp._capabilities()
        self.assertTrue(caps["tools"]["listChanged"])
        self.assertTrue(caps["resources"]["subscribe"])
        self.assertTrue(caps["resources"]["listChanged"])


class ResourceAnnotationTest(unittest.TestCase):
    def test_annotations_and_size_in_list(self):
        mcp = Anodize("a")

        @mcp.resource(
            "data://x",
            mime_type="text/plain",
            size=123,
            annotations={"audience": ["user"], "priority": 0.8},
        )
        def x() -> str:
            return "v"

        session, _ = init_session(mcp)
        res = request(mcp, session, "resources/list", {})["result"]["resources"][0]
        self.assertEqual(res["size"], 123)
        self.assertEqual(res["annotations"]["priority"], 0.8)


class BidirectionalTest(unittest.TestCase):
    def _run_blocking_call(self, mcp, session, call_params):
        holder = {}

        def run():
            holder["resp"] = request(mcp, session, "tools/call", call_params, _id=99)

        thread = threading.Thread(target=run)
        thread.start()
        return thread, holder

    def test_sampling_roundtrip(self):
        mcp = Anodize("b")

        @mcp.tool
        def review(code: str, ctx: Context) -> str:
            result = ctx.sample(f"review {code}", system_prompt="terse")
            return f"got: {result.text} from {result.model}"

        session, outgoing = init_session(mcp, capabilities={"sampling": {}})
        thread, holder = self._run_blocking_call(
            mcp, session, {"name": "review", "arguments": {"code": "x"}}
        )

        outbound = outgoing.get(timeout=2)
        self.assertEqual(outbound["method"], "sampling/createMessage")
        self.assertEqual(outbound["params"]["systemPrompt"], "terse")
        session.resolve_response(
            {
                "jsonrpc": "2.0",
                "id": outbound["id"],
                "result": {
                    "role": "assistant",
                    "content": {"type": "text", "text": "ok"},
                    "model": "m1",
                },
            }
        )
        thread.join(timeout=2)
        self.assertEqual(holder["resp"]["result"]["content"][0]["text"], "got: ok from m1")

    def test_elicitation_roundtrip(self):
        mcp = Anodize("b")

        @dataclass
        class NameForm:
            name: str

        @mcp.tool
        def ask(ctx: Context) -> str:
            res = ctx.elicit("name?", NameForm)
            return f"{res.action}:{res.data.name}"

        session, outgoing = init_session(mcp, capabilities={"elicitation": {}})
        thread, holder = self._run_blocking_call(mcp, session, {"name": "ask", "arguments": {}})

        outbound = outgoing.get(timeout=2)
        self.assertEqual(outbound["method"], "elicitation/create")
        self.assertEqual(outbound["params"]["requestedSchema"]["type"], "object")
        session.resolve_response(
            {
                "jsonrpc": "2.0",
                "id": outbound["id"],
                "result": {"action": "accept", "content": {"name": "Ada"}},
            }
        )
        thread.join(timeout=2)
        self.assertEqual(holder["resp"]["result"]["content"][0]["text"], "accept:Ada")

    def test_list_roots(self):
        mcp = Anodize("b")

        @mcp.tool
        def where(ctx: Context) -> str:
            roots = ctx.list_roots()
            return ",".join(r.uri for r in roots)

        session, outgoing = init_session(mcp, capabilities={"roots": {}})
        thread, holder = self._run_blocking_call(mcp, session, {"name": "where", "arguments": {}})

        outbound = outgoing.get(timeout=2)
        self.assertEqual(outbound["method"], "roots/list")
        session.resolve_response(
            {
                "jsonrpc": "2.0",
                "id": outbound["id"],
                "result": {"roots": [{"uri": "file:///a"}, {"uri": "file:///b"}]},
            }
        )
        thread.join(timeout=2)
        self.assertEqual(holder["resp"]["result"]["content"][0]["text"], "file:///a,file:///b")

    def test_missing_capability_errors(self):
        mcp = Anodize("b")

        @mcp.tool
        def review(ctx: Context) -> str:
            return ctx.sample("hi").text or ""

        session, _ = init_session(mcp, capabilities={})  # no sampling
        resp = request(mcp, session, "tools/call", {"name": "review", "arguments": {}})
        self.assertEqual(resp["error"]["code"], -32600)  # INVALID_REQUEST

    def test_mcp_error_accepts_error_data(self):
        from anodize_mcp.exceptions import ErrorData

        exc = McpError(ErrorData(code=-32602, message="bad", data={"k": 1}))
        self.assertEqual(exc.code, -32602)
        self.assertEqual(exc.message, "bad")
        self.assertEqual(exc.data, {"k": 1})
        plain = McpError("oops", code=-32000)
        self.assertEqual((plain.code, plain.message), (-32000, "oops"))

    def test_request_timeout(self):
        mcp = Anodize("b")
        with self.assertRaises(McpError) as cm:
            mcp.new_session(send=lambda m: None).send_request("roots/list", {}, timeout=0.2)
        self.assertEqual(cm.exception.data, {"reason": "timeout", "method": "roots/list"})


if __name__ == "__main__":
    unittest.main()
