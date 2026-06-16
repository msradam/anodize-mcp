"""Tests for the async data-pipeline MCP server.

Run with:
    cd /Users/amsrahman/anodize-mcp && PYTHONPATH=src uv run python -m pytest examples/async_pipeline/test_async_pipeline.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Add the examples/async_pipeline directory so we can import server.py directly.
sys.path.insert(0, str(Path(__file__).parent))

from server import _timing, mcp  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session():
    """Return (session, notifications_list).

    The session captures all notifications/messages sent to the 'client'.
    """
    notes: list[dict] = []
    session = mcp.new_session(send=notes.append)
    return session, notes


def call_tool(session, name: str, arguments: dict | None = None) -> dict:
    """Send a tools/call message and return the result portion."""
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    response = mcp.handle_message(msg, session)
    assert response is not None, "expected a JSON-RPC response"
    assert "result" in response, f"unexpected error response: {response}"
    return response["result"]


def extract_text(result: dict) -> str:
    """Return the text of the first content block."""
    blocks = result.get("content", [])
    assert blocks, "no content blocks in result"
    return blocks[0]["text"]


def extract_json(result: dict) -> object:
    """Parse the structured content or text block as JSON."""
    if result.get("structuredContent"):
        return result["structuredContent"]
    return json.loads(extract_text(result))


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.session, self.notes = _make_session()

    def test_all_valid(self):
        result = call_tool(
            self.session,
            "ingest",
            {"records": [{"a": 1}, {"b": 2}, {"c": 3}]},
        )
        self.assertFalse(result["isError"])
        data = extract_json(result)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["valid"], 3)
        self.assertEqual(data["invalid"], 0)

    def test_mixed_valid_invalid(self):
        result = call_tool(
            self.session,
            "ingest",
            {"records": [{"a": 1}, {}, {"c": 3}]},
        )
        self.assertFalse(result["isError"])
        data = extract_json(result)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["valid"], 2)
        self.assertEqual(data["invalid"], 1)

    def test_empty_batch(self):
        result = call_tool(self.session, "ingest", {"records": []})
        self.assertFalse(result["isError"])
        data = extract_json(result)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["valid"], 0)
        self.assertEqual(data["invalid"], 0)

    def test_context_logging_captured(self):
        """ctx.info() and ctx.debug() send notifications/message to the session."""
        call_tool(self.session, "ingest", {"records": [{"x": 1}]})
        log_notes = [n for n in self.notes if n.get("method") == "notifications/message"]
        # Expect at least one info-level log from ingest.
        levels = [n["params"]["level"] for n in log_notes]
        self.assertIn("info", levels)


class TestTransform(unittest.TestCase):
    def setUp(self):
        self.session, _ = _make_session()

    def test_upper(self):
        result = call_tool(self.session, "transform", {"payload": "hello", "operations": ["upper"]})
        self.assertFalse(result["isError"])
        self.assertEqual(extract_text(result), "HELLO")

    def test_lower(self):
        result = call_tool(self.session, "transform", {"payload": "WORLD", "operations": ["lower"]})
        self.assertFalse(result["isError"])
        self.assertEqual(extract_text(result), "world")

    def test_strip(self):
        result = call_tool(
            self.session, "transform", {"payload": "  hi  ", "operations": ["strip"]}
        )
        self.assertFalse(result["isError"])
        self.assertEqual(extract_text(result), "hi")

    def test_reverse(self):
        result = call_tool(self.session, "transform", {"payload": "abc", "operations": ["reverse"]})
        self.assertFalse(result["isError"])
        self.assertEqual(extract_text(result), "cba")

    def test_chained_operations(self):
        result = call_tool(
            self.session,
            "transform",
            {"payload": "  Hello  ", "operations": ["strip", "upper", "reverse"]},
        )
        self.assertFalse(result["isError"])
        self.assertEqual(extract_text(result), "OLLEH")

    def test_bad_operation_returns_error(self):
        """An unknown operation must produce an isError result."""
        result = call_tool(self.session, "transform", {"payload": "x", "operations": ["explode"]})
        self.assertTrue(result["isError"], "expected isError=true for unknown operation")
        self.assertIn("explode", extract_text(result))

    def test_empty_operations(self):
        result = call_tool(self.session, "transform", {"payload": "unchanged", "operations": []})
        self.assertFalse(result["isError"])
        self.assertEqual(extract_text(result), "unchanged")


class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.session, _ = _make_session()

    def test_word_counts(self):
        result = call_tool(
            self.session,
            "summarize",
            {"texts": ["hello world", "one two three", "single"]},
        )
        self.assertFalse(result["isError"])
        data = extract_json(result)
        self.assertEqual(data["hello world"], 2)
        self.assertEqual(data["one two three"], 3)
        self.assertEqual(data["single"], 1)

    def test_empty_list(self):
        result = call_tool(self.session, "summarize", {"texts": []})
        self.assertFalse(result["isError"])
        data = extract_json(result)
        self.assertEqual(data, {})

    def test_empty_string(self):
        result = call_tool(self.session, "summarize", {"texts": [""]})
        self.assertFalse(result["isError"])
        data = extract_json(result)
        # "".split() == [] → 0 words
        self.assertEqual(data[""], 0)


class TestGetMetrics(unittest.TestCase):
    def setUp(self):
        # Reset stats so tests are independent.
        _timing._stats.clear()
        self.session, _ = _make_session()

    def test_metrics_accumulate(self):
        call_tool(self.session, "transform", {"payload": "a", "operations": ["upper"]})
        call_tool(self.session, "transform", {"payload": "b", "operations": ["lower"]})
        result = call_tool(self.session, "get_metrics", {})
        self.assertFalse(result["isError"])
        data = extract_json(result)
        self.assertIn("transform", data)
        self.assertEqual(data["transform"]["count"], 2)
        self.assertGreaterEqual(data["transform"]["total_ms"], 0)

    def test_metrics_empty_initially(self):
        result = call_tool(self.session, "get_metrics", {})
        self.assertFalse(result["isError"])
        data = extract_json(result)
        self.assertEqual(data, {})

    def test_metrics_track_ingest(self):
        call_tool(self.session, "ingest", {"records": [{"a": 1}]})
        result = call_tool(self.session, "get_metrics", {})
        data = extract_json(result)
        self.assertIn("ingest", data)
        self.assertEqual(data["ingest"]["count"], 1)
        self.assertIn("mean_ms", data["ingest"])


class TestToolListing(unittest.TestCase):
    def setUp(self):
        self.session, _ = _make_session()

    def test_all_tools_registered(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        response = mcp.handle_message(msg, self.session)
        tool_names = {t["name"] for t in response["result"]["tools"]}
        self.assertIn("ingest", tool_names)
        self.assertIn("transform", tool_names)
        self.assertIn("summarize", tool_names)
        self.assertIn("get_metrics", tool_names)


if __name__ == "__main__":
    unittest.main()
