"""Response caching middleware.

Caches ``on_call_tool`` and ``on_read_resource`` results in an in-memory dict.
The cache key is ``(method, sorted_params_json)`` so calls with the same
arguments always hit the same entry regardless of dict insertion order.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from .middleware import Middleware, MiddlewareContext


class ResponseCachingMiddleware(Middleware):
    """Cache tool and resource responses in memory.

    Args:
        ttl: Seconds before a cached entry expires. ``None`` means no expiry.
        max_size: Maximum number of entries. When full, the oldest entry is
            evicted (insertion-order FIFO via ``dict`` iteration).
    """

    def __init__(self, ttl: Optional[float] = None, max_size: int = 1000):
        self.ttl = ttl
        self.max_size = max_size
        # {key: (result, stored_at)}
        self._cache: dict[str, tuple[Any, float]] = {}

    def _make_key(self, method: Optional[str], message: Any) -> str:
        try:
            params = dict(message) if not isinstance(message, dict) else message
        except (TypeError, ValueError):
            params = {"_repr": repr(message)}
        return json.dumps({"method": method, "params": params}, sort_keys=True)

    def _get(self, key: str) -> tuple[bool, Any]:
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        result, stored_at = entry
        if self.ttl is not None and (time.monotonic() - stored_at) > self.ttl:
            del self._cache[key]
            return False, None
        return True, result

    def _put(self, key: str, result: Any) -> None:
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self.max_size:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[key] = (result, time.monotonic())

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        key = self._make_key(context.method, context.message)
        hit, cached = self._get(key)
        if hit:
            return cached
        result = await call_next(context)
        self._put(key, result)
        return result

    async def on_read_resource(self, context: MiddlewareContext, call_next: Any) -> Any:
        key = self._make_key(context.method, context.message)
        hit, cached = self._get(key)
        if hit:
            return cached
        result = await call_next(context)
        self._put(key, result)
        return result
