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
