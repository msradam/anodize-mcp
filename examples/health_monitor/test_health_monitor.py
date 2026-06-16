"""Tests for the health-monitor MCP server."""

from __future__ import annotations

import json
import os
import sys
import unittest

# Ensure the package is importable when run directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from anodize_mcp import Client


def _make_server():
    """Return a fresh server instance with clean module-level state."""
    import importlib

    import examples.health_monitor.server as mod

    # Reset module-level mutable state so tests do not bleed into each other.
    mod._event_log.clear()
    mod._metrics_collector = None

    # Re-import to get a new AnodizeMCP instance.
    importlib.reload(mod)
    return mod.mcp


class ResourceReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_cpu_resource(self):
        async with Client(_make_server()) as c:
            contents = await c.read_resource("metrics://cpu")
            payload = json.loads(contents[0]["text"])
            self.assertIn("usage_pct", payload)
            self.assertEqual(payload["usage_pct"], 42.0)

    async def test_memory_resource(self):
        async with Client(_make_server()) as c:
            contents = await c.read_resource("metrics://memory")
            payload = json.loads(contents[0]["text"])
            self.assertIn("usage_pct", payload)
            self.assertIn("total_mb", payload)

    async def test_cpu_and_memory_listed(self):
        async with Client(_make_server()) as c:
            resources = await c.list_resources()
            uris = {r["uri"] for r in resources}
            self.assertIn("metrics://cpu", uris)
            self.assertIn("metrics://memory", uris)


class TemplateResourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_known_pid_returns_stats(self):
        async with Client(_make_server()) as c:
            contents = await c.read_resource("metrics://process/1")
            payload = json.loads(contents[0]["text"])
            self.assertEqual(payload["pid"], 1)
            self.assertEqual(payload["name"], "init")

    async def test_another_known_pid(self):
        async with Client(_make_server()) as c:
            contents = await c.read_resource("metrics://process/100")
            payload = json.loads(contents[0]["text"])
            self.assertEqual(payload["name"], "health_monitor")

    async def test_unknown_pid_raises_error(self):
        from anodize_mcp import ClientError

        async with Client(_make_server()) as c:
            with self.assertRaises(ClientError):
                await c.read_resource("metrics://process/9999")

    async def test_template_listed(self):
        async with Client(_make_server()) as c:
            templates = await c.list_resource_templates()
            uri_templates = [t["uriTemplate"] for t in templates]
            self.assertIn("metrics://process/{pid}", uri_templates)


class ToolRoundTripTest(unittest.IsolatedAsyncioTestCase):
    async def test_record_then_get(self):
        async with Client(_make_server()) as c:
            r = await c.call_tool(
                "record_event",
                {"name": "disk_full", "severity": "error", "message": "no space left"},
            )
            self.assertEqual(r.data, 1)

            r2 = await c.call_tool("get_events", {"limit": 5})
            events = r2.data
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["name"], "disk_full")
            self.assertEqual(events[0]["severity"], "error")

    async def test_multiple_records(self):
        async with Client(_make_server()) as c:
            await c.call_tool("record_event", {"name": "e1", "severity": "info", "message": "m1"})
            await c.call_tool("record_event", {"name": "e2", "severity": "warn", "message": "m2"})
            r = await c.call_tool("get_events", {"limit": 10})
            self.assertEqual(len(r.data), 2)

    async def test_get_events_default_limit(self):
        async with Client(_make_server()) as c:
            for i in range(15):
                await c.call_tool(
                    "record_event",
                    {"name": f"e{i}", "severity": "info", "message": f"msg {i}"},
                )
            r = await c.call_tool("get_events", {})
            # default limit is 10
            self.assertEqual(len(r.data), 10)

    async def test_clear_events(self):
        async with Client(_make_server()) as c:
            await c.call_tool("record_event", {"name": "x", "severity": "info", "message": "y"})
            await c.call_tool("record_event", {"name": "x2", "severity": "warn", "message": "z"})
            cleared = await c.call_tool("clear_events", {})
            self.assertEqual(cleared.data, 2)

            remaining = await c.call_tool("get_events", {})
            self.assertEqual(remaining.data, [])


class LifespanTest(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_collector_initialized_before_first_request(self):
        import examples.health_monitor.server as mod

        mod._event_log.clear()
        mod._metrics_collector = None

        import importlib

        importlib.reload(mod)
        server = mod.mcp

        async with Client(server) as c:
            # The lifespan must have fired before we can call any tool.
            self.assertIsNotNone(mod._metrics_collector)
            self.assertTrue(mod._metrics_collector.get("started"))

            # Sanity-check: a tool call works inside the lifespan.
            r = await c.call_tool(
                "record_event",
                {"name": "startup", "severity": "info", "message": "server ready"},
            )
            self.assertEqual(r.data, 1)

        # After context exit, collector is torn down.
        self.assertIsNone(mod._metrics_collector)

    async def test_lifespan_context_accessible_via_ctx(self):
        """ctx.lifespan_context carries the yielded dict after startup."""
        import importlib

        import examples.health_monitor.server as mod

        mod._event_log.clear()
        importlib.reload(mod)
        server = mod.mcp

        # Verify the lifespan was entered and the state dict is available
        # via server._lifespan_state (the value yielded by the context manager).
        # We confirm this indirectly: record_event succeeds, and the module-level
        # _metrics_collector set during lifespan startup is not None.
        async with Client(server) as c:
            self.assertIsNotNone(mod._metrics_collector)
            self.assertTrue(mod._metrics_collector.get("started"))
            # The yielded value is the same object as the module-level collector.
            self.assertIs(server._lifespan_state, mod._metrics_collector)

            r = await c.call_tool(
                "record_event",
                {"name": "lc_test", "severity": "info", "message": "lifespan ok"},
            )
            self.assertFalse(r.is_error)


if __name__ == "__main__":
    unittest.main()
