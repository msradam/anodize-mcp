import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from anodize_mcp import Anodize
from anodize_mcp.transports.http import _make_handler, _Manager


def make_server() -> Anodize:
    mcp = Anodize("http-test")

    @mcp.tool
    def echo(text: str) -> str:
        return text

    return mcp


class HttpTestBase(unittest.TestCase):
    stateless = False

    def setUp(self):
        self.manager = _Manager(
            server=make_server(),
            endpoint="/mcp",
            allowed_origins=None,
            stateless=self.stateless,
        )
        handler = _make_handler(self.manager)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, body, headers=None, path="/mcp"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            payload = resp.read()
            return resp.status, dict(resp.headers), (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as e:
            payload = e.read()
            return e.code, dict(e.headers), (json.loads(payload) if payload else None)

    def initialize(self):
        status, headers, body = self.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
            }
        )
        return status, headers, body


class HttpStatefulTest(HttpTestBase):
    def test_initialize_assigns_session(self):
        status, headers, body = self.initialize()
        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", headers)
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")

    def test_call_with_session(self):
        _, headers, _ = self.initialize()
        sid = headers["Mcp-Session-Id"]
        status, _, body = self.post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hi"}},
            },
            headers={"Mcp-Session-Id": sid},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["content"][0]["text"], "hi")

    def test_missing_session_rejected(self):
        status, _, _ = self.post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hi"}},
            }
        )
        self.assertEqual(status, 400)

    def test_unknown_session_404(self):
        status, _, _ = self.post(
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"Mcp-Session-Id": "deadbeef"},
        )
        self.assertEqual(status, 404)

    def test_notification_returns_202(self):
        _, headers, _ = self.initialize()
        sid = headers["Mcp-Session-Id"]
        status, _, body = self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Mcp-Session-Id": sid},
        )
        self.assertEqual(status, 202)
        self.assertIsNone(body)

    def test_delete_session(self):
        _, headers, _ = self.initialize()
        sid = headers["Mcp-Session-Id"]
        url = f"http://127.0.0.1:{self.port}/mcp"
        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Mcp-Session-Id", sid)
        resp = urllib.request.urlopen(req, timeout=5)
        self.assertEqual(resp.status, 200)

    def test_bad_origin_rejected(self):
        status, _, _ = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Origin": "http://evil.example.com"},
        )
        self.assertEqual(status, 403)

    def test_localhost_origin_allowed(self):
        status, _, _ = self.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
            },
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(status, 200)

    def test_unsupported_protocol_header(self):
        status, _, _ = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"MCP-Protocol-Version": "1999-01-01"},
        )
        self.assertEqual(status, 400)

    def test_parse_error(self):
        url = f"http://127.0.0.1:{self.port}/mcp"
        req = urllib.request.Request(url, data=b"not json", method="POST")
        req.add_header("Accept", "application/json, text/event-stream")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            body = json.loads(e.read())
            self.assertEqual(body["error"]["code"], -32700)

    def test_wrong_path_404(self):
        status, _, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, path="/wrong")
        self.assertEqual(status, 404)


class HttpStatelessTest(HttpTestBase):
    stateless = True

    def test_no_session_required(self):
        status, _, body = self.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "x"}},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["content"][0]["text"], "x")


if __name__ == "__main__":
    unittest.main()
