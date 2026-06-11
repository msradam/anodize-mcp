"""Middleware, mirroring ``fastmcp.server.middleware``.

The base hooks and the bundled middleware (timing, logging, error handling, rate
limiting) live in modules with the same names as FastMCP's.
"""

from __future__ import annotations

from .error_handling import ErrorHandlingMiddleware, RetryMiddleware
from .logging import LoggingMiddleware, StructuredLoggingMiddleware
from .middleware import CallNext, Middleware, MiddlewareContext
from .rate_limiting import (
    RateLimitError,
    RateLimitingMiddleware,
    SlidingWindowRateLimiter,
    SlidingWindowRateLimitingMiddleware,
    TokenBucketRateLimiter,
)
from .timing import DetailedTimingMiddleware, TimingMiddleware

__all__ = [
    "Middleware",
    "MiddlewareContext",
    "CallNext",
    "TimingMiddleware",
    "DetailedTimingMiddleware",
    "LoggingMiddleware",
    "StructuredLoggingMiddleware",
    "ErrorHandlingMiddleware",
    "RetryMiddleware",
    "RateLimitingMiddleware",
    "SlidingWindowRateLimitingMiddleware",
    "TokenBucketRateLimiter",
    "SlidingWindowRateLimiter",
    "RateLimitError",
]
