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

Targets the core MCP protocol surface anodize implements: tools, resources,
prompts, server, client, and middleware. FastMCP 3.x features anodize does not
claim (OpenAPI generation, OAuth proxy, task queues, server mounting, the CLI,
provider integrations) are out of scope.

Two categories of FastMCP test fail by design and will not be made to pass:

- Tests that assert `isinstance(x, mcp.types.*)` or coerce pydantic models.
  anodize returns plain dicts and validates with a stdlib coercer; depending on
  the `mcp` SDK or pydantic would pull in the Rust toolchain anodize exists to
  avoid. Attribute access (`tool.name`, `result.content[0].text`) works on both.
- Timing- and concurrency-based integration tests, and tests of FastMCP's own
  internal helpers (e.g. `_parse_call_tool_result`).

## Status

Run against FastMCP 3.4.2 at the pinned SHA. The CI gate runs these files in
full (excluding timing/concurrency tests, which are not deterministic):

| FastMCP test file | Result |
|---|---|
| `tests/prompts/test_prompt.py` | 50/50 |
| `tests/resources/test_function_resources.py` | 20/20 |
| `tests/resources/test_file_resources.py` | 11/11 |
| `tests/tools/tool/test_output_schema.py` | 56/56 |
| `tests/server/middleware/test_rate_limiting.py` | 22/22 |
| `tests/server/middleware/test_timing.py` | 13/13 |

Across the full core suite (tools, resources, prompts, server, client, all
middleware; excluding the non-deterministic integration/retry tests) the figure
is 408 of 527 (77%). The remaining 119 are dominated by:

- The two out-of-scope categories above (`isinstance(x, mcp.types.*)`, pydantic
  model coercion, FastMCP-internal helpers). Substituting `mcp.types.*` in the
  harness is not viable: it breaks FastMCP's own content pipeline, which builds
  real `mcp.types` objects. `mcp.McpError` is safely substituted, so error-path
  `isinstance` checks do resolve.
- FastMCP 3.x surface anodize does not implement: providers, server mounting,
  task queues, the `FunctionTool`/`Tool.from_function` object model, and version
  identity (`mcp.name` starts with `FastMCP-`).
- The session-scoped `Context` state store, which would require aliasing
  FastMCP's `Context` and reimplementing its state model.
