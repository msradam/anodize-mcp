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

    import anodize_mcp
    import anodize_mcp.errorhandling
    import anodize_mcp.logging_middleware
    import anodize_mcp.middleware
    import anodize_mcp.ratelimit
    import anodize_mcp.timing

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

    # Feature modules, mapped to the anodize equivalents.
    sys.modules["fastmcp.server.middleware.middleware"] = anodize_mcp.middleware
    sys.modules["fastmcp.server.middleware.rate_limiting"] = anodize_mcp.ratelimit
    sys.modules["fastmcp.server.middleware.timing"] = anodize_mcp.timing
    sys.modules["fastmcp.server.middleware.logging"] = anodize_mcp.logging_middleware
    sys.modules["fastmcp.server.middleware.error_handling"] = anodize_mcp.errorhandling
