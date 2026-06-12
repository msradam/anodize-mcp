<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/msradam/anodize-mcp/main/assets/logo-dark.svg">
    <img alt="AnodizeMCP" src="https://raw.githubusercontent.com/msradam/anodize-mcp/main/assets/logo-light.svg" width="110" height="110">
  </picture>
</p>

<h1 align="center">AnodizeMCP</h1>

A lightweight, pure-Python implementation of the [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server framework. **No Rust and no compiled extensions**, so it installs and runs wherever a Rust toolchain cannot.

The official MCP SDK and FastMCP both depend on `pydantic`, which depends on `pydantic-core` (compiled Rust). That dependency has no prebuilt wheel for many targets and cannot be compiled where a Rust toolchain is unavailable or disallowed. AnodizeMCP fills that gap: it implements the same FastMCP-style API using only the standard library plus pure-Python dependencies. The server class is `AnodizeMCP`, also exported as `FastMCP` so switching later is a one-line import change.

The constraint is specifically **Rust**, not dependencies. AnodizeMCP's only runtime dependency is `uvicorn` (pure Python, no compiled code), which provides a production-grade HTTP server. The MCP core itself is standard library only.

## Why it exists

The barrier is specific: a Rust-based package with no prebuilt wheel for your platform and no way to build one because there is no Rust toolchain. `pydantic-core` (under both FastMCP and the official SDK) is the clearest case. AnodizeMCP and its dependencies contain no Rust and no compiled code, so they install where those cannot:

- **z/OS** (the sharpest case): IBM's Open Enterprise SDK for Python bundles `cryptography` (3.3.2, pre-Rust) and `numpy`, but there is no `rustc` targeting z/OS, so `pydantic-core` cannot be built or installed. AnodizeMCP and `uvicorn` install clean.
- **Linux on IBM Z (s390x), AIX, Solaris/illumos, the BSDs, Cygwin** where prebuilt wheels are often absent (on s390x Linux you can build from source, slowly; AnodizeMCP skips the build).
- **Exotic or older CPU architectures**: ppc64le, riscv64, ARMv6/v7, mips, sparc.
- **WebAssembly** (Pyodide, PyScript) and **locked-down or air-gapped build environments** with no compiler, no network, or a no-Rust policy.

| | Dependencies | Rust / compiled code | Installs without a build toolchain |
|---|---|---|---|
| Official `mcp` SDK | pydantic, anyio, httpx, starlette, uvicorn | pydantic-core (Rust) | no |
| FastMCP | pydantic + many | pydantic-core (Rust) | no |
| `pure-mcp` | pydantic, anyio, httpx, jsonschema | pydantic-core (Rust) | no |
| **AnodizeMCP** | uvicorn (pure Python) | **none** | **yes** |

## Install

```sh
pip install anodize-mcp
```

Requires Python 3.9 or newer. The only runtime dependency is `uvicorn` (pure Python). Verified on z/OS: both install clean.

## Quickstart

```python
from anodize_mcp import AnodizeMCP

mcp = AnodizeMCP("demo", instructions="A small demo server.")

@mcp.tool
def add(a: int, b: int) -> int:
    "Add two numbers."
    return a + b

@mcp.resource("config://app")
def config() -> str:
    return '{"theme": "dark"}'

@mcp.prompt
def review(code: str) -> str:
    return f"Review this code:\n\n{code}"

if __name__ == "__main__":
    mcp.run()  # stdio transport
```

Tool input schemas are generated from type hints. Supported types include the primitives, `Optional`/`Union`, `list`/`dict`/`set`/`tuple`, `Literal`, `Enum`, dataclasses, and stdlib types (`datetime`, `date`, `UUID`, `Decimal`). Arguments are validated and coerced at call time. Constraints come from `Annotated`:

```python
from typing import Annotated
from anodize_mcp import Field

@mcp.tool
def scale(factor: Annotated[float, Field(ge=0, le=10, description="0 to 10")]) -> float:
    return factor * 2
```

A dataclass return value produces an `outputSchema` and `structuredContent` automatically.

## Drop-in compatibility with FastMCP

The intended workflow: build your server with AnodizeMCP today on a platform
where Rust is unavailable, and if Rust later becomes available, switch to
FastMCP by changing one import line.

The class is exported as `FastMCP`, and the decorator and `Context` APIs match
FastMCP's:

```python
from anodize_mcp import FastMCP, Context        # later: from fastmcp import FastMCP

mcp = FastMCP("demo", instructions="...")

@mcp.tool
async def summarize(text: str, ctx: Context) -> str:
    await ctx.info("summarizing")
    result = await ctx.sample(text, system_prompt="Be concise.")
    return result.text
```

To stay portable both directions, write FastMCP's async style: `async def`
handlers and `await ctx.*`. AnodizeMCP's `Context` methods are awaitable for this
reason (they also work without `await`, as a convenience, but that sync-only
form does not port back to FastMCP).

This is checked against the official `mcp` reference client: the same client
driving a FastMCP server and an AnodizeMCP server (identical bodies, only the
import differs) sees matching tool descriptions, input schemas
(`additionalProperties: false`, parameter `default`s), and structured output
(scalar returns wrapped as `{"result": value}` with an `outputSchema`, like
FastMCP).

### What ports unchanged

- `FastMCP(name, instructions=..., version=..., lifespan=..., icons=..., website_url=..., on_duplicate=..., mask_error_details=..., auth=...)`; `@mcp.tool`, `@mcp.resource`, `@mcp.prompt` with `name`/`title`/`description`/`annotations`/`tags`; `add_tool`/`add_resource`/`add_prompt`
- `@mcp.custom_route(path, methods=...)`, `mcp.add_middleware(...)`, `mcp.list_tools/list_resources/list_prompts/get_tool/get_prompt/call_tool/render_prompt`, `mcp.disable_tool/enable_tool`
- `ctx: Context` injection; `await ctx.debug/info/notice/warning/error(...)`, `ctx.log(message, level=...)`, `report_progress`, `read_resource`, `list_resources`, `list_prompts`, `get_prompt`, `get_state/set_state/delete_state`, `send_notification`, `sample` (result `.text`), `elicit(message, dataclass)` (result `.action`/`.data`), `list_roots`; `ctx.session_id`/`client_id`/`request_id`/`fastmcp`/`transport`/`request_context.lifespan_context`/`access_token`
- Parameter types: primitives, `Optional`/`Union`/`Literal`/`Enum`, `list`/`dict`/`set`/`tuple`, `datetime`/`date`/`UUID`/`Decimal`, dataclasses, **`pydantic.BaseModel`** (built via the server's own pydantic), and constraints via either AnodizeMCP's `Field` or **`pydantic.Field`/`annotated_types`** (`Annotated[int, Field(ge=0)]` validates)
- Return types: `str`, numbers, `dict`, `list`, dataclasses, pydantic models, `bytes`, `None`, content blocks (`TextContent`, `ImageContent`, ...), the `Image`/`File` helpers, and `ToolResult`
- `mcp.run(transport="stdio"|"http", host=..., port=...)`
- `fastmcp.Client` in-memory, over stdio, and over Streamable HTTP (`Client("http://host/mcp")`)

### What is not implemented as of now (use the alternative)

These are not fundamental limits, just features not built yet.

| FastMCP feature | On AnodizeMCP |
|---|---|
| `from fastmcp.exceptions import ToolError` | `from anodize_mcp import ToolError` (one import line) |
| `@mcp.custom_route` handler body | Decorator and `handler(request) -> response` shape match; the request/response objects are AnodizeMCP's, not Starlette's |
| OAuth 2.1 server flow / hosted-IdP provider wrappers | Not implemented as of now; verify externally-issued tokens with `auth=` instead |
| `mcp.mount` / `import_server` / `as_proxy` / `from_openapi` | Not implemented as of now (server composition and generation) |
| `@mcp.tool(task=True)` background tasks | Not implemented as of now |
| the `fastmcp` CLI | Not implemented as of now |
| `transport="sse"` (deprecated) | Raises a clear error; use `"http"` |

The other expected difference is the negotiated protocol revision: AnodizeMCP
implements `2025-06-18` and negotiates down gracefully if the client offers a
newer one.

### Conformance against FastMCP's own test suite

As a parity check, FastMCP's tests can be pointed at AnodizeMCP by aliasing
`fastmcp.FastMCP` and `fastmcp.Client` to the AnodizeMCP equivalents (pytest loads
plugins before test modules, so `from fastmcp import FastMCP` then resolves to
AnodizeMCP). Run against FastMCP 3.x's `tests/tools`, `tests/resources`, and
`tests/prompts` with only that alias and the optional `name`, **554 of 606 pass**.

This is the tool/resource/prompt behavioral subset, not FastMCP's full suite
(which also covers the CLI, OpenAPI generation, telemetry, and other features
not implemented as of now). The 52 failures fall into three groups: features
AnodizeMCP does not implement yet (per-tool `timeout`, the thread-offload API, tool
transformation), tests that assert on the official `mcp` SDK result objects
(AnodizeMCP's client returns dicts), and a small number of edge cases under
review.

## Protocol coverage

Implements MCP protocol revision `2025-06-18`.

| Area | Methods |
|---|---|
| Lifecycle | `initialize`, `notifications/initialized`, `ping` |
| Tools | `tools/list` (paginated), `tools/call`, `notifications/tools/list_changed` |
| Resources | `resources/list`, `resources/read`, `resources/templates/list`, `resources/subscribe`, `resources/unsubscribe`, `notifications/resources/updated`, `notifications/resources/list_changed` |
| Prompts | `prompts/list`, `prompts/get`, `notifications/prompts/list_changed` |
| Completions | `completion/complete` |
| Logging | `logging/setLevel`, `notifications/message` |
| Progress | `notifications/progress` |
| Sampling | `sampling/createMessage` (server to client) |
| Elicitation | `elicitation/create` (server to client) |
| Roots | `roots/list` (server to client) |

## Context

A handler receives a `Context` by declaring a parameter annotated as `Context`. It is excluded from the input schema and injected at call time.

```python
from anodize_mcp import Context

@mcp.tool
def review(code: str, ctx: Context) -> str:
    ctx.info("starting review")
    result = ctx.sample(f"Review:\n{code}", system_prompt="Be terse.")
    return result.text
```

Context provides:

- Logging: `ctx.debug/info/notice/warning/error(...)`. The default level is `info`; the client narrows it with `logging/setLevel`.
- Progress: `ctx.report_progress(progress, total=..., message=...)`.
- Reading resources: `ctx.read_resource(uri)`.
- Sampling: `ctx.sample(messages, system_prompt=..., max_tokens=...)` asks the client's LLM. `messages` is a string, a single message dict, or a list of either.
- Elicitation: `ctx.elicit(message, schema)` asks the user, where `schema` is a JSON Schema dict or a dataclass.
- Roots: `ctx.list_roots()` returns the client's filesystem roots.

`sample`, `elicit`, and `list_roots` are server-to-client requests: the handler blocks until the client responds. They require the client to have declared the matching capability, otherwise they raise an error.

A tool can return a string (text), a dataclass (structured output), or content blocks built directly:

```python
from anodize_mcp import TextContent, ImageContent

@mcp.tool
def render() -> list:
    return [TextContent(text="caption"), ImageContent.from_bytes(png_bytes, "image/png")]
```

## Transports

stdio (default), newline-delimited UTF-8 JSON:

```python
mcp.run()                       # or mcp.run("stdio")
mcp.run("stdio", max_workers=8) # thread pool size for concurrent handlers
```

Streamable HTTP, a single endpoint (default `/mcp`):

```python
mcp.run("http", host="127.0.0.1", port=8000)  # serves POST/GET on /mcp
```

This runs under **uvicorn**, so production concerns (keep-alive and read timeouts, request size limits, graceful shutdown, signal handling) are handled by uvicorn rather than reimplemented. The uvicorn config matches FastMCP's defaults (`timeout_graceful_shutdown=2`, `lifespan="on"`) and accepts a passthrough:

```python
mcp.run(
    "http",
    host="0.0.0.0",
    port=8000,
    path="/mcp",
    log_level="info",
    allowed_origins={"localhost", "127.0.0.1"},   # or {"*"} to disable the Origin check
    stateless=False,                              # True skips session tracking
    uvicorn_config={"timeout_keep_alive": 30},    # any uvicorn.Config setting
)
```

For your own server (gunicorn, hypercorn, behind a reverse proxy, or mounted in a larger app), get the ASGI app directly:

```python
app = mcp.asgi_app(path="/mcp")
# uvicorn anodize_app:app --host 0.0.0.0 --port 8000
```

If uvicorn is somehow not installed, `run("http", ...)` falls back to the standard-library `http.server`. The HTTP transport validates the `Origin` header (localhost only by default), tracks sessions with `Mcp-Session-Id`, and serves server-to-client messages (progress, logging, sampling) over a GET SSE stream.

## Completions

Register argument completers per prompt or resource template:

```python
@mcp.complete_prompt("review")
def complete(argument: str, value: str) -> list[str]:
    if argument == "language":
        return [x for x in ("python", "rust", "go") if x.startswith(value)]
    return []
```

A completer may take a third `context` argument (the already-entered values) and may return a `CompletionResult(values=..., total=..., has_more=...)` for explicit totals.

## Authentication

Authentication applies to the HTTP transport. stdio relies on the operating system process boundary (the server runs under the identity of whoever launched it), so it has no token layer.

The model matches FastMCP: pass a token verifier to the server, the HTTP layer reads `Authorization: Bearer <token>`, and a handler reads the result with `get_access_token()` or `ctx.access_token`. Issuing tokens is left to an external identity provider.

```python
from anodize_mcp import AnodizeMCP, Context, StaticTokenVerifier, get_access_token

mcp = AnodizeMCP(
    "demo",
    auth=StaticTokenVerifier({"dev-token": {"client_id": "cli", "scopes": ["read"]}}),
)

@mcp.tool
def whoami(ctx: Context) -> str:
    token = ctx.access_token            # also: get_access_token()
    return f"{token.client_id} {token.scopes}"
```

A request with no token gets `401` and a `WWW-Authenticate: Bearer` header; an invalid token gets `401`; a valid token missing a required scope gets `403`.

`JWTVerifier` validates JSON Web Tokens. HS256/384/512 (HMAC) use only the standard library; RS256/384/512 use the `cryptography` package if it is importable and raise a clear error otherwise.

```python
from anodize_mcp import JWTVerifier

# Symmetric (HS256), standard library only
mcp = AnodizeMCP("demo", auth=JWTVerifier(secret="...", issuer="https://idp", audience="my-api"))

# Asymmetric, keys fetched from the IdP (needs cryptography for RS256)
mcp = AnodizeMCP("demo", auth=JWTVerifier(jwks_uri="https://idp/.well-known/jwks.json",
                                          issuer="https://idp", audience="my-api"))
```

The verifier is any object with `verify_token(token: str) -> AccessToken | None` and an optional `required_scopes`, so a custom verifier (LDAP, RACF, a database lookup) drops in. The OAuth 2.1 authorization-server flow and the hosted-IdP provider wrappers are not implemented as of now; point those at your IdP and verify the tokens here.

## Lifespan

Run setup and teardown around the server with `lifespan`, a context manager whose yielded value is available to every handler. Synchronous and asynchronous context managers both work; synchronous resources are the clean case, an async resource bound to an event loop carries the usual cross-loop caveat since each handler runs on its own loop.

```python
import contextlib

@contextlib.contextmanager
def lifespan(server):
    pool = open_connection_pool()
    try:
        yield {"pool": pool}
    finally:
        pool.close()

mcp = AnodizeMCP("demo", lifespan=lifespan)

@mcp.tool
def query(sql: str, ctx: Context) -> list:
    return ctx.request_context.lifespan_context["pool"].run(sql)
```

## Custom routes

Register handlers at arbitrary HTTP paths for health checks, metrics, or OAuth callbacks. Custom routes bypass the MCP auth and Origin checks (HTTP transport only). A handler returns a `Response`, a `(status, body)` tuple, a `dict`/`list` (JSON), a `str`, or `bytes`.

```python
@mcp.custom_route("/health", methods=["GET"])
def health(request):
    return {"status": "ok"}
```

The decorator and the `handler(request) -> response` shape match FastMCP; the request and response objects are AnodizeMCP's own (no Starlette dependency).

## Middleware

`add_middleware` wraps a chain of hooks around request dispatch. Hook names and the `(context, call_next)` shape match FastMCP. `on_message` runs for every request; per-operation hooks (`on_call_tool`, `on_read_resource`, `on_get_prompt`, ...) run nested inside for the matching method.

```python
from anodize_mcp import Middleware

class Timing(Middleware):
    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        return result

mcp.add_middleware(Timing())
```

## Testing with the in-memory client

`Client` connects to a server with no network in between, the way FastMCP's test client does, so a test exercises the real MCP request path. Pass the server object for an in-process connection, or a command list to launch a subprocess over stdio.

```python
import asyncio
from anodize_mcp import AnodizeMCP, Client

mcp = AnodizeMCP("demo")

@mcp.tool
def add(a: int, b: int) -> int:
    return a + b

async def main():
    async with Client(mcp) as client:                 # in-process
        result = await client.call_tool("add", {"a": 1, "b": 2})
        assert result.data == {"result": 3}

    async with Client(["python", "server.py"]) as client:  # subprocess over stdio
        tools = await client.list_tools()

asyncio.run(main())
```

The client has `list_tools`, `call_tool`, `list_resources`, `read_resource`, `list_resource_templates`, `list_prompts`, `get_prompt`, `complete`, and `ping`, and follows pagination automatically. Pass `sampling_handler`, `elicitation_handler`, and `roots` to answer the server's `ctx.sample`/`ctx.elicit`/`ctx.list_roots` calls; the client advertises the matching capability and the round-trip runs in process.

```python
async with Client(mcp, sampling_handler=lambda params: "the model reply") as client:
    result = await client.call_tool("summarize", {"text": "..."})
```

## Dynamic changes

Registries can change at runtime. Removing an item or calling a notify method broadcasts the corresponding `list_changed` notification to connected clients:

```python
mcp.remove_tool("old_tool")        # broadcasts notifications/tools/list_changed
mcp.notify_resource_updated(uri)   # to clients subscribed to that uri
```

## Pagination

List endpoints page automatically when a registry exceeds `page_size`:

```python
mcp = AnodizeMCP("demo", page_size=100)
```

Clients receive a `nextCursor` and echo it back. The cursor is opaque.

## Development

```sh
uv venv && uv pip install -e ".[dev]"
python -m unittest discover -s tests
ruff format . && ruff check . && mypy
```

The test suite uses only the standard library `unittest`.

## License

MIT. See [LICENSE](LICENSE).

## Credits

Diode logo by [Eucalyp](https://thenounproject.com/Eucalyp/) from the [Noun Project](https://thenounproject.com/icon/diode-3160468/).
