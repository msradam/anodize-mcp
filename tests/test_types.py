import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

from anodize_mcp import AnodizeMCP, Audio, Client, File, Image
from anodize_mcp.tools import Tool, ToolDef, ToolResult


class ImageTest(unittest.TestCase):
    def test_mime_from_path_uses_registered_types(self):
        self.assertEqual(Image(path="photo.jpg").mime_type, "image/jpeg")
        self.assertEqual(Image(path="pic.svg").mime_type, "image/svg+xml")
        self.assertEqual(Image(path="pic.webp").mime_type, "image/webp")

    def test_explicit_format_wins(self):
        self.assertEqual(Image(data=b"x", format="JPEG").mime_type, "image/jpeg")
        self.assertEqual(Image(data=b"x").mime_type, "image/png")

    def test_path_and_data_rejected(self):
        with self.assertRaises(ValueError):
            Image(path="x.png", data=b"x")
        with self.assertRaises(ValueError):
            Image()


class AudioTest(unittest.TestCase):
    def test_mime_mapping(self):
        self.assertEqual(Audio(path="a.mp3").mime_type, "audio/mpeg")
        self.assertEqual(Audio(data=b"x").mime_type, "audio/wav")
        self.assertEqual(Audio(data=b"x", format="ogg").mime_type, "audio/ogg")

    def test_content_block(self):
        block = Audio(data=b"abc").to_content_block().to_dict()
        self.assertEqual(block["type"], "audio")
        self.assertEqual(block["data"], base64.b64encode(b"abc").decode())


class FileTest(unittest.TestCase):
    def test_format_maps_to_full_mime(self):
        self.assertEqual(File(data=b"x", format="pdf").mime_type, "application/pdf")
        for fmt in ("plain", "txt", "text"):
            self.assertEqual(File(data=b"x", format=fmt).mime_type, "text/plain")

    def test_path_keeps_directory_and_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "notes.txt"
            path.parent.mkdir()
            path.write_text("hello")
            block = File(path=path).to_content_block().to_dict()
            resource = block["resource"]
            self.assertEqual(resource["uri"], path.resolve().as_uri())
            self.assertEqual(resource["text"], "hello")
            self.assertEqual(resource["mimeType"], "text/plain")
            self.assertNotIn("blob", resource)

    def test_data_uri_includes_extension(self):
        block = File(data=b"%PDF", format="pdf", name="doc").to_content_block().to_dict()
        self.assertEqual(block["resource"]["uri"], "file:///doc.pdf")
        self.assertIn("blob", block["resource"])

    def test_path_and_data_rejected(self):
        with self.assertRaises(ValueError):
            File(path="x.bin", data=b"x")


class ToolResultTest(unittest.TestCase):
    def test_requires_some_content(self):
        with self.assertRaises(ValueError):
            ToolResult()

    def test_structured_only_backfills_content(self):
        result = ToolResult(structured_content={"a": 1})
        self.assertEqual(json.loads(result.content), {"a": 1})

    def test_is_error_flows_to_wire(self):
        mcp = AnodizeMCP("t")

        @mcp.tool
        def fail() -> ToolResult:
            return ToolResult(content="nope", is_error=True)

        async def main():
            async with Client(mcp) as c:
                return await c.call_tool("fail", {}, raise_on_error=False)

        result = asyncio.run(main())
        self.assertTrue(result.is_error)

    def test_package_exports(self):
        self.assertIs(Tool, ToolDef)
        from anodize_mcp.tools import ToolResult as ReExported

        self.assertIs(ReExported, ToolResult)


class Round6ParityTest(unittest.TestCase):
    def test_list_return_is_json_text(self):
        mcp = AnodizeMCP("l")

        @mcp.tool
        def crew() -> list:
            return ["Pinchy", "Bubbles"]

        async def main():
            async with Client(mcp) as c:
                return await c.call_tool("crew", {})

        result = asyncio.run(main())
        self.assertEqual(result.text, '["Pinchy","Bubbles"]')

    def test_client_error_is_tool_error(self):
        from anodize_mcp import ToolError

        mcp = AnodizeMCP("e")

        @mcp.tool
        def boom() -> str:
            raise ValueError("bad")

        async def main():
            async with Client(mcp) as c:
                try:
                    await c.call_tool("boom", {})
                except ToolError:
                    return "caught"
            return "missed"

        self.assertEqual(asyncio.run(main()), "caught")

    def test_array_constraints_emit_max_items(self):
        from typing import Annotated

        from anodize_mcp import Field

        mcp = AnodizeMCP("s")

        @mcp.tool
        def names(values: Annotated[list, Field(max_length=10)]) -> int:
            return len(values)

        async def main():
            async with Client(mcp) as c:
                return (await c.list_tools())[0]

        tool = asyncio.run(main())
        prop = tool["inputSchema"]["properties"]["values"]
        self.assertEqual(prop.get("maxItems"), 10)
        self.assertNotIn("maxLength", prop)

    def test_helper_return_annotations_have_no_output_schema(self):
        from anodize_mcp.tools import ToolResult

        mcp = AnodizeMCP("h")

        @mcp.tool
        def pic() -> Image:
            return Image(data=b"x")

        @mcp.tool
        def explicit() -> ToolResult:
            return ToolResult(content="x")

        async def main():
            async with Client(mcp) as c:
                return {t["name"]: t for t in await c.list_tools()}

        tools = asyncio.run(main())
        self.assertNotIn("outputSchema", tools["pic"])
        self.assertNotIn("outputSchema", tools["explicit"])

    def test_empty_required_omitted_and_wrap_marker_present(self):
        mcp = AnodizeMCP("r")

        @mcp.tool
        def zero() -> int:
            return 1

        async def main():
            async with Client(mcp) as c:
                return (await c.list_tools())[0]

        tool = asyncio.run(main())
        self.assertNotIn("required", tool["inputSchema"])
        self.assertTrue(tool["outputSchema"]["x-fastmcp-wrap-result"])

    def test_resource_mime_defaults_to_text_plain(self):
        mcp = AnodizeMCP("m")

        @mcp.resource("data://d")
        def d() -> dict:
            return {"a": 1}

        async def main():
            async with Client(mcp) as c:
                listed = (await c.list_resources())[0]
                contents = await c.read_resource("data://d")
                return listed, contents

        listed, contents = asyncio.run(main())
        self.assertEqual(listed["mimeType"], "text/plain")
        self.assertEqual(contents[0]["mimeType"], "text/plain")

    def test_pydantic_schema_is_inlined(self):
        try:
            from pydantic import BaseModel
        except ImportError:
            self.skipTest("pydantic not installed")

        class Inner(BaseModel):
            x: int

        class Outer(BaseModel):
            inner: Inner

        mcp = AnodizeMCP("p")

        @mcp.tool
        def take(o: Outer) -> int:
            return o.inner.x

        async def main():
            async with Client(mcp) as c:
                return (await c.list_tools())[0]

        tool = asyncio.run(main())
        prop = tool["inputSchema"]["properties"]["o"]
        self.assertNotIn("$ref", json.dumps(prop))
        self.assertNotIn("title", prop)
        self.assertEqual(prop["properties"]["inner"]["properties"]["x"]["type"], "integer")


class Round7ParityTest(unittest.TestCase):
    def test_int_keyed_dict_coercion(self):
        from typing import Dict

        mcp = AnodizeMCP("k")

        @mcp.tool
        def typed(m: Dict[int, str]) -> list:
            return [k * 2 for k in m]

        async def main():
            async with Client(mcp) as c:
                return await c.call_tool("typed", {"m": {"1": "a", "2": "b"}})

        result = asyncio.run(main())
        self.assertEqual(result.data, [2, 4])

    def test_enum_keyed_dict_emits_property_names(self):
        import enum
        from typing import Dict

        class Color(enum.Enum):
            RED = "red"
            BLUE = "blue"

        mcp = AnodizeMCP("e")

        @mcp.tool
        def take(m: Dict[Color, int]) -> int:
            return len(m)

        async def main():
            async with Client(mcp) as c:
                return (await c.list_tools())[0]

        tool = asyncio.run(main())
        prop = tool["inputSchema"]["properties"]["m"]
        self.assertEqual(sorted(prop["propertyNames"]["enum"]), ["blue", "red"])

    def test_prompt_optional_arg_lists_required_false(self):
        from typing import Optional

        mcp = AnodizeMCP("p")

        @mcp.prompt
        def review(code: str, style: Optional[str] = None) -> str:
            return code

        async def main():
            async with Client(mcp) as c:
                return (await c.list_prompts())[0]

        prompt = asyncio.run(main())
        args = {a["name"]: a for a in prompt["arguments"]}
        self.assertTrue(args["code"]["required"])
        self.assertFalse(args["style"]["required"])

    def test_bare_container_schemas_keep_vacuous_keywords(self):
        mcp = AnodizeMCP("v")

        @mcp.tool
        def take(items: list, mapping: dict) -> int:
            return len(items) + len(mapping)

        async def main():
            async with Client(mcp) as c:
                return (await c.list_tools())[0]

        tool = asyncio.run(main())
        props = tool["inputSchema"]["properties"]
        self.assertEqual(props["items"]["items"], {})
        self.assertIs(props["mapping"]["additionalProperties"], True)

    def test_progress_handler_receives_floats(self):
        from anodize_mcp import Context

        mcp = AnodizeMCP("f")

        @mcp.tool
        async def prog(ctx: Context) -> str:
            await ctx.report_progress(1, 2)
            return "ok"

        events = []

        async def main():
            async with Client(mcp, progress_handler=lambda p, t, m: events.append((p, t))) as c:
                await c.call_tool("prog", {})

        asyncio.run(main())
        self.assertEqual(events, [(1.0, 2.0)])
        self.assertIsInstance(events[0][0], float)

    def test_default_version_is_package_version(self):
        import anodize_mcp

        self.assertEqual(AnodizeMCP("x").version, anodize_mcp.__version__)
        self.assertEqual(AnodizeMCP("x", version="9.9").version, "9.9")

    def test_callable_middleware_runs(self):
        mcp = AnodizeMCP("c")
        counts = {"n": 0}

        async def counting(context, call_next):
            counts["n"] += 1
            return await call_next(context)

        mcp.add_middleware(counting)

        @mcp.tool
        def t() -> str:
            return "x"

        async def main():
            async with Client(mcp) as c:
                await c.call_tool("t", {})

        asyncio.run(main())
        self.assertGreater(counts["n"], 0)


class ResourceListTest(unittest.TestCase):
    def test_list_return_is_one_json_document(self):
        mcp = AnodizeMCP("r")

        @mcp.resource("dir://x")
        def listing() -> list:
            return ["a", "b"]

        async def main():
            async with Client(mcp) as c:
                return await c.read_resource("dir://x")

        contents = asyncio.run(main())
        self.assertEqual(len(contents), 1)
        self.assertEqual(json.loads(contents[0]["text"]), ["a", "b"])


class MetaTagsTest(unittest.TestCase):
    def test_listed_tools_always_carry_tags(self):
        mcp = AnodizeMCP("m")

        @mcp.tool
        def plain() -> str:
            return "x"

        @mcp.tool(tags={"b", "a"})
        def tagged() -> str:
            return "y"

        async def main():
            async with Client(mcp) as c:
                return {t["name"]: t for t in await c.list_tools()}

        tools = asyncio.run(main())
        self.assertEqual(tools["plain"]["_meta"]["fastmcp"]["tags"], [])
        self.assertEqual(tools["tagged"]["_meta"]["fastmcp"]["tags"], ["a", "b"])


class BareResourceDecoratorTest(unittest.TestCase):
    def test_bare_resource_decorator_raises(self):
        mcp = AnodizeMCP("d")
        with self.assertRaises(TypeError):

            @mcp.resource
            def broken() -> str:
                return "x"


class PromptArgumentTest(unittest.TestCase):
    def test_client_stringifies_prompt_arguments(self):
        mcp = AnodizeMCP("p")
        seen = {}

        @mcp.prompt
        def show(data: str) -> str:
            seen["type"] = type(data).__name__
            return data

        async def main():
            async with Client(mcp) as c:
                return await c.get_prompt("show", {"data": {"x": 1}})

        result = asyncio.run(main())
        self.assertEqual(seen["type"], "str")
        self.assertEqual(json.loads(result["messages"][0]["content"]["text"]), {"x": 1})


if __name__ == "__main__":
    unittest.main()
