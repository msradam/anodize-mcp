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
            return wrap(self[name])
        # MCP carries metadata in the wire field "_meta"; expose it as ".meta".
        if name == "meta" and "_meta" in self:
            return wrap(self["_meta"])
        raise AttributeError(name)


def wrap(value: Any) -> Any:
    if isinstance(value, AttrDict):
        return value
    if isinstance(value, dict):
        return AttrDict(value)
    if isinstance(value, list):
        return [wrap(item) for item in value]
    return value
