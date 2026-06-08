"""Opaque cursor pagination for list endpoints.

The cursor is a base64-encoded byte offset into the full ordered list. It is
deliberately opaque to clients; they only echo it back. An unparseable cursor is
an ``InvalidParams`` error, as the spec requires.
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from .exceptions import InvalidParams


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeDecodeError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise InvalidParams(f"invalid cursor: {cursor!r}") from exc


def paginate(
    items: list[Any], cursor: Optional[str], page_size: int
) -> tuple[list[Any], Optional[str]]:
    """Return ``(page, next_cursor)``; ``next_cursor`` is ``None`` on the last page."""
    offset = decode_cursor(cursor) if cursor else 0
    if offset < 0 or offset > len(items):
        raise InvalidParams(f"cursor out of range: {cursor!r}")
    end = offset + page_size
    page = items[offset:end]
    next_cursor = encode_cursor(end) if end < len(items) else None
    return page, next_cursor
