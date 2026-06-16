"""anodize: a lightweight, pure-Python MCP server framework.

Pure Python with no compiled extensions, so no Rust toolchain is required. Its
dependencies (uvicorn and friends) are themselves pure Python. That makes it
usable wherever the official SDK's Rust-backed dependencies (pydantic-core) have
no prebuilt wheel and cannot be compiled:
IBM mainframes (z/OS, Linux on Z / s390x), AIX, Solaris, the BSDs, exotic or
older CPU architectures, WebAssembly (Pyodide), and locked-down or air-gapped
build environments.

The public API mirrors FastMCP's ergonomics: build an :class:`AnodizeMCP`
server, register tools, resources, and prompts with decorators, then ``run()``.

    from anodize_mcp import AnodizeMCP

    mcp = AnodizeMCP("demo")

    @mcp.tool
    def add(a: int, b: int) -> int:
        "Add two numbers."
        return a + b

    if __name__ == "__main__":
        mcp.run()

The class is also exported as ``FastMCP`` (and the short alias ``Anodize``), so
code can ``from anodize_mcp import FastMCP`` today and switch to
``from fastmcp import FastMCP`` once a Rust toolchain is available, changing only
the import line.
"""

from __future__ import annotations

from .auth import (
    AccessToken,
    JWTVerifier,
    StaticTokenVerifier,
    TokenVerifier,
    get_access_token,
)
from .client import (
    CallToolResult,
    Client,
    ClientError,
    FastMCPTransport,
    NodeStdioTransport,
    PythonStdioTransport,
    StdioTransport,
    StreamableHttpTransport,
)
from .clientfeatures import CompletionResult, CreateMessageResult, ElicitResult, Root
from .content import (
    AudioContent,
    EmbeddedResource,
    ImageContent,
    ResourceContents,
    ResourceLink,
    TextContent,
)
from .exceptions import McpError, NotFoundError, PromptError, ResourceError, ToolError
from .prompts import PromptMessage
from .protocol import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from .routes import Request, Response
from .schema import Field
from .server import Anodize, AnodizeMCP, Context, FastMCP, RequestContext
from .server.middleware import (
    DetailedTimingMiddleware,
    ErrorHandlingMiddleware,
    LoggingMiddleware,
    Middleware,
    MiddlewareContext,
    RateLimitError,
    RateLimitingMiddleware,
    RetryMiddleware,
    SlidingWindowRateLimiter,
    SlidingWindowRateLimitingMiddleware,
    StructuredLoggingMiddleware,
    TimingMiddleware,
    TokenBucketRateLimiter,
)
from .tools.tool import ToolResult
from .utilities.types import Audio, File, Image

__version__ = "0.7.0"

__all__ = [
    "AnodizeMCP",
    "Anodize",
    "FastMCP",
    "Context",
    "Field",
    "ToolError",
    "ResourceError",
    "PromptError",
    "NotFoundError",
    "McpError",
    "TextContent",
    "ImageContent",
    "AudioContent",
    "ResourceLink",
    "EmbeddedResource",
    "ResourceContents",
    "Image",
    "Audio",
    "File",
    "ToolResult",
    "PromptMessage",
    "CreateMessageResult",
    "ElicitResult",
    "Root",
    "CompletionResult",
    "AccessToken",
    "TokenVerifier",
    "StaticTokenVerifier",
    "JWTVerifier",
    "get_access_token",
    "Middleware",
    "MiddlewareContext",
    "TimingMiddleware",
    "DetailedTimingMiddleware",
    "LoggingMiddleware",
    "StructuredLoggingMiddleware",
    "ErrorHandlingMiddleware",
    "RetryMiddleware",
    "Request",
    "Response",
    "RequestContext",
    "RateLimitingMiddleware",
    "SlidingWindowRateLimitingMiddleware",
    "TokenBucketRateLimiter",
    "SlidingWindowRateLimiter",
    "RateLimitError",
    "Client",
    "ClientError",
    "FastMCPTransport",
    "NodeStdioTransport",
    "PythonStdioTransport",
    "StdioTransport",
    "StreamableHttpTransport",
    "CallToolResult",
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "__version__",
]
