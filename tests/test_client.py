import os
import sys
import unittest
from dataclasses import dataclass

from anodize_mcp import AnodizeMCP, Client, ClientError, Context


def build_server(page_size: int = 100) -> AnodizeMCP:
    mcp = AnodizeMCP("client-demo", page_size=page_size)

    @mcp.tool
    def add(a: int, b: int) -> int:
        "Add two integers."
        return a + b

    @dataclass
    class Weather:
        temp: float
        sky: str

    @mcp.tool
    def weather(city: str) -> Weather:
        return Weather(21.5, "clear")

    @mcp.tool
    def boom() -> str:
        raise ValueError("kaboom")

    @mcp.tool
    async def review(code: str, ctx: Context) -> str:
        result = await ctx.sample(f"review {code}", system_prompt="terse")
        return f"llm:{result.text}"

    @mcp.tool
    async def ask(ctx: Context) -> str:
        res = await ctx.elicit(
            "name?", {"type": "object", "properties": {"name": {"type": "string"}}}
        )
        return f"{res.action}:{res.data}"

    @mcp.tool
    async def where(ctx: Context) -> str:
        roots = await ctx.list_roots()
        return ",".join(r.uri for r in roots)

    @mcp.resource("config://app")
    def config() -> str:
        return '{"v": 1}'

    @mcp.resource("file://{path:path}")
    def readf(path: str) -> str:
        return f"contents of {path}"

    @mcp.prompt
    def greet(language: str) -> str:
        return f"hi in {language}"

    @mcp.complete_prompt("greet")
    def complete(argument, value):
        return [x for x in ("english", "spanish") if x.startswith(value)]

    return mcp


class InMemoryClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_and_tools(self):
        async with Client(build_server()) as c:
            self.assertTrue(await c.ping())
            self.assertEqual(c.initialize_result["serverInfo"]["name"], "client-demo")
            names = sorted(t["name"] for t in await c.list_tools())
            self.assertIn("add", names)

            r = await c.call_tool("add", {"a": 2, "b": 3})
            self.assertEqual(r.text, "5")
            self.assertEqual(r.data, {"result": 5})
            self.assertFalse(r.is_error)

    async def test_structured_output(self):
        async with Client(build_server()) as c:
            r = await c.call_tool("weather", {"city": "NYC"})
            self.assertEqual(r.data, {"temp": 21.5, "sky": "clear"})

    async def test_tool_error_and_unknown(self):
        async with Client(build_server()) as c:
            r = await c.call_tool("boom", {}, raise_on_error=False)
            self.assertTrue(r.is_error)
            with self.assertRaises(ClientError):
                await c.call_tool("boom", {})
            with self.assertRaises(ClientError):
                await c.call_tool("nope", {})
            with self.assertRaises(ClientError):
                await c.call_tool("add", {"a": "x", "b": 1})

    async def test_resources_and_prompts(self):
        async with Client(build_server()) as c:
            self.assertEqual([r["uri"] for r in await c.list_resources()], ["config://app"])
            self.assertEqual((await c.read_resource("config://app"))[0]["text"], '{"v": 1}')
            self.assertEqual(
                (await c.read_resource("file:///etc/hosts"))[0]["text"], "contents of /etc/hosts"
            )
            self.assertEqual(
                [t["uriTemplate"] for t in await c.list_resource_templates()],
                ["file://{path:path}"],
            )
            prompt = await c.get_prompt("greet", {"language": "french"})
            self.assertEqual(prompt["messages"][0]["content"]["text"], "hi in french")

    async def test_completion(self):
        async with Client(build_server()) as c:
            comp = await c.complete(
                {"type": "ref/prompt", "name": "greet"}, {"name": "language", "value": "s"}
            )
            self.assertEqual(comp["values"], ["spanish"])

    async def test_sampling_roundtrip(self):
        def sampling_handler(params):
            text = params["messages"][0]["content"]["text"]
            return f"got({text})"

        async with Client(build_server(), sampling_handler=sampling_handler) as c:
            r = await c.call_tool("review", {"code": "x=1"})
            self.assertEqual(r.text, "llm:got(review x=1)")

    async def test_elicitation_roundtrip(self):
        def elicit_handler(params):
            return {"action": "accept", "content": {"name": "Ada"}}

        async with Client(build_server(), elicitation_handler=elicit_handler) as c:
            r = await c.call_tool("ask", {})
            self.assertEqual(r.text, "accept:{'name': 'Ada'}")

    async def test_roots(self):
        async with Client(build_server(), roots=[{"uri": "file:///a"}, {"uri": "file:///b"}]) as c:
            r = await c.call_tool("where", {})
            self.assertEqual(r.text, "file:///a,file:///b")

    async def test_pagination_is_transparent(self):
        mcp = build_server(page_size=2)
        for i in range(5):
            mcp.tool(name=f"t{i}")(lambda: "x")
        async with Client(mcp) as c:
            names = {t["name"] for t in await c.list_tools()}
            self.assertTrue({"t0", "t1", "t2", "t3", "t4"} <= names)


class StdioClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_subprocess_server(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(repo, "src")
        command = [sys.executable, os.path.join(repo, "examples", "quickstart.py")]
        async with Client(command, env=env) as c:  # type: ignore[arg-type]
            tools = sorted(t["name"] for t in await c.list_tools())
            self.assertIn("add", tools)
            r = await c.call_tool("add", {"a": 4, "b": 5})
            self.assertEqual(r.text, "9")


if __name__ == "__main__":
    unittest.main()
