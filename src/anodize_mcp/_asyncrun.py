"""Run coroutine results from synchronous, possibly multi-threaded, callers.

The transports are synchronous but handlers may be ``async def``. A handler runs
on its own thread, so we drive its coroutine with ``asyncio.run`` on a private
loop (see :func:`run_maybe_async`). The shared background loop below is only the
fallback for the rare case of being called from within an already-running loop.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any


class _LoopRunner:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is not None:
            return loop
        with self._lock:
            if self._loop is None:
                new_loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=new_loop.run_forever,
                    name="anodize-asyncio",
                    daemon=True,
                )
                thread.start()
                self._loop = new_loop
            return self._loop

    def run(self, coro: Any) -> Any:
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result()


_runner = _LoopRunner()


def run_coro(coro: Any) -> Any:
    """Run a coroutine on the shared background loop and block for the result.

    Used for lifespan enter/exit, where a single persistent loop must own any
    async resources for their whole lifetime.
    """
    return _runner.run(coro)


def run_maybe_async(value: Any) -> Any:
    """If ``value`` is a coroutine, run it to completion and return the result.

    Handlers run on their own thread (a stdio worker or an HTTP request thread),
    so the common case is "no event loop in this thread": we spin up a private
    loop with ``asyncio.run`` so concurrent async handlers do not serialize on a
    shared loop. If we are already inside a running loop, fall back to the shared
    background loop via ``run_coroutine_threadsafe``.
    """
    if not inspect.iscoroutine(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    return _runner.run(value)
