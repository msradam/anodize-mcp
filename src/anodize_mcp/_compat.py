"""Typing helpers that smooth over differences between Python 3.9 and newer.

Everything here is standard-library only. The goal is to let the rest of the
package reason about annotations without sprinkling version checks around.
"""

from __future__ import annotations

import sys
import typing
from typing import Any, Union

if sys.version_info >= (3, 10):
    from types import UnionType as _UnionType

    _UNION_TYPES: tuple[Any, ...] = (Union, _UnionType)
else:  # Python 3.9 has no X | Y union operator
    _UNION_TYPES = (Union,)

NoneType = type(None)


def get_origin(tp: Any) -> Any:
    return typing.get_origin(tp)


def get_args(tp: Any) -> tuple[Any, ...]:
    return typing.get_args(tp)


def is_union(tp: Any) -> bool:
    return typing.get_origin(tp) in _UNION_TYPES


def unwrap_annotated(tp: Any) -> tuple[Any, tuple[Any, ...]]:
    """Split ``Annotated[T, m1, m2]`` into ``(T, (m1, m2))``.

    Returns ``(tp, ())`` for non-annotated types.
    """
    if typing.get_origin(tp) is typing.Annotated:
        args = typing.get_args(tp)
        return args[0], args[1:]
    return tp, ()


def get_type_hints(obj: Any) -> dict[str, Any]:
    """``typing.get_type_hints`` with ``Annotated`` metadata preserved."""
    try:
        return typing.get_type_hints(obj, include_extras=True)
    except Exception:
        # Fall back to raw annotations if forward references cannot resolve.
        return dict(getattr(obj, "__annotations__", {}))


PY_VERSION = sys.version_info[:2]
