"""Internal registry records and prompt message types."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .schema import ParamSpec


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

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "inputSchema": self.input_schema}
        if self.title is not None:
            out["title"] = self.title
        if self.description is not None:
            out["description"] = self.description
        if self.output_schema is not None:
            out["outputSchema"] = self.output_schema
        if self.annotations:
            out["annotations"] = self.annotations
        return out


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

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"uri": self.uri, "name": self.name}
        if self.title is not None:
            out["title"] = self.title
        if self.description is not None:
            out["description"] = self.description
        if self.mime_type is not None:
            out["mimeType"] = self.mime_type
        if self.size is not None:
            out["size"] = self.size
        if self.annotations:
            out["annotations"] = self.annotations
        return out


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

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"uriTemplate": self.uri_template, "name": self.name}
        if self.title is not None:
            out["title"] = self.title
        if self.description is not None:
            out["description"] = self.description
        if self.mime_type is not None:
            out["mimeType"] = self.mime_type
        return out

    def match(self, uri: str) -> Optional[dict[str, str]]:
        m = self.pattern.match(uri)
        if m is None:
            return None
        return {k: _unquote(v) for k, v in m.groupdict().items()}


@dataclass
class PromptArgument:
    name: str
    description: Optional[str] = None
    required: bool = False

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.description is not None:
            out["description"] = self.description
        if self.required:
            out["required"] = True
        return out


@dataclass
class PromptDef:
    name: str
    handler: Callable[..., Any]
    arguments: list[PromptArgument]
    param_specs: list[ParamSpec]
    title: Optional[str] = None
    description: Optional[str] = None
    context_param: Optional[str] = None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.title is not None:
            out["title"] = self.title
        if self.description is not None:
            out["description"] = self.description
        if self.arguments:
            out["arguments"] = [a.describe() for a in self.arguments]
        return out


@dataclass
class PromptMessage:
    role: str  # "user" or "assistant"
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": {"type": "text", "text": self.text}}


def normalize_prompt_result(value: Any) -> dict[str, Any]:
    """Turn a prompt handler's return value into a ``prompts/get`` result."""
    if isinstance(value, dict) and "messages" in value:
        return value
    messages = _normalize_messages(value)
    return {"messages": messages}


def _normalize_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [PromptMessage("user", value).to_dict()]
    if isinstance(value, PromptMessage):
        return [value.to_dict()]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            out.extend(_normalize_messages(item))
        return out
    return [PromptMessage("user", str(value)).to_dict()]


def compile_uri_template(template: str) -> tuple[re.Pattern[str], list[str]]:
    """Compile an RFC-6570-style URI template into a regex and variable list.

    Supported variable forms:

    * ``{name}``      matches a single path segment (no ``/``).
    * ``{name:path}`` matches greedily, including ``/`` (for file paths).
    """
    names: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(template):
        ch = template[i]
        if ch == "{":
            end = template.index("}", i)
            spec = template[i + 1 : end]
            if ":" in spec:
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
