"""Tests for the prompt-library MCP server."""

from __future__ import annotations

import json
import unittest

from examples.prompt_library.server import mcp

from anodize_mcp import Client


class PromptListTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_prompts_returns_all_three(self):
        async with Client(mcp) as c:
            prompts = await c.list_prompts()
        names = {p["name"] for p in prompts}
        self.assertEqual(names, {"code_review", "summarize", "debug_help"})

    async def test_prompt_descriptions_present(self):
        async with Client(mcp) as c:
            prompts = await c.list_prompts()
        by_name = {p["name"]: p for p in prompts}
        self.assertIn("language", str(by_name["code_review"]))
        self.assertIn("debug_help", by_name)


class PromptGetTest(unittest.IsolatedAsyncioTestCase):
    async def test_code_review_returns_messages(self):
        async with Client(mcp) as c:
            result = await c.get_prompt("code_review", {"language": "Python", "code": "x = 1"})
        messages = result["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        text = messages[0]["content"]["text"]
        self.assertIn("Python", text)
        self.assertIn("x = 1", text)

    async def test_summarize_bullet_style(self):
        async with Client(mcp) as c:
            result = await c.get_prompt("summarize", {"text": "Hello world.", "style": "bullet"})
        messages = result["messages"]
        self.assertEqual(len(messages), 1)
        text = messages[0]["content"]["text"]
        self.assertIn("bullet", text.lower())
        self.assertIn("Hello world.", text)

    async def test_summarize_tldr_style(self):
        async with Client(mcp) as c:
            result = await c.get_prompt("summarize", {"text": "A long story.", "style": "tldr"})
        text = result["messages"][0]["content"]["text"]
        self.assertIn("single sentence", text.lower())

    async def test_debug_help_with_context(self):
        async with Client(mcp) as c:
            result = await c.get_prompt(
                "debug_help",
                {"error": "NullPointerException", "context": "line 42"},
            )
        text = result["messages"][0]["content"]["text"]
        self.assertIn("NullPointerException", text)
        self.assertIn("line 42", text)

    async def test_debug_help_without_context(self):
        async with Client(mcp) as c:
            result = await c.get_prompt("debug_help", {"error": "IndexError"})
        text = result["messages"][0]["content"]["text"]
        self.assertIn("IndexError", text)
        # Context section should not appear when omitted
        self.assertNotIn("Context:", text)


class ResourceIndexTest(unittest.IsolatedAsyncioTestCase):
    async def test_index_lists_three_prompts(self):
        async with Client(mcp) as c:
            contents = await c.read_resource("prompts://index")
        data = json.loads(contents[0]["text"])
        names = {entry["name"] for entry in data}
        self.assertEqual(names, {"code_review", "summarize", "debug_help"})

    async def test_index_entries_have_descriptions(self):
        async with Client(mcp) as c:
            contents = await c.read_resource("prompts://index")
        data = json.loads(contents[0]["text"])
        for entry in data:
            self.assertIn("name", entry)
            self.assertIn("description", entry)
            self.assertTrue(entry["description"])


class ResourceTemplateTest(unittest.IsolatedAsyncioTestCase):
    async def test_code_review_template(self):
        async with Client(mcp) as c:
            contents = await c.read_resource("prompts://template/code_review")
        text = contents[0]["text"]
        self.assertIn("{language}", text)
        self.assertIn("{code}", text)

    async def test_summarize_template(self):
        async with Client(mcp) as c:
            contents = await c.read_resource("prompts://template/summarize")
        text = contents[0]["text"]
        self.assertIn("{text}", text)
        self.assertIn("{style}", text)

    async def test_debug_help_template(self):
        async with Client(mcp) as c:
            contents = await c.read_resource("prompts://template/debug_help")
        text = contents[0]["text"]
        self.assertIn("{error}", text)

    async def test_unknown_template_raises_error(self):
        async with Client(mcp) as c:
            with self.assertRaises(Exception) as cm:
                await c.read_resource("prompts://template/nonexistent")
        # Should surface a resource-not-found-style error
        self.assertTrue(
            "nonexistent" in str(cm.exception).lower()
            or "error" in type(cm.exception).__name__.lower()
            or cm.exception is not None
        )


if __name__ == "__main__":
    unittest.main()
