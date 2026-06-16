"""Tests for the config-vault MCP server.

Run with:
    cd /Users/amsrahman/anodize-mcp && PYTHONPATH=src uv run python -m pytest examples/config_vault/test_config_vault.py -v
"""

from __future__ import annotations

import asyncio
import json
import unittest

from anodize_mcp import Client, FastMCPTransport


class ConfigVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        from examples.config_vault.server import _store, mcp

        self.mcp = mcp
        self._store = _store
        # Reset store to a known state before each test.
        _store.clear()
        _store["dev"] = {"db_url": "postgres://localhost/dev", "log_level": "DEBUG"}
        _store["prod"] = {"db_url": "postgres://rds.example.com/prod", "log_level": "WARNING"}

    def _run(self, coro):  # noqa: ANN001
        return asyncio.run(coro)

    # -- resource read -------------------------------------------------------

    def test_resource_read_existing_key(self) -> None:
        """Reading a present config key returns the correct JSON value."""

        async def _test() -> None:
            async with Client(transport=FastMCPTransport(self.mcp)) as client:
                contents = await client.read_resource("config://dev/db_url")
                self.assertTrue(len(contents) > 0)
                item = contents[0]
                raw = item.get("text") or item.get("blob", "")
                body = json.loads(raw)
                self.assertEqual(body["env"], "dev")
                self.assertEqual(body["key"], "db_url")
                self.assertEqual(body["value"], "postgres://localhost/dev")

        self._run(_test())

    # -- resource 404 --------------------------------------------------------

    def test_resource_read_missing_key_raises(self) -> None:
        """Reading a missing key raises a ClientError (resource not found)."""
        from anodize_mcp import ClientError

        async def _test() -> None:
            async with Client(transport=FastMCPTransport(self.mcp)) as client:
                with self.assertRaises(ClientError):
                    await client.read_resource("config://dev/nonexistent_key")

        self._run(_test())

    # -- tool call with valid scope ------------------------------------------

    def test_set_config_with_write_scope(self) -> None:
        """set_config succeeds when the session carries the 'write' scope."""

        async def _test() -> None:
            async with Client(transport=FastMCPTransport(self.mcp)) as client:
                # Inject scopes into the session state via the internal setup tool.
                await client.call_tool("_set_test_scopes", {"scopes": "read write"})

                result = await client.call_tool(
                    "set_config",
                    {"env": "dev", "key": "new_key", "value": "new_value"},
                )
                self.assertFalse(result.is_error)
                body = json.loads(result.text)
                self.assertEqual(body["status"], "ok")
                self.assertEqual(body["key"], "new_key")

            self.assertEqual(self._store["dev"]["new_key"], "new_value")

        self._run(_test())

    # -- masked errors don't leak internal details ---------------------------

    def test_masked_errors_do_not_leak_internals(self) -> None:
        """With mask_error_details=True, tool errors hide the original exception message."""
        from anodize_mcp import ClientError

        async def _test() -> None:
            # Inject read-only scope; set_config requires 'write'.
            async with Client(transport=FastMCPTransport(self.mcp)) as client:
                await client.call_tool("_set_test_scopes", {"scopes": "read"})

                with self.assertRaises(ClientError) as cm:
                    await client.call_tool(
                        "set_config",
                        {"env": "dev", "key": "k", "value": "v"},
                    )
                error_message = str(cm.exception)
                # mask_error_details replaces the original message with a generic one.
                self.assertIn("Error calling tool", error_message)
                self.assertNotIn("write scope required", error_message)

        self._run(_test())

    # -- delete_config with write scope --------------------------------------

    def test_delete_config_with_write_scope(self) -> None:
        """delete_config removes a key when the session carries the 'write' scope."""

        async def _test() -> None:
            async with Client(transport=FastMCPTransport(self.mcp)) as client:
                await client.call_tool("_set_test_scopes", {"scopes": "read write"})

                result = await client.call_tool(
                    "delete_config",
                    {"env": "dev", "key": "log_level"},
                )
                self.assertFalse(result.is_error)
                body = json.loads(result.text)
                self.assertEqual(body["status"], "deleted")

            self.assertNotIn("log_level", self._store["dev"])

        self._run(_test())


if __name__ == "__main__":
    unittest.main()
