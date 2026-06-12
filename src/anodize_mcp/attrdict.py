"""Attribute-accessible dicts for results that callers read with dot access.

Wire results are JSON objects (dicts), but FastMCP hands back typed objects read
as ``tool.name`` or ``result.content[0].text``. :class:`AttrDict` subclasses
``dict`` so item access, equality with plain dicts, and JSON serialization keep
working while attribute access is added on top.
"""

from __future__ import annotations

from typing import Any


class AttrDict(dict):
    """A dict whose keys are also reachable as attributes."""

    def __getattr__(self, name: str) -> Any:
        if name in self:
            return self._wrapped(name)
        # MCP carries metadata in the wire field "_meta"; expose it as ".meta".
        if name == "meta" and "_meta" in self:
            return self._wrapped("_meta")
        # Dunder lookups must fail normally (copy, pickle, inspect protocols).
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        # FastMCP's typed objects expose unset optional fields as None
        # (tool.outputSchema, result.meta); match that rather than raising.
        return None

    def _wrapped(self, key: str) -> Any:
        # Wrap in place so repeated reads return the same objects and
        # in-place mutations (result.contents[0].text = ...) survive into
        # serialization.
        value = self[key]
        if isinstance(value, list):
            for i, item in enumerate(value):
                wrapped_item = wrap(item)
                if wrapped_item is not item:
                    value[i] = wrapped_item
            return value
        wrapped = wrap(value)
        if wrapped is not value:
            self[key] = wrapped
        return wrapped


def wrap(value: Any) -> Any:
    if isinstance(value, AttrDict):
        return value
    if isinstance(value, dict):
        return AttrDict(value)
    if isinstance(value, list):
        return [wrap(item) for item in value]
    return value
