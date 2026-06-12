"""Content helpers, mirroring ``fastmcp.utilities.types``.

Return an :class:`Image` or :class:`File` from a tool and it becomes the matching
MCP content block. Construct from raw bytes or a filesystem path.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Optional, Union

from ..content import EmbeddedResource, ImageContent


class Image:
    """An image result; becomes an ``ImageContent`` block when returned by a tool."""

    def __init__(
        self,
        path: Union[str, Path, None] = None,
        data: Optional[bytes] = None,
        format: Optional[str] = None,
        annotations: Optional[dict[str, Any]] = None,
    ):
        if path is not None:
            p = Path(path)
            data = p.read_bytes()
            format = format or p.suffix.lstrip(".") or "png"
        if data is None:
            raise ValueError("Image requires either path or data")
        self.data = data
        self._format = (format or "png").lower()
        self.annotations = annotations

    @property
    def mime_type(self) -> str:
        return f"image/{self._format}"

    def to_content_block(self) -> ImageContent:
        block = ImageContent.from_bytes(self.data, self.mime_type)
        block.annotations = self.annotations
        return block


class File:
    """A binary file result; becomes an embedded-resource block when returned."""

    def __init__(
        self,
        path: Union[str, Path, None] = None,
        data: Optional[bytes] = None,
        format: Optional[str] = None,
        name: Optional[str] = None,
        annotations: Optional[dict[str, Any]] = None,
    ):
        if path is not None:
            p = Path(path)
            data = p.read_bytes()
            name = name or p.name
            format = format or (mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        if data is None:
            raise ValueError("File requires either path or data")
        self.data = data
        self.name = name or "file"
        self.mime_type = format or "application/octet-stream"
        self.annotations = annotations

    def to_content_block(self) -> EmbeddedResource:
        return EmbeddedResource(
            uri=f"file:///{self.name}",
            blob=base64.b64encode(self.data).decode("ascii"),
            mimeType=self.mime_type,
            annotations=self.annotations,
        )
