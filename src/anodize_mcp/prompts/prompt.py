"""Prompt registry records and message types, mirroring ``fastmcp.prompts.prompt``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .._components import build_meta
from ..schema import ParamSpec


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
    tags: Any = None
    meta: Optional[dict[str, Any]] = None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.title is not None:
            out["title"] = self.title
        if self.description is not None:
            out["description"] = self.description
        if self.arguments:
            out["arguments"] = [a.describe() for a in self.arguments]
        meta = build_meta(self.meta, self.tags)
        if meta:
            out["_meta"] = meta
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
    return {"messages": _normalize_messages(value)}


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
