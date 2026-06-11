"""Shared helpers for registry components (tools, resources, prompts)."""

from __future__ import annotations

from typing import Any, Optional


def build_meta(meta: Optional[dict[str, Any]], tags: Any) -> Optional[dict[str, Any]]:
    """Combine user-supplied metadata with tags under the ``fastmcp`` key.

    Matches FastMCP, which surfaces a component's tags at
    ``_meta["fastmcp"]["tags"]`` while preserving any caller metadata.
    """
    out = dict(meta) if meta else {}
    if tags:
        fastmcp = dict(out.get("fastmcp") or {})
        fastmcp["tags"] = sorted(tags)
        out["fastmcp"] = fastmcp
    return out or None
