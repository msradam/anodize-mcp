"""Run FastMCP's own test suite against AnodizeMCP.

pytest loads ``-p`` plugins before test modules, so rebinding FastMCP's public
classes and module paths here makes ``from fastmcp import FastMCP`` and
``from fastmcp.server.middleware.rate_limiting import ...`` resolve to the
anodize implementations. This proves behavioral parity against FastMCP's own
assertions. Requires fastmcp installed, so it runs in a dedicated CI job (never
on z/OS).
"""

import sys


def pytest_configure(config):
    import fastmcp
    import fastmcp.client
    import fastmcp.client.transports
    import fastmcp.exceptions
    import fastmcp.server.server as server_module
    import mcp
    import mcp.shared.exceptions

    import anodize_mcp
    import anodize_mcp.server.middleware.error_handling
    import anodize_mcp.server.middleware.logging
    import anodize_mcp.server.middleware.middleware
    import anodize_mcp.server.middleware.rate_limiting
    import anodize_mcp.server.middleware.timing

    # Substitute anodize's pure-Python McpError so that isinstance() checks and
    # error-path assertions resolve without the SDK. (mcp.types.* is deliberately
    # not aliased: it would break FastMCP's own content pipeline, which builds
    # real mcp.types objects.)
    mcp.McpError = anodize_mcp.McpError
    mcp.shared.exceptions.McpError = anodize_mcp.McpError

    # Server and client classes (both the top-level and submodule bindings).
    fastmcp.FastMCP = anodize_mcp.AnodizeMCP
    server_module.FastMCP = anodize_mcp.AnodizeMCP
    fastmcp.Client = anodize_mcp.Client
    fastmcp.client.Client = anodize_mcp.Client
    fastmcp.client.transports.FastMCPTransport = anodize_mcp.FastMCPTransport

    # At the client boundary FastMCP raises ToolError where anodize raises
    # ClientError; map them so error-path assertions resolve.
    fastmcp.exceptions.ToolError = anodize_mcp.ClientError
    fastmcp.exceptions.NotFoundError = anodize_mcp.NotFoundError

    # Feature modules, mapped to the anodize equivalents (same relative paths).
    mw = anodize_mcp.server.middleware
    sys.modules["fastmcp.server.middleware.middleware"] = mw.middleware
    sys.modules["fastmcp.server.middleware.rate_limiting"] = mw.rate_limiting
    sys.modules["fastmcp.server.middleware.timing"] = mw.timing
    sys.modules["fastmcp.server.middleware.logging"] = mw.logging
    sys.modules["fastmcp.server.middleware.error_handling"] = mw.error_handling
