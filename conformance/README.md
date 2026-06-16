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
claim are out of scope: OpenAPI/FastAPI generation (`from_fastapi`), OAuth proxy
and `as_proxy`/`FastMCPProxy`, task queues (`task=True`), server mounting
(`mount`), the CLI (`fastmcp run`), provider integrations, and the component
transforms (`PromptsAsTools`, `ResourcesAsTools`, search). Returning a tool's
pydantic-model param/result, the `Image`/`File` content helpers, and `ToolResult`
ARE supported (anodize uses the server's own pydantic when present).

Two categories of FastMCP test fail by design and will not be made to pass:

- Tests that assert `isinstance(x, mcp.types.*)`. anodize returns plain dicts;
  depending on the `mcp` SDK would pull in the Rust toolchain anodize exists to
  avoid. Attribute access (`tool.name`, `result.content[0].text`) works on both.
- Timing- and concurrency-based integration tests, and tests of FastMCP's own
  internal helpers (e.g. `_parse_call_tool_result`).

## Status

Run against FastMCP 3.4.2 at the pinned SHA. The CI gate runs these files in
full (excluding timing/concurrency tests, which are not deterministic):

| FastMCP test file | Filter | Result |
|---|---|---|
| `tests/prompts/test_prompt.py` | (none) | 50/50 |
| `tests/resources/test_function_resources.py` | (none) | 20/20 |
| `tests/resources/test_file_resources.py` | (none) | 11/11 |
| `tests/tools/tool/test_output_schema.py` | (none) | 56/56 |
| `tests/server/middleware/test_rate_limiting.py` | (none) | 22/22 |
| `tests/server/middleware/test_timing.py` | (none) | 13/13 |
| `tests/server/middleware/test_error_handling.py` | `not Integration and not Retry and not transform_tool_error` | 17/17 |
| `tests/client/client/test_error_handling.py` | `(TestErrorHandling or TestCallToolRaiseOnError) and not validation_errors and not general_tool_exceptions and not specific_tool_errors_are_sent` | 8/8 |

The broader core-suite figure is measured against a pinned file set so it can
be reproduced exactly. The set is FastMCP's `tests/tools/tool`,
`tests/resources`, `tests/prompts`, `tests/server/middleware`,
`tests/server/test_server.py`, and `tests/client/client`, with
`-k "not Integration and not Retry"` and a per-test timeout:

```sh
PYTHONPATH=src:conformance uv run --no-project --python 3.12 \
  --with "fastmcp==3.4.2" --with pytest --with pytest-asyncio --with pytest-timeout \
  --with opentelemetry-sdk --with opentelemetry-api --with dirty-equals --with inline-snapshot \
  python -m pytest -p aliasplugin -W ignore -o asyncio_mode=auto \
  -k "not Integration and not Retry and not streamable_http_clients" --timeout=30 \
  /tmp/fastmcp-src/tests/tools/tool /tmp/fastmcp-src/tests/resources \
  /tmp/fastmcp-src/tests/prompts /tmp/fastmcp-src/tests/server/middleware \
  /tmp/fastmcp-src/tests/server/test_server.py /tmp/fastmcp-src/tests/client/client
```

The current figure on that set is 605 of 762 (79%); 60 further tests are
deselected by the `-k` filter. The remaining failures are dominated by:

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
- Client-side OAuth and the SSE transport (`tests/client/client/test_auth.py`),
  which anodize does not implement; verify externally-issued tokens server-side
  instead.
