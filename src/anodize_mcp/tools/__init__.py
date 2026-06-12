"""Tool registry records, mirroring ``fastmcp.tools``."""

from __future__ import annotations

from .tool import ToolDef, ToolResult

# FastMCP's component class is named Tool; ToolDef is the anodize equivalent.
Tool = ToolDef

__all__ = ["Tool", "ToolDef", "ToolResult"]
