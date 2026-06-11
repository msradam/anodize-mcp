import unittest
from unittest.mock import AsyncMock, MagicMock

from anodize_mcp import (
    RateLimitError,
    RateLimitingMiddleware,
    SlidingWindowRateLimiter,
    SlidingWindowRateLimitingMiddleware,
    TokenBucketRateLimiter,
)
from anodize_mcp.middleware import MiddlewareContext


def mock_context():
    context = MagicMock(spec=MiddlewareContext)
    context.method = "test_method"
    return context


class TokenBucketTest(unittest.IsolatedAsyncioTestCase):
    def test_init(self):
        limiter = TokenBucketRateLimiter(capacity=10, refill_rate=5.0)
        self.assertEqual(limiter.capacity, 10)
        self.assertEqual(limiter.refill_rate, 5.0)
        self.assertEqual(limiter.tokens, 10)

    async def test_consume_until_empty(self):
        limiter = TokenBucketRateLimiter(capacity=5, refill_rate=1.0)
        self.assertTrue(await limiter.consume(5))
        self.assertFalse(await limiter.consume(1))

    async def test_refill(self):
        # Deterministic: rewind last_refill instead of sleeping.
        limiter = TokenBucketRateLimiter(capacity=10, refill_rate=10.0)
        self.assertTrue(await limiter.consume(10))
        self.assertFalse(await limiter.consume(1))
        limiter.last_refill -= 0.2  # 0.2s at 10/s = 2 tokens
        self.assertTrue(await limiter.consume(2))
        self.assertFalse(await limiter.consume(1))

    async def test_denied_consumes_advance_clock(self):
        # Regression: last_refill advances on denied calls too, so a client
        # cannot re-count the same window by retrying.
        limiter = TokenBucketRateLimiter(capacity=10, refill_rate=10.0)
        self.assertTrue(await limiter.consume(10))
        limiter.last_refill -= 0.2
        for _ in range(5):
            await limiter.consume(1)  # consumes the ~2 refilled tokens
        self.assertFalse(await limiter.consume(5))


class SlidingWindowTest(unittest.IsolatedAsyncioTestCase):
    async def test_within_and_over_limit(self):
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)
        self.assertTrue(await limiter.is_allowed())
        self.assertTrue(await limiter.is_allowed())
        self.assertFalse(await limiter.is_allowed())

    async def test_window_slides(self):
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=1)
        self.assertTrue(await limiter.is_allowed())
        self.assertTrue(await limiter.is_allowed())
        self.assertFalse(await limiter.is_allowed())
        # Age the recorded requests past the window deterministically.
        limiter.requests = type(limiter.requests)(t - 2 for t in limiter.requests)
        self.assertTrue(await limiter.is_allowed())


class MiddlewareTest(unittest.IsolatedAsyncioTestCase):
    async def test_per_client_limit(self):
        mw = RateLimitingMiddleware(max_requests_per_second=0.001, burst_capacity=1)
        call_next = AsyncMock(return_value="ok")
        self.assertEqual(await mw.on_request(mock_context(), call_next), "ok")
        with self.assertRaises(RateLimitError):
            await mw.on_request(mock_context(), call_next)

    async def test_global_limit(self):
        mw = RateLimitingMiddleware(
            max_requests_per_second=0.001, burst_capacity=1, global_limit=True
        )
        call_next = AsyncMock(return_value="ok")
        await mw.on_request(mock_context(), call_next)
        with self.assertRaises(RateLimitError):
            await mw.on_request(mock_context(), call_next)

    async def test_custom_client_id(self):
        mw = RateLimitingMiddleware(get_client_id=lambda ctx: "client-42")
        self.assertEqual(mw._get_client_identifier(mock_context()), "client-42")

    async def test_sliding_window_middleware(self):
        mw = SlidingWindowRateLimitingMiddleware(max_requests=1)
        call_next = AsyncMock(return_value="ok")
        await mw.on_request(mock_context(), call_next)
        with self.assertRaises(RateLimitError):
            await mw.on_request(mock_context(), call_next)


class RateLimitErrorTest(unittest.TestCase):
    def test_messages(self):
        self.assertIn("Rate limit exceeded", str(RateLimitError()))
        self.assertEqual(str(RateLimitError("Custom message")), "Custom message")


if __name__ == "__main__":
    unittest.main()
