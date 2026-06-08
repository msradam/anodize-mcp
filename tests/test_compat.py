"""FastMCP source-compatibility: the async `await ctx.*` style must run here."""

import queue
import threading
import unittest

import anodize_mcp
from anodize_mcp import Context, FastMCP
from test_features import init_session, request


class AliasTest(unittest.TestCase):
    def test_fastmcp_alias(self):
        self.assertIs(FastMCP, anodize_mcp.Anodize)

    def test_tags_kwarg_accepted(self):
        mcp = FastMCP("compat")

        @mcp.tool(tags={"infra", "ops"})
        def ping() -> str:
            return "pong"

        @mcp.resource("r://x", tags={"a"})
        def res() -> str:
            return "x"

        self.assertIn("ping", mcp._tools)

    def test_programmatic_add(self):
        mcp = FastMCP("compat")

        def hello(name: str) -> str:
            return f"hi {name}"

        mcp.add_tool(hello, name="hello")
        self.assertIn("hello", mcp._tools)

    def test_build_with_alias(self):
        mcp = FastMCP("compat", instructions="hi")

        @mcp.tool
        def add(a: int, b: int) -> int:
            return a + b

        session, _ = init_session(mcp)
        result = request(mcp, session, "tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}})
        self.assertEqual(result["result"]["content"][0]["text"], "3")


class AwaitableContextTest(unittest.TestCase):
    """Handlers written in FastMCP's async style run unchanged."""

    def test_async_tool_awaits_logging_and_progress(self):
        mcp = FastMCP("compat")

        @mcp.tool
        async def work(ctx: Context) -> str:
            await ctx.info("starting")
            await ctx.report_progress(1, total=1)
            await ctx.set_state("k", "v")
            value = await ctx.get_state("k")
            return f"done:{value}"

        session, _ = init_session(mcp)
        result = request(mcp, session, "tools/call", {"name": "work", "arguments": {}, "_meta": {}})
        self.assertEqual(result["result"]["content"][0]["text"], "done:v")

    def test_async_tool_awaits_sample(self):
        mcp = FastMCP("compat")

        @mcp.tool
        async def review(code: str, ctx: Context) -> str:
            result = await ctx.sample(f"review {code}", system_prompt="terse")
            return result.text or ""

        outgoing: queue.Queue = queue.Queue()
        session = mcp.new_session(send=outgoing.put)
        request(
            mcp,
            session,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {"sampling": {}}, "clientInfo": {}},
        )

        holder = {}

        def run():
            holder["resp"] = request(
                mcp, session, "tools/call", {"name": "review", "arguments": {"code": "x"}}, _id=9
            )

        thread = threading.Thread(target=run)
        thread.start()
        outbound = outgoing.get(timeout=2)
        self.assertEqual(outbound["method"], "sampling/createMessage")
        session.resolve_response(
            {
                "jsonrpc": "2.0",
                "id": outbound["id"],
                "result": {"role": "assistant", "content": {"type": "text", "text": "ok"}},
            }
        )
        thread.join(timeout=2)
        self.assertEqual(holder["resp"]["result"]["content"][0]["text"], "ok")

    def test_sync_and_async_yield_same_value(self):
        mcp = FastMCP("compat")
        session, _ = init_session(mcp)
        ctx = Context(session, mcp)

        # Synchronous use: the Deferred proxies attribute access.
        self.assertIsNone(ctx.info("x").unwrap())
        # Stored then read back synchronously.
        ctx.set_state("a", 1)
        self.assertEqual(ctx.get_state("a"), 1)

    def test_log_message_first_signature(self):
        # FastMCP calls ctx.log(message, level=...); ours must accept that order.
        notes = []
        mcp = FastMCP("compat")
        session = mcp.new_session(send=notes.append)
        ctx = Context(session, mcp)
        ctx.log("hello", level="warning")
        levels = [n["params"]["level"] for n in notes if n.get("method") == "notifications/message"]
        self.assertEqual(levels, ["warning"])

    def test_context_introspection(self):
        mcp = FastMCP("compat")

        @mcp.resource("data://x")
        def res() -> str:
            return "v"

        @mcp.prompt
        def greet(name: str) -> str:
            return f"hi {name}"

        session, _ = init_session(mcp)
        ctx = Context(session, mcp)
        self.assertEqual([r["uri"] for r in ctx.list_resources()], ["data://x"])
        self.assertEqual([p["name"] for p in ctx.list_prompts()], ["greet"])
        rendered = ctx.get_prompt("greet", {"name": "Ada"})
        self.assertEqual(rendered["messages"][0]["content"]["text"], "hi Ada")


if __name__ == "__main__":
    unittest.main()
