"""Async data-pipeline MCP server.

Tools: ingest, transform, summarize, get_metrics.
Middleware: StatsTimingMiddleware (wraps TimingMiddleware), LoggingMiddleware.
"""

from __future__ import annotations

import time
from typing import Any

from anodize_mcp import AnodizeMCP, Context
from anodize_mcp.server.middleware.logging import LoggingMiddleware
from anodize_mcp.server.middleware.middleware import CallNext, MiddlewareContext
from anodize_mcp.server.middleware.timing import TimingMiddleware

mcp = AnodizeMCP("async-pipeline")

# ---------------------------------------------------------------------------
# Stats-collecting timing middleware
# (TimingMiddleware only logs; it has no get_stats(). We extend it here.)
# ---------------------------------------------------------------------------

_VALID_OPS = {"upper", "lower", "strip", "reverse"}


class StatsTimingMiddleware(TimingMiddleware):
    """TimingMiddleware extended with per-tool call statistics."""

    def __init__(self) -> None:
        super().__init__()
        self._stats: dict[str, list[float]] = {}

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        tool_name = getattr(context.message, "name", "unknown")
        start = time.perf_counter()
        try:
            result = await call_next(context)
            duration_ms = (time.perf_counter() - start) * 1000
            self._stats.setdefault(tool_name, []).append(duration_ms)
            return result
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            self._stats.setdefault(tool_name, []).append(duration_ms)
            raise

    def get_stats(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for name, durations in self._stats.items():
            n = len(durations)
            out[name] = {
                "count": n,
                "total_ms": sum(durations),
                "mean_ms": sum(durations) / n,
                "min_ms": min(durations),
                "max_ms": max(durations),
            }
        return out


_timing = StatsTimingMiddleware()
mcp.add_middleware(_timing)
mcp.add_middleware(LoggingMiddleware())


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def ingest(records: list[dict], ctx: Context) -> dict:
    """Validate and count a batch of records.

    Returns total, valid, and invalid counts.
    """
    await ctx.info(f"ingest: received {len(records)} record(s)")

    valid = 0
    invalid = 0
    for i, rec in enumerate(records):
        await ctx.debug(f"ingest: checking record {i}")
        if isinstance(rec, dict) and rec:
            valid += 1
        else:
            invalid += 1

    await ctx.info(f"ingest: valid={valid} invalid={invalid}")
    return {"total": len(records), "valid": valid, "invalid": invalid}


@mcp.tool
async def transform(payload: str, operations: list[str]) -> str:
    """Apply a sequence of string operations to payload.

    Supported operations: upper, lower, strip, reverse.
    Raises ValueError on unknown operation.
    """
    result = payload
    for op in operations:
        if op not in _VALID_OPS:
            raise ValueError(f"Unknown operation: {op!r}. Valid: {sorted(_VALID_OPS)}")
        if op == "upper":
            result = result.upper()
        elif op == "lower":
            result = result.lower()
        elif op == "strip":
            result = result.strip()
        elif op == "reverse":
            result = result[::-1]
    return result


@mcp.tool
async def summarize(texts: list[str]) -> dict:
    """Count words in each string.

    Returns a dict mapping each string to its word count.
    """
    return {text: len(text.split()) for text in texts}


@mcp.tool
def get_metrics() -> dict:
    """Return per-tool timing statistics collected by StatsTimingMiddleware."""
    return _timing.get_stats()


if __name__ == "__main__":
    mcp.run()
