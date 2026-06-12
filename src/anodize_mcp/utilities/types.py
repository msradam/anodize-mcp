"""Content helpers, mirroring ``fastmcp.utilities.types``.

Return an :class:`Image`, :class:`Audio`, or :class:`File` from a tool and it
becomes the matching MCP content block. Construct from raw bytes or a
filesystem path (exactly one of the two).
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any, Optional, Union

from ..content import AudioContent, EmbeddedResource, ImageContent


def _expanded_path(path: Union[str, Path, None]) -> Optional[Path]:
    return Path(os.path.expandvars(str(path))).expanduser() if path else None


def _require_one_source(path: Any, data: Any) -> None:
    if path is None is data:
        raise ValueError("Either path or data must be provided")
    if path is not None and data is not None:
        raise ValueError("Only one of path or data can be provided")


class Image:
    """An image result; becomes an ``ImageContent`` block when returned by a tool."""

    def __init__(
        self,
        path: Union[str, Path, None] = None,
        data: Optional[bytes] = None,
        format: Optional[str] = None,
        annotations: Optional[dict[str, Any]] = None,
    ):
        _require_one_source(path, data)
        self.path = _expanded_path(path)
        self.data = data
        self._format = format
        self.annotations = annotations

    @property
    def mime_type(self) -> str:
        if self._format:
            return f"image/{self._format.lower()}"
        if self.path:
            # mimetypes lacks webp before Python 3.11.
            mimetypes.add_type("image/webp", ".webp")
            guessed = mimetypes.guess_type(str(self.path), strict=False)[0]
            return guessed or "application/octet-stream"
        return "image/png"

    def _read(self) -> bytes:
        if self.path is not None:
            return self.path.read_bytes()
        assert self.data is not None
        return self.data

    def to_content_block(self) -> ImageContent:
        block = ImageContent.from_bytes(self._read(), self.mime_type)
        block.annotations = self.annotations
        return block


class Audio:
    """An audio result; becomes an ``AudioContent`` block when returned by a tool."""

    def __init__(
        self,
        path: Union[str, Path, None] = None,
        data: Optional[bytes] = None,
        format: Optional[str] = None,
        annotations: Optional[dict[str, Any]] = None,
    ):
        _require_one_source(path, data)
        self.path = _expanded_path(path)
        self.data = data
        self._format = format
        self.annotations = annotations

    @property
    def mime_type(self) -> str:
        if self._format:
            return f"audio/{self._format.lower()}"
        if self.path:
            return {
                ".wav": "audio/wav",
                ".mp3": "audio/mpeg",
                ".ogg": "audio/ogg",
                ".m4a": "audio/mp4",
                ".flac": "audio/flac",
            }.get(self.path.suffix.lower(), "application/octet-stream")
        return "audio/wav"

    def _read(self) -> bytes:
        if self.path is not None:
            return self.path.read_bytes()
        assert self.data is not None
        return self.data

    def to_content_block(self) -> AudioContent:
        block = AudioContent.from_bytes(self._read(), self.mime_type)
        block.annotations = self.annotations
        return block


class File:
    """A file result; becomes an embedded-resource block when returned.

    Text MIME types embed decoded text; everything else embeds a base64 blob,
    as FastMCP does.
    """

    def __init__(
        self,
        path: Union[str, Path, None] = None,
        data: Optional[bytes] = None,
        format: Optional[str] = None,
        name: Optional[str] = None,
        annotations: Optional[dict[str, Any]] = None,
    ):
        _require_one_source(path, data)
        self.path = _expanded_path(path)
        self.data = data
        self._format = format
        self.name = name
        self.annotations = annotations

    @property
    def mime_type(self) -> str:
        if self._format:
            fmt = self._format.lower()
            if fmt in {"plain", "txt", "text"}:
                return "text/plain"
            return f"application/{fmt}"
        if self.path:
            guessed = mimetypes.guess_type(str(self.path))[0]
            if guessed:
                return guessed
        return "application/octet-stream"

    def to_content_block(self) -> EmbeddedResource:
        mime = self.mime_type
        if self.path is not None:
            raw = self.path.read_bytes()
            uri = self.path.resolve().as_uri()
        else:
            assert self.data is not None
            raw = self.data
            stem = self.name or "resource"
            uri = f"file:///{stem}.{mime.split('/')[1]}"
        if mime.startswith("text/"):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            return EmbeddedResource(uri=uri, text=text, mimeType=mime, annotations=self.annotations)
        return EmbeddedResource(
            uri=uri,
            blob=base64.b64encode(raw).decode("ascii"),
            mimeType=mime,
            annotations=self.annotations,
        )
