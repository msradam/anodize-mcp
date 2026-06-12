"""Resource templates, mirroring ``fastmcp.resources.template``.

A template is a parameterized URI (RFC-6570 style); :func:`compile_uri_template`
turns it into a regex, and :meth:`ResourceTemplateDef.match` extracts the
variables from a concrete URI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .._components import build_meta


@dataclass
class ResourceTemplateDef:
    uri_template: str
    handler: Callable[..., Any]
    name: str
    param_names: list[str]
    pattern: re.Pattern[str]
    title: Optional[str] = None
    description: Optional[str] = None
    mime_type: Optional[str] = None
    context_param: Optional[str] = None
    tags: Any = None
    meta: Optional[dict[str, Any]] = None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"uriTemplate": self.uri_template, "name": self.name}
        if self.title is not None:
            out["title"] = self.title
        if self.description is not None:
            out["description"] = self.description
        if self.mime_type is not None:
            out["mimeType"] = self.mime_type
        meta = build_meta(self.meta, self.tags)
        if meta:
            out["_meta"] = meta
        return out

    def match(self, uri: str) -> Optional[dict[str, str]]:
        m = self.pattern.match(uri)
        if m is None:
            return None
        return {k: _unquote(v) for k, v in m.groupdict().items()}


def compile_uri_template(template: str) -> tuple[re.Pattern[str], list[str]]:
    """Compile an RFC-6570-style URI template into a regex and variable list.

    Supported variable forms:

    * ``{name}``      matches a single path segment (no ``/``).
    * ``{name*}``     RFC 6570 explode, FastMCP's wildcard: matches across ``/``.
    * ``{name:path}`` the equivalent Starlette-style spelling.
    """
    names: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(template):
        ch = template[i]
        if ch == "{":
            end = template.index("}", i)
            spec = template[i + 1 : end]
            if spec.endswith("*"):
                name, modifier = spec[:-1], "path"
            elif ":" in spec:
                name, modifier = spec.split(":", 1)
            else:
                name, modifier = spec, ""
            names.append(name)
            if modifier == "path":
                out.append(f"(?P<{name}>.+)")
            else:
                out.append(f"(?P<{name}>[^/]+)")
            i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$"), names


def _unquote(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)
