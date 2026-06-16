import base64
import hashlib
import hmac
import json
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from anodize_mcp import (
    AnodizeMCP,
    Context,
    JWTVerifier,
    StaticTokenVerifier,
    get_access_token,
)
from anodize_mcp.transports.http import _make_handler, _Manager


def make_hs256(claims, secret="s3cr3t"):
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    head = seg({"alg": "HS256", "typ": "JWT"})
    body = seg(claims)
    signing = f"{head}.{body}".encode()
    sig = (
        base64.urlsafe_b64encode(hmac.new(secret.encode(), signing, hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    return f"{head}.{body}.{sig}"


class StaticTokenVerifierTest(unittest.TestCase):
    def test_valid_and_unknown(self):
        v = StaticTokenVerifier({"tok": {"client_id": "c", "scopes": ["read"]}})
        access = v.verify_token("tok")
        self.assertEqual(access.client_id, "c")
        self.assertEqual(access.scopes, ["read"])
        self.assertIsNone(v.verify_token("nope"))

    def test_expired(self):
        v = StaticTokenVerifier({"tok": {"expires_at": time.time() - 1}})
        self.assertIsNone(v.verify_token("tok"))


class JWTVerifierHS256Test(unittest.TestCase):
    def setUp(self):
        self.v = JWTVerifier(secret="s3cr3t", issuer="https://idp", audience="anodize")

    def test_valid(self):
        token = make_hs256(
            {
                "iss": "https://idp",
                "aud": "anodize",
                "sub": "u1",
                "scope": "a b",
                "exp": time.time() + 60,
            }
        )
        access = self.v.verify_token(token)
        self.assertEqual(access.subject, "u1")
        self.assertEqual(access.scopes, ["a", "b"])

    def test_bad_signature(self):
        token = make_hs256({"iss": "https://idp", "aud": "anodize"}, secret="wrong")
        self.assertIsNone(self.v.verify_token(token))

    def test_expired(self):
        token = make_hs256({"iss": "https://idp", "aud": "anodize", "exp": time.time() - 1})
        self.assertIsNone(self.v.verify_token(token))

    def test_issuer_and_audience_mismatch(self):
        self.assertIsNone(
            self.v.verify_token(
                make_hs256({"iss": "evil", "aud": "anodize", "exp": time.time() + 60})
            )
        )
        self.assertIsNone(
            self.v.verify_token(
                make_hs256({"iss": "https://idp", "aud": "other", "exp": time.time() + 60})
            )
        )

    def test_malformed(self):
        self.assertIsNone(self.v.verify_token("not-a-jwt"))


class HttpAuthTest(unittest.TestCase):
    def setUp(self):
        mcp = AnodizeMCP("auth", auth=StaticTokenVerifier({"good": {"scopes": ["use"]}}))

        @mcp.tool
        def whoami(ctx: Context) -> str:
            tok = ctx.access_token
            assert get_access_token() is tok
            return tok.token if tok else "none"

        self.mcp = mcp
        self.manager = _Manager(server=mcp, endpoint="/mcp", allowed_origins=None, stateless=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.manager))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, token=None):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/mcp", data=json.dumps(body).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, dict(r.headers), json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), None

    def test_missing_token_401(self):
        status, headers, _ = self.post()
        self.assertEqual(status, 401)
        self.assertIn("WWW-Authenticate", headers)

    def test_invalid_token_401(self):
        self.assertEqual(self.post("garbage")[0], 401)

    def test_valid_token_200_and_context(self):
        status, _, body = self.post("good")
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["content"][0]["text"], "good")

    def test_client_fails_fast_without_token(self):
        import asyncio
        import time

        from anodize_mcp import Client, McpError

        async def main():
            start = time.monotonic()
            with self.assertRaises(McpError) as caught:
                async with Client(f"http://127.0.0.1:{self.port}/mcp"):
                    pass
            return time.monotonic() - start, str(caught.exception)

        elapsed, message = asyncio.run(main())
        # The 401 must resolve the request immediately, not starve it until
        # the client timeout.
        self.assertLess(elapsed, 5.0)
        self.assertIn("401", message)

    def test_insufficient_scope_403(self):
        self.mcp.auth = StaticTokenVerifier(
            {"good": {"scopes": ["use"]}}, required_scopes=["admin"]
        )
        self.assertEqual(self.post("good")[0], 403)


class JWTVerifierJWKSCacheTest(unittest.TestCase):
    """JWKS cache must expire after cache_ttl seconds and re-fetch."""

    def _make_fake_jwks_urlopen(self, call_counter):
        """Return a urlopen mock that increments call_counter on each real fetch."""

        class _FakeResponse:
            def __init__(self):
                self.data = json.dumps({"keys": []}).encode()

            def read(self):
                return self.data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        import contextlib

        @contextlib.contextmanager
        def fake_urlopen(url, timeout=None):
            call_counter.append(1)
            yield _FakeResponse()

        return fake_urlopen

    def test_cache_is_used_within_ttl(self):
        counter = []
        v = JWTVerifier(jwks_uri="https://example.com/.well-known/jwks.json", cache_ttl=3600)
        fake_open = self._make_fake_jwks_urlopen(counter)

        with unittest.mock.patch("urllib.request.urlopen", fake_open):
            v._load_jwks()
            v._load_jwks()
            v._load_jwks()

        self.assertEqual(len(counter), 1, "should only fetch once within TTL")

    def test_cache_expires_after_ttl(self):
        counter = []
        v = JWTVerifier(jwks_uri="https://example.com/.well-known/jwks.json", cache_ttl=60)
        fake_open = self._make_fake_jwks_urlopen(counter)

        times = [1000.0, 1010.0, 1070.0]  # within TTL, within TTL, past 60s TTL
        with (
            unittest.mock.patch("urllib.request.urlopen", fake_open),
            unittest.mock.patch("time.monotonic", side_effect=times),
        ):
            v._load_jwks()  # t=1000, fetches
            v._load_jwks()  # t=1010, cache hit (10s < 60s)
            v._load_jwks()  # t=1070, re-fetches (70s > 60s)

        self.assertEqual(len(counter), 2, "should re-fetch once TTL is exceeded")

    def test_cache_ttl_zero_always_fetches(self):
        counter = []
        v = JWTVerifier(jwks_uri="https://example.com/.well-known/jwks.json", cache_ttl=0)
        fake_open = self._make_fake_jwks_urlopen(counter)

        with unittest.mock.patch("urllib.request.urlopen", fake_open):
            v._load_jwks()
            v._load_jwks()

        self.assertEqual(len(counter), 2, "cache_ttl=0 should always re-fetch")


class NoAuthTest(unittest.TestCase):
    def test_no_token_outside_request(self):
        self.assertIsNone(get_access_token())


if __name__ == "__main__":
    unittest.main()
