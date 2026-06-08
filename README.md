# anodize-mcp

A lightweight, pure-Python implementation of the [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server framework. Standard library only, zero third-party dependencies, and no Rust toolchain required.

The official MCP SDK and FastMCP both depend on `pydantic`, which depends on `pydantic-core` (compiled Rust). That dependency has no prebuilt wheel for many targets and cannot be compiled where a Rust toolchain is unavailable or disallowed. anodize fills that gap: it implements the same FastMCP-style API using only `json`, `http.server`, `threading`, `dataclasses`, and `typing` from the standard library. The server class is `AnodizeMCP`, also exported as `FastMCP` so switching later is a one-line import change.

## Why it exists

anodize installs and runs where the Rust-backed alternatives cannot:

- IBM mainframes: z/OS, and Linux on Z (s390x), where `pydantic-core` wheels are not published
- Other commercial Unix: AIX, Solaris/illumos, the BSDs, Cygwin
- Exotic or older CPU architectures: ppc64le, riscv64, ARMv6/v7, mips, sparc
- musl + ARM (Alpine on ARM), where the manylinux wheel does not match and source builds need Rust
- WebAssembly runtimes (Pyodide, PyScript)
- Locked-down or air-gapped build environments with no compiler, no network, or a policy against installing a Rust toolchain

| | Third-party deps | Needs Rust | Installs on the targets above |
|---|---|---|---|
| Official `mcp` SDK | pydantic, anyio, httpx, starlette, uvicorn | yes | no |
| FastMCP | pydantic + many | yes | no |
| `pure-mcp` | pydantic, anyio, httpx, jsonschema | yes | no |
| **anodize** | none | **no** | **yes** |

## Install

```sh
pip install anodize-mcp
```

Requires Python 3.9 or newer. There are no other dependencies.

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
handlers and `await ctx.*`. anodize's `Context` methods are awaitable for this
reason (they also work without `await`, as a convenience, but that sync-only
form does not port back to FastMCP). Matching surface:

- `FastMCP(name, instructions=..., version=...)`, `@mcp.tool`, `@mcp.resource`, `@mcp.prompt`
- `ctx: Context` parameter injection
- `await ctx.debug/info/warning/error(...)`, `await ctx.report_progress(...)`
- `await ctx.read_resource(uri)`, `await ctx.sample(...)` (result has `.text`), `await ctx.elicit(message, dataclass)` (result has `.action`/`.data`)
- `await ctx.get_state/set_state/delete_state(...)`
- `mcp.run(transport="stdio"|"http", host=..., port=...)`

Drop-in fidelity is checked against the official `mcp` reference client: the
same client driving a FastMCP server and an AnodizeMCP server (identical bodies,
only the import differs) sees matching tool descriptions, input schemas
(`additionalProperties: false`), and structured output (scalar returns wrapped
as `{"result": value}` with an `outputSchema`, like FastMCP). The one expected
difference is the negotiated protocol revision: AnodizeMCP implements
`2025-06-18` and negotiates down gracefully if the client offers a newer one.

Anything FastMCP-specific not implemented here (middleware, auth providers, the
deprecated `sse` transport, server composition) is a no-op or raises a clear
error rather than silently differing.

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

Streamable HTTP, a single endpoint (default `/mcp`) on the standard-library HTTP server:

```python
mcp.run("http", host="127.0.0.1", port=8000)  # serves POST/GET on /mcp
```

The HTTP transport validates the `Origin` header (localhost only by default), tracks sessions with `Mcp-Session-Id`, and serves server-to-client messages (progress, logging, sampling) over a GET SSE stream. A client that never opens that GET stream will not receive those notifications; queued ones are bounded and drop oldest-first. Options:

```python
mcp.run(
    "http",
    host="127.0.0.1",
    port=8000,
    endpoint="/mcp",
    allowed_origins={"localhost", "127.0.0.1"},  # or {"*"} to disable the check
    stateless=False,                              # True skips session tracking
)
```

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
