import unittest
from dataclasses import dataclass

from anodize_mcp import Anodize, Context, TextContent, ToolError


def build_server() -> Anodize:
    mcp = Anodize("test", version="9.9.9", instructions="be nice")

    @mcp.tool
    def add(a: int, b: int) -> int:
        "Add two integers."
        return a + b

    @dataclass
    class Out:
        value: int

    @mcp.tool
    def doubled(n: int) -> Out:
        return Out(n * 2)

    @mcp.tool
    def boom() -> str:
        raise ToolError("kaboom")

    @mcp.tool
    def crash() -> str:
        raise RuntimeError("unexpected")

    @mcp.tool
    def logs(ctx: Context) -> str:
        ctx.info("hello from tool")
        return "done"

    @mcp.tool
    def blocks() -> list:
        # SDK-style construction with an explicit type= discriminator.
        return [TextContent(type="text", text="one"), TextContent(text="two")]

    @mcp.resource("data://static")
    def static_res() -> str:
        return "static-body"

    @mcp.resource("item://{id}")
    def item(id: str) -> str:
        return f"item-{id}"

    @mcp.prompt
    def summarize(text: str) -> str:
        "Summarize text."
        return f"Summarize: {text}"

    return mcp


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.mcp = build_server()
        self.session = self.mcp.new_session(send=self._collect)
        self.notifications = []

    def _collect(self, message):
        self.notifications.append(message)

    def call(self, method, params=None, _id=1):
        msg = {"jsonrpc": "2.0", "id": _id, "method": method}
        if params is not None:
            msg["params"] = params
        return self.mcp.handle_message(msg, self.session)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        return self.mcp.handle_message(msg, self.session)

    # -- lifecycle --------------------------------------------------------

    def test_initialize(self):
        result = self.call(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c"}},
        )["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"], {"name": "test", "version": "9.9.9"})
        self.assertEqual(result["instructions"], "be nice")
        self.assertIn("tools", result["capabilities"])

    def test_protocol_version_fallback(self):
        result = self.call(
            "initialize",
            {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {}},
        )["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")

    def test_initialized_notification_returns_none(self):
        self.assertIsNone(self.notify("notifications/initialized"))
        self.assertTrue(self.session.initialized)

    def test_ping(self):
        self.assertEqual(self.call("ping")["result"], {})

    # -- tools ------------------------------------------------------------

    def test_tools_list(self):
        tools = {t["name"]: t for t in self.call("tools/list")["result"]["tools"]}
        self.assertEqual(tools["add"]["description"], "Add two integers.")
        # doubled returns a dataclass (object schema); add returns int (wrapped).
        self.assertEqual(tools["doubled"]["outputSchema"]["type"], "object")
        self.assertIn("result", tools["add"]["outputSchema"]["properties"])

    def test_tool_call(self):
        result = self.call("tools/call", {"name": "add", "arguments": {"a": 2, "b": 5}})["result"]
        self.assertEqual(result["content"][0]["text"], "7")
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], {"result": 7})

    def test_tool_structured(self):
        result = self.call("tools/call", {"name": "doubled", "arguments": {"n": 4}})["result"]
        self.assertEqual(result["structuredContent"], {"value": 8})

    def test_unexpected_argument_rejected(self):
        resp = self.call("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2, "x": 9}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_non_json_returns_are_serializable(self):
        import base64
        import datetime
        import json

        from anodize_mcp.protocol import json_default

        mcp = Anodize("ser")

        @mcp.tool
        def raw() -> bytes:
            return b"\x89PNG"

        @dataclass
        class Ev:
            when: datetime.datetime

        @mcp.tool
        def ev() -> Ev:
            return Ev(datetime.datetime(2025, 1, 1, 12, 0, 0))

        session = mcp.new_session()
        for name in ("raw", "ev"):
            resp = mcp.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {}},
                },
                session,
            )
            # The whole response must survive the encoder the transports use.
            json.dumps(resp, default=json_default)
            self.assertFalse(resp["result"]["isError"])
        bytes_resp = mcp.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "raw", "arguments": {}},
            },
            session,
        )
        expected = base64.b64encode(b"\x89PNG").decode("ascii")
        self.assertEqual(bytes_resp["result"]["structuredContent"], {"result": expected})

    def test_tool_returns_content_blocks(self):
        result = self.call("tools/call", {"name": "blocks", "arguments": {}})["result"]
        self.assertEqual(
            result["content"],
            [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
        )

    def test_tool_invalid_args(self):
        resp = self.call("tools/call", {"name": "add", "arguments": {"a": "x", "b": 1}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_tool_error_is_result(self):
        result = self.call("tools/call", {"name": "boom", "arguments": {}})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "kaboom")

    def test_tool_unexpected_exception_masked(self):
        result = self.call("tools/call", {"name": "crash", "arguments": {}})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("unexpected", result["content"][0]["text"])

    def test_unknown_tool(self):
        resp = self.call("tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(resp["result"]["isError"])
        self.assertEqual(resp["result"]["content"][0]["text"], "Unknown tool: 'nope'")

    def test_context_logging(self):
        self.call("tools/call", {"name": "logs", "arguments": {}})
        levels = [
            n["params"]["level"]
            for n in self.notifications
            if n.get("method") == "notifications/message"
        ]
        self.assertIn("info", levels)

    def test_log_level_filtering(self):
        self.call("logging/setLevel", {"level": "error"})
        self.notifications.clear()
        self.call("tools/call", {"name": "logs", "arguments": {}})
        msgs = [n for n in self.notifications if n.get("method") == "notifications/message"]
        self.assertEqual(msgs, [])

    # -- resources --------------------------------------------------------

    def test_resources_list(self):
        uris = {r["uri"] for r in self.call("resources/list")["result"]["resources"]}
        self.assertEqual(uris, {"data://static"})

    def test_resource_templates_list(self):
        templates = self.call("resources/templates/list")["result"]["resourceTemplates"]
        self.assertEqual(templates[0]["uriTemplate"], "item://{id}")

    def test_read_static(self):
        contents = self.call("resources/read", {"uri": "data://static"})["result"]["contents"]
        self.assertEqual(contents[0]["text"], "static-body")

    def test_read_template(self):
        contents = self.call("resources/read", {"uri": "item://42"})["result"]["contents"]
        self.assertEqual(contents[0]["text"], "item-42")

    def test_read_missing(self):
        resp = self.call("resources/read", {"uri": "item://"})
        # "item://" has an empty id and does not match the [^/]+ template.
        self.assertEqual(resp["error"]["code"], -32002)

    # -- prompts ----------------------------------------------------------

    def test_prompts_list(self):
        prompt = self.call("prompts/list")["result"]["prompts"][0]
        self.assertEqual(prompt["name"], "summarize")
        self.assertEqual(prompt["arguments"][0]["name"], "text")
        self.assertTrue(prompt["arguments"][0]["required"])

    def test_prompt_get(self):
        result = self.call("prompts/get", {"name": "summarize", "arguments": {"text": "hi"}})[
            "result"
        ]
        self.assertEqual(result["messages"][0]["content"]["text"], "Summarize: hi")
        self.assertEqual(result["description"], "Summarize text.")

    # -- malformed --------------------------------------------------------

    def test_bad_jsonrpc(self):
        resp = self.mcp.handle_message({"id": 1, "method": "ping"}, self.session)
        self.assertEqual(resp["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
