# AGENTS.md

Handoff guide for an autonomous coding agent working on AnodizeMCP. Read this
first, then verify any claim against the code before acting on it.

## North star

AnodizeMCP lets people ship real MCP servers on platforms where Rust cannot run.
The official `mcp` SDK and FastMCP both pull in `pydantic-core` (compiled Rust),
which has no wheel and no compiler on z/OS, AIX, s390x, the BSDs, Pyodide, and
locked-down build environments. AnodizeMCP implements the same FastMCP-style API
in pure Python so those servers install and run.

The promise we are keeping: a server written for FastMCP, using only the
implemented surface, runs on AnodizeMCP by changing one import line, and a team
can trust it in production. Every change is measured against that promise.

## Non-negotiable constraints

1. No Rust and no compiled extensions, ever, in the package or its required
   dependencies. The only runtime dependency is `uvicorn` (pure Python).
2. The constraint is Rust, not dependencies. A pure-Python dependency is
   acceptable if it earns its place; a Rust-backed one is not.
3. Never `import pydantic` or `import mcp` in the package. When a user's server
   brings pydantic, use it by duck typing (`model_validate`, `model_json_schema`,
   `model_dump`); never require it. See `schema.py`, `content.py`, `protocol.py`.
4. Public API stability: the top-level `anodize_mcp` re-exports are the contract.
   Subpackages may move; the names exported from `anodize_mcp/__init__.py` may not
   break without a deliberate major-version decision.
5. Support Python 3.9 through 3.14. No `X | Y` unions at runtime on 3.9; use
   `Optional`/`Union`. `_compat.py` holds the version shims.

## How to work here (self-validation loop)

Run the full gate after every change. Do not report work complete with a failing
pass. All commands assume `uv` and run from the repo root.

```sh
uvx ruff format .
uvx ruff check --fix .
uvx mypy
PYTHONPATH=src uv run --no-project --with uvicorn --with httpx python -m unittest discover -s tests
```

Today this is 153 unittest tests, ruff clean, mypy clean on 41 source files.
Spot-check the matrix ends, 3.9 and 3.14, since most regressions surface there:

```sh
PYTHONPATH=src uv run --no-project --python 3.9  --with uvicorn --with httpx python -m unittest discover -s tests
PYTHONPATH=src uv run --no-project --python 3.14 --with uvicorn --with httpx python -m unittest discover -s tests
```

## Conformance with FastMCP (the trust metric)

The real measure is how many of FastMCP's own tests pass against AnodizeMCP, not
our tests. `conformance/aliasplugin.py` is a pytest plugin that rebinds
`fastmcp.FastMCP`, `fastmcp.Client`, the middleware modules, and `mcp.McpError`
to the anodize equivalents before FastMCP's test modules import, so FastMCP's
assertions become the acceptance criteria.

Setup and run (needs `fastmcp`, which needs Rust, so this runs only in CI and on
dev machines, never on z/OS):

```sh
git clone https://github.com/jlowin/fastmcp /tmp/fastmcp-src
git -C /tmp/fastmcp-src checkout 3b8538e
PYTHONPATH=src:conformance uv run --no-project --python 3.12 \
  --with "fastmcp==3.4.2" --with pytest --with pytest-asyncio \
  --with opentelemetry-sdk --with opentelemetry-api --with dirty-equals --with inline-snapshot \
  python -m pytest -p aliasplugin <fastmcp test files> -k "not Integration and not Retry" -o asyncio_mode=auto
```

Two figures to hold:

- Green gate (CI-enforced, must never regress): the six FastMCP test files that
  pass in full, 169 tests. Listed in `.github/workflows/ci.yml` and
  `conformance/README.md`.
- Broader core suite: about 408 of 527 (77%) across tools, resources, prompts,
  server, client, and all middleware. Raising this number, without regressing the
  green gate, is the main conformance objective.

The remaining failures are dominated by tests that assert
`isinstance(x, mcp.types.*)`. Substituting `mcp.types.*` in the harness is not
viable: it breaks FastMCP's own content pipeline, which builds real `mcp.types`
objects. Treat that as the honest boundary, not a bug. Attribute access
(`tool.name`, `result.content[0].text`) already works on both.

## Architecture

The package mirrors FastMCP's layout so the two trees diff cleanly. When you port
a FastMCP module, keep it near-verbatim and change only the Rust-abstraction
lines (`anyio` to `asyncio`, `pydantic_core` to stdlib `json`, pydantic models to
dataclasses, `mcp.types` to plain dicts). Flag each such line in a comment.

| AnodizeMCP | mirrors |
|---|---|
| `server/server.py`, `server/context.py` | `fastmcp/server/{server,context}.py` |
| `server/middleware/{middleware,timing,logging,error_handling,rate_limiting}.py` | same under `fastmcp/server/middleware/` |
| `client/{client,transports}.py` | `fastmcp/client/{client,transports}.py` |
| `tools/tool.py`, `resources/{resource,template}.py`, `prompts/prompt.py` | `fastmcp/{tools,resources,prompts}/*` |
| `utilities/types.py` (`Image`, `File`) | `fastmcp/utilities/types.py` |
| `exceptions.py`, `schema.py`, `content.py`, `protocol.py` | the pure-Python core (no FastMCP counterpart) |

The Rust-abstraction layer lives in `schema.py` (the pydantic replacement: JSON
Schema generation and runtime coercion from type hints), `content.py` (the
`mcp.types` replacement: content blocks as dataclasses), `attrdict.py` (dict
subclass with attribute access, so wire dicts read like typed objects),
`_deferred.py` (dual sync/async return), and `_asyncrun.py` (run sync or async
handlers uniformly).

## Current state (v0.6.0, unpublished; PyPI has 0.5.0)

Implemented and conformance-checked: tools, resources, resource templates,
prompts; `Context` with logging, progress (through the client), `read_resource`,
state (`get_state`/`set_state`/`delete_state`), `sample`, `elicit`, `list_roots`;
pydantic `BaseModel` params and returns (via the server's own pydantic); content
blocks plus the `Image`/`File` helpers and `ToolResult`; middleware (timing,
logging, error handling, rate limiting); the HTTP server (uvicorn, with a stdlib
fallback) and an HTTP client (`Client("http://host/mcp")`, stdlib urllib); auth
(`StaticTokenVerifier`, `JWTVerifier` HS256 stdlib / RS256 via optional
`cryptography`).

Not implemented as of now (not fundamental limits, just unbuilt): `mcp.mount` /
`import_server` / `as_proxy` / `from_openapi` / `from_fastapi`; `@mcp.tool(task=True)`
background tasks; provider integrations; the OAuth 2.1 server flow and hosted-IdP
provider wrappers; the `fastmcp` CLI; component transforms (`PromptsAsTools`,
`ResourcesAsTools`, search); per-tool `timeout`, thread-offload, tool
transformation; legacy SSE transport.

## Enterprise-grade hardening checklist

Production concerns that uvicorn already covers (keep-alive and read timeouts,
request size limits, graceful shutdown, signal handling) are delegated to it, not
reimplemented. The open hardening work, roughly in priority order:

1. Session lifecycle: idle TTL and eviction for HTTP sessions, bounded session
   count, and a clear error when a session expires. Today sessions live for the
   process lifetime.
2. Structured, configurable logging across the server with a request id on every
   line, and an optional OpenTelemetry span per request (behind an extra, never a
   hard dependency).
3. Auth hardening: JWT clock-skew tolerance, JWKS cache TTL and rotation, audience
   and issuer required-by-default options, and scope enforcement audited against
   FastMCP's auth tests.
4. Backpressure and limits: per-client concurrency caps in the dispatcher,
   configurable max request body size at the stdlib fallback (uvicorn already caps
   this), and bounded in-flight server-to-client requests.
5. Error surface: confirm `mask_error_details` covers every path, that no
   traceback leaks by default, and that tool errors map to the MCP-recommended
   `isError` result rather than protocol errors.
6. Supply chain: pinned, audited dependencies; reproducible build; `py.typed`
   shipped (already present); SBOM on release.
7. Fuzz and property tests for `schema.py` coercion and the JSON-RPC envelope,
   since those are the security-relevant parsers.

Each item ships with tests, and where FastMCP has a matching test, with
conformance coverage. Hardening that cannot be validated is not done.

## Roadmap (feasible, phased)

Phase 1, ship v0.6.0. Finalize the middleware, pydantic, Image/File/ToolResult,
and HTTP-client work already in the tree. Run the full gate plus the conformance
green gate, then publish to PyPI (bump only when shipping; the user has asked not
to be version-happy).

Phase 2, conformance expansion. Close the in-scope clusters that do not require
the Rust SDK: the session-scoped `Context` state store with isolation (FastMCP's
`tests/server/test_context.py`), `ToolResult` content tests, and the remaining
FastMCP middleware modules (`caching`, `response_limiting`, `ping`,
`tool_injection`). Target: raise the broader-suite figure and add files to the
green gate. Add each newly-green file to CI.

Phase 3, cross-implementation HTTP. Make the AnodizeMCP client interoperate with a
real FastMCP server over Streamable HTTP and SSE framing, end to end, in CI.
Today the client talks cleanly to an AnodizeMCP HTTP server; FastMCP's exact SSE
stream lifecycle needs work (the POST read timeout already prevents a hang).

Phase 4, enterprise hardening. Work the checklist above, session TTL first.

Phase 5, real-server validation as a standing signal. Keep the convert-and-run
methodology: take real FastMCP example servers, swap the import to `anodize_mcp`,
run them, and track the share that run drop-in. Regressions in that share are
release blockers.

Deferred until there is demand, since each is large: server mounting and proxy,
task queues, OpenAPI/FastAPI generation, the CLI, OAuth authorization-server flow.
None contradicts the north star; they are scope, not constraint.

## Maintainability conventions

- Mirror FastMCP. Same module paths and names. A reviewer should be able to put
  the two files side by side and account for every difference as a deliberate
  Rust abstraction.
- Comments are minimal and accurate. Explain unidiomatic or abstraction-point
  code; never narrate. No comments that reference a session, a request, or an
  agent. No em dashes in docs and comments.
- Commits and PRs: plain declarative messages, no AI co-author trailer, no
  "generated with" footer. One logical change per commit; keep the conformance
  alias plugin and CI in sync when module paths move.
- Tests: new behavior gets an anodize `unittest` test, and where FastMCP has a
  test for it, conformance coverage through the alias plugin.
- Never regress the green gate or the broader-suite figure. Re-measure both before
  committing anything that touches the request path, the client, or schema/content.

## Definition of done (the trust bar)

A change is done when: the full gate is green on 3.9 and 3.14; the conformance
green gate still passes (169) and the broader suite did not regress from its
recorded figure; any FastMCP server using only the implemented surface still runs
by changing the import line; and the change is documented where it affects the
public API or the scope boundary in `README.md` and `conformance/README.md`.
