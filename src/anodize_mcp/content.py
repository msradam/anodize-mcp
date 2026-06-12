"""MCP content blocks and helpers to normalize handler return values.

Each dataclass serializes to the exact JSON the protocol expects via
:meth:`to_dict`. The ``type`` field is accepted but ignored on input (the
official SDK passes ``type="text"`` etc.); ``to_dict`` always emits the correct
literal, so the discriminator can never be wrong.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
from collections import abc as _abc
from dataclasses import dataclass
from typing import Any, Optional, Union

from .protocol import json_default


@dataclass
class TextContent:
    text: str
    annotations: Optional[dict[str, Any]] = None
    type: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "text", "text": self.text}
        if self.annotations:
            out["annotations"] = self.annotations
        return out


@dataclass
class ImageContent:
    data: str  # base64-encoded
    mimeType: str
    annotations: Optional[dict[str, Any]] = None
    type: Optional[str] = None

    @classmethod
    def from_bytes(cls, raw: bytes, mime_type: str) -> ImageContent:
        return cls(data=base64.b64encode(raw).decode("ascii"), mimeType=mime_type)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "image", "data": self.data, "mimeType": self.mimeType}
        if self.annotations:
            out["annotations"] = self.annotations
        return out


@dataclass
class AudioContent:
    data: str  # base64-encoded
    mimeType: str
    annotations: Optional[dict[str, Any]] = None
    type: Optional[str] = None

    @classmethod
    def from_bytes(cls, raw: bytes, mime_type: str) -> AudioContent:
        return cls(data=base64.b64encode(raw).decode("ascii"), mimeType=mime_type)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "audio", "data": self.data, "mimeType": self.mimeType}
        if self.annotations:
            out["annotations"] = self.annotations
        return out


@dataclass
class ResourceLink:
    uri: str
    name: str
    description: Optional[str] = None
    mimeType: Optional[str] = None
    annotations: Optional[dict[str, Any]] = None
    type: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "resource_link", "uri": self.uri, "name": self.name}
        if self.description is not None:
            out["description"] = self.description
        if self.mimeType is not None:
            out["mimeType"] = self.mimeType
        if self.annotations:
            out["annotations"] = self.annotations
        return out


@dataclass
class EmbeddedResource:
    uri: str
    text: Optional[str] = None
    blob: Optional[str] = None  # base64-encoded
    mimeType: Optional[str] = None
    annotations: Optional[dict[str, Any]] = None
    type: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        resource: dict[str, Any] = {"uri": self.uri}
        if self.mimeType is not None:
            resource["mimeType"] = self.mimeType
        if self.text is not None:
            resource["text"] = self.text
        if self.blob is not None:
            resource["blob"] = self.blob
        if self.annotations:
            resource["annotations"] = self.annotations
        return {"type": "resource", "resource": resource}


ContentBlock = Union[TextContent, ImageContent, AudioContent, ResourceLink, EmbeddedResource]


@dataclass
class ResourceContents:
    """A single item returned from ``resources/read``."""

    uri: str
    text: Optional[str] = None
    blob: Optional[str] = None  # base64-encoded
    mimeType: Optional[str] = None

    @classmethod
    def from_bytes(cls, uri: str, raw: bytes, mime_type: Optional[str] = None) -> ResourceContents:
        return cls(
            uri=uri,
            blob=base64.b64encode(raw).decode("ascii"),
            mimeType=mime_type or "application/octet-stream",
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"uri": self.uri}
        if self.mimeType is not None:
            out["mimeType"] = self.mimeType
        if self.text is not None:
            out["text"] = self.text
        if self.blob is not None:
            out["blob"] = self.blob
        return out


def _is_content_block(value: Any) -> bool:
    return isinstance(
        value, (TextContent, ImageContent, AudioContent, ResourceLink, EmbeddedResource)
    )


def _model_dump(value: Any) -> Optional[dict[str, Any]]:
    """JSON-safe dict for a pydantic-style model, or None if ``value`` isn't one."""
    if isinstance(value, type):
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):  # pydantic v2
        with contextlib.suppress(Exception):
            return dump(mode="json")
    if hasattr(value, "__fields__") and callable(getattr(value, "dict", None)):  # pydantic v1
        with contextlib.suppress(Exception):
            return value.dict()
    return None


def is_content_value(value: Any) -> bool:
    """True if ``value`` is content: a block, an Image/Audio/File helper, or a
    non-empty list containing any of those."""
    if _is_content_block(value) or callable(getattr(value, "to_content_block", None)):
        return True
    return (
        isinstance(value, list)
        and bool(value)
        and any(
            _is_content_block(v) or callable(getattr(v, "to_content_block", None)) for v in value
        )
    )


def to_jsonable(value: Any) -> Any:
    """Return a fully JSON-safe copy of ``value`` for ``structuredContent``.

    Round-tripping through :func:`json_default` converts dataclasses, bytes,
    datetimes, etc. (including nested ones) so the structured payload never
    carries a type the client cannot parse. Non-finite floats are rejected
    (RFC 8259 has no Infinity/NaN; FastMCP errors on them too).
    """
    return json.loads(json.dumps(value, default=json_default, allow_nan=False))


def normalize_tool_result(value: Any) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """Turn a tool's return value into ``(content, structuredContent)``.

    Rules, applied in order:

    * ``None`` -> empty content.
    * A content block, or a list of them -> used as-is.
    * ``str`` -> a single text block.
    * ``bytes`` -> base64 image/octet-stream is ambiguous, so it becomes a
      text block of the decoded latin-1 escape; callers wanting binary should
      return an :class:`ImageContent`/:class:`AudioContent` explicitly.
    * ``dict`` or dataclass -> JSON-serialized into a text block *and* returned
      as ``structuredContent`` so output schemas work.
    * anything else -> ``str()`` into a text block.
    """
    if value is None:
        return [], None

    if _is_content_block(value):
        return [value.to_dict()], None

    # Image/File helpers (and anything else) that can turn themselves into a block.
    maker = getattr(value, "to_content_block", None)
    if callable(maker):
        return [maker().to_dict()], None

    if isinstance(value, list) and value and all(_is_content_block(v) for v in value):
        return [v.to_dict() for v in value], None

    # A mixed list containing content blocks or Image/Audio/File helpers:
    # each element becomes its own block (text for strings, JSON text
    # otherwise), as FastMCP converts untyped sequence returns.
    if (
        isinstance(value, list)
        and value
        and any(
            _is_content_block(v) or callable(getattr(v, "to_content_block", None)) for v in value
        )
    ):
        blocks = []
        for item in value:
            if _is_content_block(item):
                blocks.append(item.to_dict())
            elif callable(getattr(item, "to_content_block", None)):
                blocks.append(item.to_content_block().to_dict())
            elif isinstance(item, str):
                blocks.append(TextContent(item).to_dict())
            else:
                blocks.append(TextContent(_json_text(to_jsonable(item))).to_dict())
        return blocks, None

    if isinstance(value, str):
        return [TextContent(value).to_dict()], None

    if isinstance(value, bytes):
        return [TextContent(value.decode("utf-8", errors="replace")).to_dict()], None

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        structured = dataclasses.asdict(value)
        return [TextContent(_json_text(structured)).to_dict()], structured

    # A pydantic-style model returned by a tool becomes structured content.
    model_structured = _model_dump(value)
    if model_structured is not None:
        return [TextContent(_json_text(model_structured)).to_dict()], model_structured

    if isinstance(value, dict):
        return [TextContent(_json_text(value)).to_dict()], value

    if isinstance(value, (int, float, bool)):
        return [TextContent(json.dumps(value, allow_nan=False)).to_dict()], None

    if isinstance(value, (list, tuple, set)):
        # JSON text, as FastMCP serializes sequence returns (not Python repr).
        return [TextContent(_json_text(to_jsonable(value))).to_dict()], None

    # Generators and other lazy iterables are exhausted and serialized like
    # lists, as pydantic's serializer does; never their repr.
    if isinstance(value, _abc.Iterator):
        return normalize_tool_result(list(value))

    # Anything else serializes as JSON (datetimes as quoted ISO strings, and
    # so on), the way FastMCP's pydantic fallback does; str() is the last resort.
    try:
        return [TextContent(_json_text(to_jsonable(value))).to_dict()], None
    except (TypeError, ValueError):
        return [TextContent(str(value)).to_dict()], None


def _json_text(value: Any) -> str:
    # Compact separators, matching FastMCP's pydantic_core serialization.
    return json.dumps(value, default=json_default, separators=(",", ":"), allow_nan=False)


def normalize_resource_result(
    uri: str,
    value: Any,
    mime_type: Optional[str] = None,
    *,
    json_mime_default: str = "text/plain",
) -> list[dict[str, Any]]:
    """Turn a resource handler's return value into a ``contents`` array."""
    if isinstance(value, ResourceContents):
        return [value.to_dict()]
    # Only a list of explicit ResourceContents means multiple entries; any
    # other list is one JSON document, matching FastMCP.
    if isinstance(value, list) and value and all(isinstance(v, ResourceContents) for v in value):
        return [v.to_dict() for v in value]
    if isinstance(value, str):
        return [ResourceContents(uri=uri, text=value, mimeType=mime_type or "text/plain").to_dict()]
    if isinstance(value, bytes):
        return [ResourceContents.from_bytes(uri, value, mime_type).to_dict()]
    # Anything else is serialized as JSON text; the default label differs by
    # path (FastMCP: text/plain for static resources, application/json for
    # template reads), so the caller supplies it.
    return [
        ResourceContents(
            uri=uri,
            text=json.dumps(value, default=json_default),
            mimeType=mime_type or json_mime_default,
        ).to_dict()
    ]
