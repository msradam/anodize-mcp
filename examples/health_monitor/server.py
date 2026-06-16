"""System health-monitor MCP server.

Exposes fake system metrics as resources and tools. Demonstrates lifespan
management, resource templates, and error handling middleware.

Run:
    cd /path/to/anodize-mcp
    PYTHONPATH=src uv run python examples/health_monitor/server.py
"""

from __future__ import annotations

import contextlib
import json
from typing import Literal

from anodize_mcp import AnodizeMCP, ErrorHandlingMiddleware, ResourceError

# Module-level state initialised by the lifespan.
_metrics_collector: dict | None = None
_event_log: list[dict] = []

# Small registry of fake per-process data.
_PROCESS_REGISTRY: dict[int, dict] = {
    1: {"name": "init", "cpu_pct": 0.1, "mem_mb": 8.0},
    100: {"name": "health_monitor", "cpu_pct": 2.3, "mem_mb": 45.6},
    200: {"name": "nginx", "cpu_pct": 0.8, "mem_mb": 12.4},
}


@contextlib.asynccontextmanager
async def lifespan(server: AnodizeMCP):
    global _metrics_collector
    _metrics_collector = {
        "started": True,
        "samples_collected": 0,
    }
    try:
        yield _metrics_collector
    finally:
        _metrics_collector = None


mcp = AnodizeMCP(
    "health-monitor",
    version="1.0.0",
    instructions="Expose fake system metrics as MCP resources and tools.",
    lifespan=lifespan,
    # mask_error_details on AnodizeMCP controls the server's built-in error
    # masking; ErrorHandlingMiddleware does not accept this parameter in
    # anodize (gap vs FastMCP where the middleware accepts it directly).
    mask_error_details=True,
)

# ErrorHandlingMiddleware adds logging, error counting, and error transformation
# on top of the server's own mask_error_details flag.
mcp.add_middleware(ErrorHandlingMiddleware())


@mcp.resource("metrics://cpu", mime_type="application/json")
def cpu_metrics() -> str:
    """Current CPU utilisation."""
    return json.dumps({"usage_pct": 42.0})


@mcp.resource("metrics://memory", mime_type="application/json")
def memory_metrics() -> str:
    """Current memory statistics."""
    return json.dumps(
        {
            "total_mb": 16384,
            "used_mb": 8192,
            "free_mb": 8192,
            "usage_pct": 50.0,
        }
    )


@mcp.resource("metrics://process/{pid}", mime_type="application/json")
def process_metrics(pid: int) -> str:
    """Per-process statistics for the given PID."""
    proc = _PROCESS_REGISTRY.get(pid)
    if proc is None:
        raise ResourceError(f"No process with pid {pid}")
    return json.dumps({"pid": pid, **proc})


@mcp.tool
def record_event(
    name: str,
    severity: Literal["info", "warn", "error"],
    message: str,
) -> int:
    """Append an event to the in-memory log and return the total event count."""
    _event_log.append({"name": name, "severity": severity, "message": message})
    return len(_event_log)


@mcp.tool
def get_events(limit: int = 10) -> list[dict]:
    """Return the last *limit* events from the event log."""
    return _event_log[-limit:]


@mcp.tool
def clear_events() -> int:
    """Clear the event log and return the number of events removed."""
    count = len(_event_log)
    _event_log.clear()
    return count


if __name__ == "__main__":
    mcp.run()
