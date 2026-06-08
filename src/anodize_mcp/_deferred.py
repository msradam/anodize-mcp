"""A value that is usable both synchronously and with ``await``.

FastMCP's ``Context`` methods are all coroutines, so portable code writes
``await ctx.info(...)`` and ``result = await ctx.sample(...)``. anodize's core
is synchronous. To let the *same* handler source run unchanged on either
framework, every ``Context`` method returns a :class:`Deferred`:

* ``await ctx.sample(...)`` -> the wrapped value (FastMCP-style, portable), and
* ``ctx.sample(...)`` -> a proxy that forwards attribute/item/iteration access
  straight to the wrapped value (synchronous convenience).

The work has already happened by the time the ``Deferred`` is returned; awaiting
it just unwraps the result.
"""

from __future__ import annotations

from typing import Any, Iterator


def _deferred_await(value: Any) -> Iterator[Any]:
    yield from ()  # never yields; this is a no-op awaitable
    return value


class Deferred:
    __slots__ = ("_value",)

    def __init__(self, value: Any):
        object.__setattr__(self, "_value", value)

    def __await__(self) -> Iterator[Any]:
        return _deferred_await(self._value)

    # -- transparent proxying for synchronous use -------------------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._value, name)

    def __getitem__(self, key: Any) -> Any:
        return self._value[key]

    def __iter__(self) -> Any:
        return iter(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Deferred):
            other = other._value
        return bool(self._value == other)

    def __repr__(self) -> str:
        return repr(self._value)

    def unwrap(self) -> Any:
        return self._value


def defer(value: Any) -> Any:
    """Wrap ``value`` so it works both synchronously and with ``await``.

    Typed as ``Any`` so callers keep the wrapped value's static type: a method
    annotated ``-> CreateMessageResult`` can ``return defer(result)`` and both
    ``x = ctx.m()`` and ``x = await ctx.m()`` present as ``CreateMessageResult``.
    """
    return Deferred(value)
