import io
import json
import unittest

from anodize_mcp import Anodize
from anodize_mcp.transports.stdio import serve_stdio


def make_server() -> Anodize:
    mcp = Anodize("stdio-test")

    @mcp.tool
    def echo(text: str) -> str:
        return text

    return mcp


def encode(*messages) -> bytes:
    return b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in messages)


def run(messages) -> list:
    mcp = make_server()
    out = io.BytesIO()
    serve_stdio(mcp, in_stream=io.BytesIO(encode(*messages)), out_stream=out)
    lines = out.getvalue().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class StdioTest(unittest.TestCase):
    def test_request_response_roundtrip(self):
        responses = run(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "ping"}},
                },
            ]
        )
        # initialize response + tools/call response; the notification yields nothing.
        # Handlers run on a thread pool, so responses are matched by id, not order.
        by_id = {r["id"]: r for r in responses}
        self.assertEqual(set(by_id), {1, 2})
        self.assertIn("protocolVersion", by_id[1]["result"])
        self.assertEqual(by_id[2]["result"]["content"][0]["text"], "ping")

    def test_parse_error(self):
        mcp = make_server()
        out = io.BytesIO()
        serve_stdio(mcp, in_stream=io.BytesIO(b"not json\n"), out_stream=out)
        resp = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(resp["error"]["code"], -32700)

    def test_utf8_roundtrip(self):
        responses = run(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "café ✨ 日本"}},
                },
            ]
        )
        self.assertEqual(responses[0]["result"]["content"][0]["text"], "café ✨ 日本")

    def test_blank_lines_skipped(self):
        responses = run(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            ]
        )
        self.assertEqual(responses[0]["result"], {})


if __name__ == "__main__":
    unittest.main()
