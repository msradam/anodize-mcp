"""anodize: a lightweight, pure-Python MCP server framework.

Standard library only, zero third-party dependencies, and no Rust toolchain
required. That makes it usable wherever the official SDK's Rust-backed
dependencies (pydantic-core) have no prebuilt wheel and cannot be compiled:
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

from .clientfeatures import CompletionResult, CreateMessageResult, ElicitResult, Root
from .content import (
    AudioContent,
    EmbeddedResource,
    ImageContent,
    ResourceContents,
    ResourceLink,
    TextContent,
)
from .context import Context
from .exceptions import McpError, ResourceError, ToolError
from .models import PromptMessage
from .protocol import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from .schema import Field
from .server import Anodize, AnodizeMCP, FastMCP

__version__ = "0.1.0"

__all__ = [
    "AnodizeMCP",
    "Anodize",
    "FastMCP",
    "Context",
    "Field",
    "ToolError",
    "ResourceError",
    "McpError",
    "TextContent",
    "ImageContent",
    "AudioContent",
    "ResourceLink",
    "EmbeddedResource",
    "ResourceContents",
    "PromptMessage",
    "CreateMessageResult",
    "ElicitResult",
    "Root",
    "CompletionResult",
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "__version__",
]
