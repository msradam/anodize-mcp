"""Config-vault MCP server with JWT auth, per-environment resource templates, and middleware.

Run over stdio:
    python examples/config_vault/server.py

Or import ``mcp`` directly for in-process testing.
"""

from __future__ import annotations

import json

from anodize_mcp import AnodizeMCP, Context, JWTVerifier, ResourceError, get_access_token
from anodize_mcp.server.middleware.error_handling import ErrorHandlingMiddleware
from anodize_mcp.server.middleware.rate_limiting import RateLimitingMiddleware

JWT_SECRET = "dev-secret-hs256"

auth = JWTVerifier(secret=JWT_SECRET, cache_ttl=300.0)

# mask_error_details lives on AnodizeMCP, not on ErrorHandlingMiddleware.
# FastMCP exposes this as ErrorHandlingMiddleware(mask_error_details=True);
# in anodize it is a constructor param on AnodizeMCP itself (gap documented in tests).
mcp = AnodizeMCP(
    "config-vault",
    version="1.0.0",
    instructions="Per-environment configuration vault.",
    auth=auth,
    mask_error_details=True,
)

# 100 req/min ≈ 1.667 req/s; burst headroom of 200 for short bursts.
mcp.add_middleware(RateLimitingMiddleware(max_requests_per_second=100 / 60, burst_capacity=200))
mcp.add_middleware(ErrorHandlingMiddleware())

# In-process config store: {env: {key: value}}
_store: dict[str, dict[str, str]] = {
    "dev": {"db_url": "postgres://localhost/dev", "log_level": "DEBUG"},
    "prod": {"db_url": "postgres://rds.example.com/prod", "log_level": "WARNING"},
}

# Session-state key used to store simulated scopes for in-process testing.
# The HTTP transport sets get_access_token(); the memory transport cannot
# (ContextVar values don't cross thread boundaries), so tests write scopes here.
_SESSION_SCOPES_KEY = "_vault_scopes"


def _effective_scopes(ctx: Context) -> list[str]:
    """Return scopes from the HTTP access token or from session state (in-process tests)."""
    token = get_access_token()
    if token is not None:
        return token.scopes
    return ctx._session.state.get(_SESSION_SCOPES_KEY, [])


def _require_write_scope(ctx: Context) -> None:
    if "write" not in _effective_scopes(ctx):
        raise PermissionError("write scope required")


@mcp.tool(name="_set_test_scopes")
def set_test_scopes(scopes: str, ctx: Context) -> str:
    """Internal: store a space-separated scope string in session state for in-process tests."""
    ctx._session.state[_SESSION_SCOPES_KEY] = scopes.split()
    return json.dumps({"scopes": scopes.split()})


@mcp.resource("config://{env}/{key}", mime_type="application/json")
def get_config(env: str, key: str) -> str:
    """Return the JSON-encoded config value for the given environment and key."""
    env_store = _store.get(env, {})
    if key not in env_store:
        raise ResourceError(f"Key {key!r} not found in environment {env!r}")
    return json.dumps({"env": env, "key": key, "value": env_store[key]})


@mcp.tool
def set_config(env: str, key: str, value: str, ctx: Context) -> str:
    """Write a key/value pair into the config store. Requires the 'write' scope."""
    _require_write_scope(ctx)
    if env not in _store:
        _store[env] = {}
    _store[env][key] = value
    return json.dumps({"status": "ok", "env": env, "key": key, "value": value})


@mcp.tool
def delete_config(env: str, key: str, ctx: Context) -> str:
    """Remove a key from the config store. Requires the 'write' scope."""
    _require_write_scope(ctx)
    env_store = _store.get(env, {})
    if key not in env_store:
        raise KeyError(f"Key {key!r} not found in environment {env!r}")
    del _store[env][key]
    return json.dumps({"status": "deleted", "env": env, "key": key})


if __name__ == "__main__":
    mcp.run()
