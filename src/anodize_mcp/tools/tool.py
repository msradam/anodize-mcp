"""The tool registry record, mirroring ``fastmcp.tools.tool``.

FastMCP's ``Tool`` is a pydantic model; ``ToolDef`` is the plain-dataclass
equivalent, with the JSON Schema produced by anodize's stdlib schema generator
(:mod:`anodize_mcp.schema`) rather than pydantic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .._components import build_meta
from ..schema import ParamSpec


@dataclass
class ToolResult:
    """An explicit tool result: content, structured content, and metadata together.

    Return one from a tool to control all three at once, mirroring FastMCP's
    ``ToolResult``. ``content`` may be a string, a content block, or a list of
    them. At least one of ``content`` and ``structured_content`` is required;
    a structured-only result backfills ``content`` with the JSON text, as
    FastMCP does, so text-only clients still see output.
    """

    content: Any = None
    structured_content: Any = None
    meta: Any = None
    is_error: bool = False

    def __post_init__(self) -> None:
        if self.content is None and self.structured_content is None:
            raise ValueError("Either content or structured_content must be provided")
        if self.content is None:
            from ..content import to_jsonable

            self.content = json.dumps(to_jsonable(self.structured_content))


@dataclass
class ToolDef:
    name: str
    handler: Callable[..., Any]
    param_specs: list[ParamSpec]
    input_schema: dict[str, Any]
    title: Optional[str] = None
    description: Optional[str] = None
    output_schema: Optional[dict[str, Any]] = None
    wrap_output: bool = False
    annotations: Optional[dict[str, Any]] = None
    context_param: Optional[str] = None
    tags: Any = None
    meta: Optional[dict[str, Any]] = None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "inputSchema": self.input_schema}
        # FastMCP falls back to annotations.title when no explicit title.
        title = self.title
        if title is None and self.annotations:
            if isinstance(self.annotations, dict):
                title = self.annotations.get("title")
            else:
                title = getattr(self.annotations, "title", None)
        if title is not None:
            out["title"] = title
        if self.description is not None:
            out["description"] = self.description
        if self.output_schema is not None:
            out["outputSchema"] = self.output_schema
        if self.annotations:
            out["annotations"] = self.annotations
        meta = build_meta(self.meta, self.tags)
        if meta:
            out["_meta"] = meta
        return out
