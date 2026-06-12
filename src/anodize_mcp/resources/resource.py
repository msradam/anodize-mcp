"""The resource registry record, mirroring ``fastmcp.resources.resource``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .._components import build_meta


@dataclass
class ResourceDef:
    uri: str
    handler: Callable[..., Any]
    name: str
    title: Optional[str] = None
    description: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    annotations: Optional[dict[str, Any]] = None
    context_param: Optional[str] = None
    tags: Any = None
    meta: Optional[dict[str, Any]] = None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"uri": self.uri, "name": self.name}
        if self.title is not None:
            out["title"] = self.title
        if self.description is not None:
            out["description"] = self.description
        # FastMCP lists text/plain when no MIME type was declared.
        out["mimeType"] = self.mime_type or "text/plain"
        if self.size is not None:
            out["size"] = self.size
        if self.annotations:
            out["annotations"] = self.annotations
        meta = build_meta(self.meta, self.tags)
        if meta:
            out["_meta"] = meta
        return out
