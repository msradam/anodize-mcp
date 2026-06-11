"""Resource registry records, mirroring ``fastmcp.resources``."""

from __future__ import annotations

from .resource import ResourceDef
from .template import ResourceTemplateDef, compile_uri_template

__all__ = ["ResourceDef", "ResourceTemplateDef", "compile_uri_template"]
