"""Types and normalization for server-initiated requests to the client.

These back the :class:`~anodize_mcp.context.Context` methods ``sample``,
``elicit``, and ``list_roots``. The server asks the client to run the LLM
(sampling), ask the user (elicitation), or report its filesystem roots.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Optional, Union

from . import schema as _schema


@dataclass
class CreateMessageResult:
    """The client's reply to ``sampling/createMessage``."""

    role: str
    content: dict[str, Any]
    model: Optional[str] = None
    stop_reason: Optional[str] = None

    @property
    def text(self) -> Optional[str]:
        if self.content.get("type") == "text":
            return self.content.get("text")
        return None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CreateMessageResult:
        return cls(
            role=data.get("role", "assistant"),
            content=data.get("content", {}),
            model=data.get("model"),
            stop_reason=data.get("stopReason"),
        )


@dataclass
class ElicitResult:
    """The client's reply to ``elicitation/create``.

    ``action`` is ``"accept"``, ``"decline"``, or ``"cancel"``. ``data`` is the
    user's content: a dataclass instance when ``elicit`` was given a dataclass,
    otherwise the raw dict (matching FastMCP's typed ``.data``). FastMCP's
    client-side class names the payload ``content``; both spellings work here.
    """

    action: str
    data: Any = None
    content: Any = None

    def __post_init__(self) -> None:
        if self.data is None and self.content is not None:
            self.data = self.content
        elif self.content is None and self.data is not None:
            self.content = self.data

    @property
    def accepted(self) -> bool:
        return self.action == "accept"

    def __bool__(self) -> bool:
        return self.accepted


@dataclass
class Root:
    uri: str
    name: Optional[str] = None


@dataclass
class CompletionResult:
    """A richer return type for completion handlers than a bare list.

    ``total`` and ``has_more`` are optional hints; if omitted the server infers
    ``has_more`` from whether the values were truncated to the 100-item cap.
    """

    values: list[str]
    total: Optional[int] = None
    has_more: Optional[bool] = None


def normalize_sampling_messages(messages: Any) -> list[dict[str, Any]]:
    """Accept a string, one message, or a list and produce sampling messages."""
    if isinstance(messages, str):
        return [{"role": "user", "content": {"type": "text", "text": messages}}]
    if isinstance(messages, dict):
        return [messages]
    if isinstance(messages, (list, tuple)):
        out: list[dict[str, Any]] = []
        for item in messages:
            if isinstance(item, str):
                out.append({"role": "user", "content": {"type": "text", "text": item}})
            elif isinstance(item, dict):
                out.append(item)
        return out
    raise TypeError(f"unsupported sampling messages: {type(messages)!r}")


def elicitation_schema(requested: Union[dict[str, Any], type]) -> dict[str, Any]:
    """Build the ``requestedSchema`` (a flat object of primitives) for elicitation.

    Accepts a JSON Schema dict, a dataclass, a pydantic model, or a scalar type
    (``str``/``int``/``float``/``bool``), matching FastMCP's response types. A
    scalar is wrapped in a single ``value`` field.
    """
    if isinstance(requested, dict):
        return requested
    if dataclasses.is_dataclass(requested) and isinstance(requested, type):
        return _schema.type_to_schema(requested)
    if isinstance(requested, type) and hasattr(requested, "model_json_schema"):
        return requested.model_json_schema()
    if requested in (str, int, float, bool):
        return {
            "type": "object",
            "properties": {"value": _schema.type_to_schema(requested)},
            "required": ["value"],
        }
    raise TypeError("elicit schema must be a dict, dataclass, model, or scalar type")
