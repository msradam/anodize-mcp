"""Response-limiting middleware.

Truncates tool call text content that exceeds a configured byte limit.
"""

from __future__ import annotations

from typing import Any, Optional

from .middleware import Middleware, MiddlewareContext


class ResponseLimitingMiddleware(Middleware):
    """Truncate tool responses whose text content exceeds *max_size* bytes.

    Parameters
    ----------
    max_size:
        Maximum allowed byte length of any single text content block.
    truncation_suffix:
        String appended after truncation so callers can detect it.
    tools:
        Tool name allowlist. ``None`` applies the limit to every tool.
    """

    def __init__(
        self,
        max_size: int,
        truncation_suffix: str = "...",
        tools: Optional[list[str]] = None,
    ):
        self.max_size = max_size
        self.truncation_suffix = truncation_suffix
        self.tools = set(tools) if tools is not None else None

    def _truncate_text(self, text: str) -> str:
        encoded = text.encode("utf-8")
        if len(encoded) <= self.max_size:
            return text
        suffix = self.truncation_suffix
        suffix_bytes = suffix.encode("utf-8")
        keep = max(0, self.max_size - len(suffix_bytes))
        # Slice on bytes then decode safely to avoid splitting a multibyte char.
        truncated = encoded[:keep].decode("utf-8", errors="ignore")
        return truncated + suffix

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        tool_name = getattr(context.message, "name", None)
        if self.tools is not None and tool_name not in self.tools:
            return await call_next(context)

        result = await call_next(context)

        if not isinstance(result, dict):
            return result

        content = result.get("content")
        if not isinstance(content, list):
            return result

        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                truncated = self._truncate_text(text)
                if truncated != text:
                    block = {**block, "text": truncated}
            new_content.append(block)

        return {**result, "content": new_content}
