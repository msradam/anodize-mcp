# anodize-mcp

A lightweight, pure-Python implementation of the [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server framework. Standard library only, zero third-party dependencies, and no Rust toolchain required.

The official MCP SDK and FastMCP both depend on `pydantic`, which depends on `pydantic-core` (compiled Rust). That dependency has no prebuilt wheel for many targets and cannot be compiled where a Rust toolchain is unavailable or disallowed. anodize fills that gap: it implements the same FastMCP-style API using only `json`, `http.server`, `threading`, `dataclasses`, and `typing` from the standard library. The server class is `AnodizeMCP`, also exported as `FastMCP` so switching later is a one-line import change.

## Why it exists

The barrier is specific: a Rust-based package with no prebuilt wheel for your platform and no way to build one because there is no Rust toolchain. `pydantic-core` (under both FastMCP and the official SDK) is the clearest case. anodize has no compiled dependencies at all, so it installs where those cannot:

- **z/OS** (the sharpest case): IBM's Open Enterprise SDK for Python bundles `cryptography` and `numpy`, but there is no `rustc` targeting z/OS, so `pydantic-core` cannot be built or installed. anodize uses only `json`, `http.server`, `threading`, `dataclasses`, and `typing`.
- **Linux on IBM Z (s390x), AIX, Solaris/illumos, the BSDs, Cygwin** where prebuilt wheels are often absent (on s390x Linux you can build from source, slowly; anodize skips the build).
- **Exotic or older CPU architectures**: ppc64le, riscv64, ARMv6/v7, mips, sparc.
- **WebAssembly** (Pyodide, PyScript) and **locked-down or air-gapped build environments** with no compiler, no network, or a no-Rust policy.

| | Third-party deps | Compiled deps | Installs without a build toolchain |
|---|---|---|---|
| Official `mcp` SDK | pydantic, anyio, httpx, starlette, uvicorn | pydantic-core (Rust) | no |
| FastMCP | pydantic + many | pydantic-core (Rust) | no |
| `pure-mcp` | pydantic, anyio, httpx, jsonschema | pydantic-core (Rust) | no |
| **anodize** | none | none | **yes** |

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
form does not port back to FastMCP).

This is checked against the official `mcp` reference client: the same client
driving a FastMCP server and an AnodizeMCP server (identical bodies, only the
import differs) sees matching tool descriptions, input schemas
(`additionalProperties: false`, parameter `default`s), and structured output
(scalar returns wrapped as `{"result": value}` with an `outputSchema`, like
FastMCP).

### What ports unchanged

- `FastMCP(name, instructions=..., version=...)`; `@mcp.tool`, `@mcp.resource`, `@mcp.prompt` with `name`/`title`/`description`/`annotations`/`tags`; `add_tool`/`add_resource`/`add_prompt`
- `ctx: Context` injection; `await ctx.debug/info/notice/warning/error(...)`, `ctx.log(message, level=...)`, `report_progress`, `read_resource`, `list_resources`, `list_prompts`, `get_prompt`, `get_state/set_state/delete_state`, `sample` (result `.text`), `elicit(message, dataclass)` (result `.action`/`.data`), `list_roots`; `ctx.session_id`/`client_id`/`request_id`
- Parameter types: primitives, `Optional`/`Union`/`Literal`/`Enum`, `list`/`dict`/`set`/`tuple`, `datetime`/`date`/`UUID`/`Decimal`, dataclasses, and constraints via either anodize's `Field` or **`pydantic.Field`/`annotated_types`** (`Annotated[int, Field(ge=0)]` validates)
- Return types: `str`, numbers, `dict`, `list`, dataclasses, `bytes`, `None`, and content blocks (`TextContent`, `ImageContent`, ...)
- `mcp.run(transport="stdio"|"http", host=..., port=...)`

### What does not port (use the alternative, or it is unsupported)

| FastMCP feature | On anodize |
|---|---|
| `pydantic.BaseModel` as a tool parameter | Use a `@dataclass` instead (BaseModel params are the one hard break) |
| `from fastmcp.exceptions import ToolError` | `from anodize_mcp import ToolError` (one import line) |
| `mcp.mount` / `import_server` / server composition | Not supported |
| `@mcp.custom_route`, middleware, auth providers | Not supported |
| `@mcp.tool(task=True)` background tasks | Not supported |
| `transport="sse"` (deprecated) | Raises a clear error; use `"http"` |

The other expected difference is the negotiated protocol revision: AnodizeMCP
implements `2025-06-18` and negotiates down gracefully if the client offers a
newer one.

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
