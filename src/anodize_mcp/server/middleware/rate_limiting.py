"""Rate-limiting middleware, matching FastMCP's API.

The class names, constructor signatures, and behavior mirror
``fastmcp.server.middleware.rate_limiting`` so FastMCP's own tests run against
these unchanged. Uses ``asyncio.Lock`` rather than anyio, keeping it dependency
free.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any, Callable, Optional

from ...exceptions import McpError
from .middleware import Middleware, MiddlewareContext

# JSON-RPC reserves -32000 to -32099 for server-defined errors; FastMCP's
# RateLimitError uses -32000, so match it for conformance.
RATE_LIMIT_ERROR = -32000


class RateLimitError(McpError):
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message, code=RATE_LIMIT_ERROR)


class TokenBucketRateLimiter:
    """Token bucket: a steady refill rate with room for bursts up to capacity."""

    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens: float = capacity
        self.last_refill = time.time()
        self._lock = asyncio.Lock()

    async def consume(self, tokens: int = 1) -> bool:
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            # last_refill advances on every call, success or not, so denied
            # retries cannot re-count the same elapsed window (fastmcp #4056).
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False


class SlidingWindowRateLimiter:
    """Allow up to ``max_requests`` within a rolling ``window_seconds`` window."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def is_allowed(self) -> bool:
        async with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            while self.requests and self.requests[0] < cutoff:
                self.requests.popleft()
            if len(self.requests) < self.max_requests:
                self.requests.append(now)
                return True
            return False


class RateLimitingMiddleware(Middleware):
    """Token-bucket rate limiting, global or per-client.

    Raises :class:`RateLimitError` when the limit is exceeded.
    """

    def __init__(
        self,
        max_requests_per_second: float = 10.0,
        burst_capacity: Optional[int] = None,
        get_client_id: Optional[Callable[[MiddlewareContext], str]] = None,
        global_limit: bool = False,
    ):
        self.max_requests_per_second = max_requests_per_second
        self.burst_capacity = burst_capacity or int(max_requests_per_second * 2)
        self.get_client_id = get_client_id
        self.global_limit = global_limit
        self.limiters: dict[str, TokenBucketRateLimiter] = defaultdict(
            lambda: TokenBucketRateLimiter(self.burst_capacity, self.max_requests_per_second)
        )
        if self.global_limit:
            self.global_limiter = TokenBucketRateLimiter(
                self.burst_capacity, self.max_requests_per_second
            )

    def _get_client_identifier(self, context: MiddlewareContext) -> str:
        if self.get_client_id:
            return self.get_client_id(context)
        return "global"

    async def on_request(self, context: MiddlewareContext, call_next: Any) -> Any:
        if self.global_limit:
            if not await self.global_limiter.consume():
                raise RateLimitError("Global rate limit exceeded")
        else:
            client_id = self._get_client_identifier(context)
            if not await self.limiters[client_id].consume():
                raise RateLimitError(f"Rate limit exceeded for client: {client_id}")
        return await call_next(context)


class SlidingWindowRateLimitingMiddleware(Middleware):
    """Sliding-window rate limiting, per-client."""

    def __init__(
        self,
        max_requests: int,
        window_minutes: int = 1,
        get_client_id: Optional[Callable[[MiddlewareContext], str]] = None,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_minutes * 60
        self.get_client_id = get_client_id
        self.limiters: dict[str, SlidingWindowRateLimiter] = defaultdict(
            lambda: SlidingWindowRateLimiter(self.max_requests, self.window_seconds)
        )

    def _get_client_identifier(self, context: MiddlewareContext) -> str:
        if self.get_client_id:
            return self.get_client_id(context)
        return "global"

    async def on_request(self, context: MiddlewareContext, call_next: Any) -> Any:
        client_id = self._get_client_identifier(context)
        if not await self.limiters[client_id].is_allowed():
            raise RateLimitError(
                f"Rate limit exceeded: {self.max_requests} requests per "
                f"{self.window_seconds // 60} minutes for client: {client_id}"
            )
        return await call_next(context)
