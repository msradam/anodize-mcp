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


class Round9ParityTest(unittest.TestCase):
    def test_non_finite_floats_are_errors(self):
        mcp = AnodizeMCP("nf")

        @mcp.tool
        def inf_val() -> float:
            return float("inf")

        async def main():
            async with Client(mcp) as c:
                return await c.call_tool("inf_val", {}, raise_on_error=False)

        result = asyncio.run(main())
        self.assertTrue(result.is_error)

    def test_datetime_serialization_matches_pydantic(self):
        import datetime as dt

        mcp = AnodizeMCP("dt")

        @mcp.tool
        def utc_dt() -> dt.datetime:
            return dt.datetime(2026, 6, 12, 1, 2, 3, tzinfo=dt.timezone.utc)

        @mcp.tool
        def duration() -> dt.timedelta:
            return dt.timedelta(days=1, hours=2, seconds=3.5)

        async def main():
            async with Client(mcp) as c:
                a = await c.call_tool("utc_dt", {})
                b = await c.call_tool("duration", {})
                return a, b

        a, b = asyncio.run(main())
        self.assertEqual(a.text, '"2026-06-12T01:02:03Z"')
        self.assertEqual(a.data, "2026-06-12T01:02:03Z")
        self.assertEqual(b.data, "P1DT2H3.5S")

    def test_prompt_embedded_resource_wire_shape(self):
        from anodize_mcp import EmbeddedResource
        from anodize_mcp.prompts.prompt import PromptMessage

        mcp = AnodizeMCP("pe")

        @mcp.prompt
        def doc() -> PromptMessage:
            return PromptMessage(
                role="user",
                content=EmbeddedResource(uri="data://x", text="body", mimeType="text/plain"),
            )

        async def main():
            async with Client(mcp) as c:
                return await c.get_prompt("doc", {})

        result = asyncio.run(main())
        content = result["messages"][0]["content"]
        self.assertEqual(content["type"], "resource")
        self.assertEqual(content["resource"]["uri"], "data://x")
        self.assertEqual(content["resource"]["text"], "body")

    def test_basemodel_return_is_unwrapped(self):
        try:
            from pydantic import BaseModel
        except ImportError:
            self.skipTest("pydantic not installed")

        class Out(BaseModel):
            a: int
            b: str

        mcp = AnodizeMCP("bm")

        @mcp.tool
        def model_out() -> Out:
            return Out(a=1, b="x")

        async def main():
            async with Client(mcp) as c:
                tool = (await c.list_tools())[0]
                result = await c.call_tool("model_out", {})
                return tool, result

        tool, result = asyncio.run(main())
        self.assertNotIn("x-fastmcp-wrap-result", tool["outputSchema"])
        self.assertEqual(result.structured_content, {"a": 1, "b": "x"})

    def test_recursive_model_defs_hoisted_to_root(self):
        try:
            from pydantic import BaseModel
        except ImportError:
            self.skipTest("pydantic not installed")

        class Node(BaseModel):
            name: str
            children: "list[Node]" = []

        Node.model_rebuild()
        mcp = AnodizeMCP("rec")

        @mcp.tool
        def walk(root: Node) -> int:
            return len(root.children)

        async def main():
            async with Client(mcp) as c:
                return (await c.list_tools())[0]

        tool = asyncio.run(main())
        schema = tool["inputSchema"]
        self.assertNotIn("$defs", schema["properties"]["root"])
        if "$ref" in json.dumps(schema["properties"]["root"]):
            self.assertIn("$defs", schema)


class Round10ParityTest(unittest.TestCase):
    def test_var_args_rejected_at_registration(self):
        mcp = AnodizeMCP("va")
        with self.assertRaises(ValueError):

            @mcp.tool
            def kw(**kwargs) -> str:
                return "x"

        with self.assertRaises(ValueError):

            @mcp.tool
            def star(*args) -> str:
                return "x"

    def test_template_params_coerced_and_json_mime(self):
        mcp = AnodizeMCP("tp")

        @mcp.resource("data://pair/{a}/{b}")
        def pair(a: int, b: int) -> dict:
            return {"a": a, "b": b}

        async def main():
            async with Client(mcp) as c:
                templates = await c.list_resource_templates()
                contents = await c.read_resource("data://pair/4/5")
                return templates[0], contents[0]

        template, content = asyncio.run(main())
        self.assertEqual(template["mimeType"], "text/plain")
        self.assertEqual(content["mimeType"], "application/json")
        self.assertEqual(json.loads(content["text"]), {"a": 4, "b": 5})

    def test_meta_reaches_request_context(self):
        from anodize_mcp import Context

        mcp = AnodizeMCP("mt")

        @mcp.tool
        def who(ctx: Context) -> str:
            meta = ctx.request_context.meta
            return meta.userId if meta else "none"

        async def main():
            async with Client(mcp) as c:
                return await c.call_tool("who", {}, meta={"userId": "u1"})

        self.assertEqual(asyncio.run(main()).text, "u1")

    def test_dict_annotation_unwrapped_and_untyped_dict_structured(self):
        mcp = AnodizeMCP("dw")

        @mcp.tool
        def typed() -> dict:
            return {"a": 1}

        @mcp.tool
        def untyped():
            return {"b": 2}

        async def main():
            async with Client(mcp) as c:
                tools = {t["name"]: t for t in await c.list_tools()}
                a = await c.call_tool("typed", {})
                b = await c.call_tool("untyped", {})
                return tools, a, b

        tools, a, b = asyncio.run(main())
        self.assertNotIn("x-fastmcp-wrap-result", tools["typed"]["outputSchema"])
        self.assertEqual(a.structured_content, {"a": 1})
        self.assertEqual(b.structured_content, {"b": 2})

    def test_attrdict_missing_field_is_none(self):
        mcp = AnodizeMCP("af")

        @mcp.tool
        def pic() -> Image:
            return Image(data=b"x")

        async def main():
            async with Client(mcp) as c:
                return (await c.list_tools())[0]

        tool = asyncio.run(main())
        self.assertIsNone(tool.outputSchema)

    def test_mixed_content_list(self):
        mcp = AnodizeMCP("mx")

        @mcp.tool
        def mixed():
            return ["caption", Image(data=b"png"), {"k": 1}]

        async def main():
            async with Client(mcp) as c:
                return await c.call_tool("mixed", {})

        result = asyncio.run(main())
        kinds = [b["type"] for b in result.content]
        self.assertEqual(kinds, ["text", "image", "text"])
        self.assertEqual(result.content[0]["text"], "caption")
        self.assertEqual(json.loads(result.content[2]["text"]), {"k": 1})

    def test_elicit_without_schema(self):
        from anodize_mcp import Context

        mcp = AnodizeMCP("el")

        @mcp.tool
        async def confirm(ctx: Context) -> str:
            r = await ctx.elicit("Proceed?")
            return r.action

        async def handler(message, response_type, params, context):
            return None

        async def main():
            async with Client(mcp, elicitation_handler=handler) as c:
                return await c.call_tool("confirm", {})

        self.assertEqual(asyncio.run(main()).text, "accept")


class Round11ParityTest(unittest.TestCase):
    def test_template_description_keeps_args_section(self):
        mcp = AnodizeMCP("td")

        @mcp.resource("doc://chapter/{n}")
        def chapter(n: int) -> str:
            """A chapter of the manual.

            Args:
                n: Chapter number, one-based.
            """
            return f"chapter {n}"

        async def main():
            async with Client(mcp) as c:
                return (await c.list_resource_templates())[0]

        template = asyncio.run(main())
        self.assertIn("Args:", template["description"])
        self.assertIn("Chapter number", template["description"])

    def test_annotations_title_fallback(self):
        mcp = AnodizeMCP("at")

        @mcp.tool(annotations={"title": "Ann Title"})
        def t() -> str:
            return "x"

        async def main():
            async with Client(mcp) as c:
                return (await c.list_tools())[0]

        self.assertEqual(asyncio.run(main())["title"], "Ann Title")

    def test_set_state_serializable_kwarg(self):
        from anodize_mcp import Context

        mcp = AnodizeMCP("ss")

        @mcp.tool
        async def stash(ctx: Context) -> str:
            await ctx.set_state("conn", object(), serializable=False)
            return "ok" if await ctx.get_state("conn") is not None else "missing"

        async def main():
            async with Client(mcp) as c:
                return await c.call_tool("stash", {})

        self.assertEqual(asyncio.run(main()).text, "ok")

    def test_client_reusable_after_close(self):
        mcp = AnodizeMCP("ru")

        @mcp.tool
        def t() -> str:
            return "x"

        async def main():
            client = Client(mcp)
            async with client as c:
                await c.ping()
            await client.close()  # double close is harmless
            async with client as c:
                return (await c.call_tool("t", {})).text

        self.assertEqual(asyncio.run(main()), "x")

    def test_schema_less_elicit_accept_data_is_empty_dict(self):
        from anodize_mcp import Context

        mcp = AnodizeMCP("ed")

        @mcp.tool
        async def confirm(ctx: Context) -> str:
            r = await ctx.elicit("Proceed?")
            return f"{r.action}:{r.data!r}"

        async def handler(message, response_type, params, context):
            return None

        async def main():
            async with Client(mcp, elicitation_handler=handler) as c:
                return await c.call_tool("confirm", {})

        self.assertEqual(asyncio.run(main()).text, "accept:{}")


class Round13ParityTest(unittest.TestCase):
    def test_returns_only_docstring_kept_in_full(self):
        mcp = AnodizeMCP("doc")

        @mcp.tool
        def scenes() -> dict:
            """Lists scenes, grouped.

            Returns:
                dict mapping group to scenes.
            """
            return {}

        async def main():
            async with Client(mcp) as c:
                return (await c.list_tools())[0]

        description = asyncio.run(main())["description"]
        self.assertIn("Returns:", description)

    def test_typeddict_param_schema_and_validation(self):
        import sys

        if sys.version_info < (3, 11):
            self.skipTest("NotRequired requires 3.11")
        from typing import Annotated, NotRequired, TypedDict

        from anodize_mcp import Field

        class Opts(TypedDict):
            on: bool
            bri: NotRequired[Annotated[int, Field(ge=0, le=254)]]

        mcp = AnodizeMCP("td")

        @mcp.tool
        def set_light(opts: Opts) -> dict:
            return dict(opts)

        async def main():
            async with Client(mcp) as c:
                tool = (await c.list_tools())[0]
                ok = await c.call_tool("set_light", {"opts": {"on": True, "bri": 100}})
                bad = await c.call_tool(
                    "set_light", {"opts": {"on": True, "bri": 500}}, raise_on_error=False
                )
                return tool, ok, bad

        tool, ok, bad = asyncio.run(main())
        prop = tool["inputSchema"]["properties"]["opts"]
        self.assertEqual(prop["properties"]["bri"]["maximum"], 254)
        self.assertEqual(prop["required"], ["on"])
        self.assertEqual(ok.data, {"on": True, "bri": 100})
        self.assertTrue(bad.is_error)

    def test_client_py_path_target(self):
        import os
        import tempfile
        import textwrap

        script = textwrap.dedent(
            """
            from anodize_mcp import AnodizeMCP

            mcp = AnodizeMCP("pathsrv")

            @mcp.tool
            def hello() -> str:
                return "from-path"

            mcp.run()
            """
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(script)
            path = f.name
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parent.parent / "src")
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("PATH", "")
        try:
            # Force the subprocess interpreter by symlinking sys.executable? No:
            # the .py inference uses sys.executable already.
            async def main():
                async with Client(path, env=env) as c:
                    return (await c.call_tool("hello", {})).text

            self.assertEqual(asyncio.run(main()), "from-path")
        finally:
            os.unlink(path)


class Round14ParityTest(unittest.TestCase):
    def test_disable_enable_by_names_and_tags(self):
        mcp = AnodizeMCP("vis")

        @mcp.tool
        def alpha() -> str:
            return "a"

        @mcp.tool(tags={"beta-group"})
        def beta() -> str:
            return "b"

        async def main():
            async with Client(mcp) as c:
                mcp.disable(names={"alpha"}, tags={"beta-group"})
                hidden = sorted(t["name"] for t in await c.list_tools())
                blocked = await c.call_tool("alpha", {}, raise_on_error=False)
                mcp.enable(names={"alpha"}, keys=["tool:beta@"])
                restored = sorted(t["name"] for t in await c.list_tools())
                return hidden, blocked, restored

        hidden, blocked, restored = asyncio.run(main())
        self.assertEqual(hidden, [])
        self.assertTrue(blocked.is_error)
        self.assertEqual(restored, ["alpha", "beta"])

    def test_client_follows_trailing_slash_redirect(self):
        import threading
        from http.server import ThreadingHTTPServer

        from anodize_mcp.transports.http import _make_handler, _Manager

        mcp = AnodizeMCP("rs")

        @mcp.tool
        def hello() -> str:
            return "hi"

        manager = _Manager(server=mcp, endpoint="/mcp", allowed_origins=None, stateless=True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(manager))
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:

            async def main():
                async with Client(f"http://127.0.0.1:{port}/mcp/", timeout=10) as c:
                    return (await c.call_tool("hello", {})).text

            self.assertEqual(asyncio.run(main()), "hi")
        finally:
            httpd.shutdown()
            httpd.server_close()


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
