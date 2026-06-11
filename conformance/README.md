# FastMCP conformance

Runs FastMCP's own test suite against AnodizeMCP to verify behavioral parity.
`aliasplugin.py` rebinds FastMCP's public classes and feature modules to the
anodize equivalents (pytest loads `-p` plugins before test modules), so
`from fastmcp import FastMCP` and
`from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware`
resolve to anodize's implementations. FastMCP's assertions become the acceptance
criteria.

This requires `fastmcp` installed, which needs Rust, so it runs only in CI on
Linux and never on z/OS. The packaged library never imports it.

## Run locally

```sh
git clone https://github.com/jlowin/fastmcp /tmp/fastmcp-src
PYTHONPATH=src:conformance uv run --with "fastmcp==3.4.2" --with pytest --with pytest-asyncio \
  --with opentelemetry-sdk --with opentelemetry-api --with dirty-equals --with inline-snapshot \
  python -m pytest -p aliasplugin \
  /tmp/fastmcp-src/tests/server/middleware/test_rate_limiting.py -k "not Integration" \
  -o asyncio_mode=auto
```

## Scope

Targets FastMCP's deterministic tests. Out of scope: their timing- and
concurrency-based integration tests, and tests that assert on the official `mcp`
SDK result objects or FastMCP transport internals (which anodize does not
reimplement).

## Status

| FastMCP test file | Result against anodize |
|---|---|
| `tests/server/middleware/test_rate_limiting.py` (deterministic classes) | 22/22 pass |
