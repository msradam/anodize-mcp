"""Prompt registry records, mirroring ``fastmcp.prompts``."""

from __future__ import annotations

from .prompt import PromptArgument, PromptDef, PromptMessage, normalize_prompt_result

__all__ = ["PromptArgument", "PromptDef", "PromptMessage", "normalize_prompt_result"]
