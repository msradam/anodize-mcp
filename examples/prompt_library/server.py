"""Prompt-library MCP server.

Exposes a curated set of reusable prompt templates via MCP prompts and resources.

Run over stdio:

    python examples/prompt_library/server.py

"""

from __future__ import annotations

import json
from typing import Literal

from anodize_mcp import AnodizeMCP, PromptMessage, ResourceError, StaticTokenVerifier

auth = StaticTokenVerifier(
    {"library-token": {"scopes": ["read"]}},
    required_scopes=["read"],
)

mcp = AnodizeMCP("prompt-library", version="1.0.0", auth=auth)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, str] = {
    "code_review": (
        "Please review the following {language} code and provide detailed feedback "
        "covering correctness, style, and potential improvements.\n\n"
        "```{language}\n{code}\n```"
    ),
    "summarize": (
        "Summarize the following text.\n\n"
        "Style: {style}\n\n"
        "bullet  — return a bullet-point list\n"
        "paragraph — return flowing prose\n"
        "tldr — return a single sentence\n\n"
        "Text:\n{text}"
    ),
    "debug_help": ("Help me debug the following error.\n\nError:\n{error}\n\nContext:\n{context}"),
}

_DESCRIPTIONS: dict[str, str] = {
    "code_review": "Request a review of a code snippet in a given language.",
    "summarize": "Summarize text in bullet, paragraph, or tldr style.",
    "debug_help": "Get debugging help for an error with optional context.",
}


@mcp.prompt
def code_review(language: str, code: str) -> list[PromptMessage]:
    """Request a code review for a snippet in the given programming language."""
    return [
        PromptMessage(
            role="user",
            text=(
                f"Please review the following {language} code and provide detailed feedback "
                f"covering correctness, style, and potential improvements.\n\n"
                f"```{language}\n{code}\n```"
            ),
        ),
    ]


@mcp.prompt
def summarize(text: str, style: Literal["bullet", "paragraph", "tldr"]) -> list[PromptMessage]:
    """Summarize text in bullet-list, paragraph, or tldr style."""
    style_instructions = {
        "bullet": "Return a bullet-point list of the key points.",
        "paragraph": "Return a concise summary in flowing prose.",
        "tldr": "Return a single sentence that captures the main idea.",
    }
    instruction = style_instructions[style]
    return [
        PromptMessage(
            role="user",
            text=f"Summarize the following text. {instruction}\n\nText:\n{text}",
        ),
    ]


@mcp.prompt
def debug_help(error: str, context: str = "") -> list[PromptMessage]:
    """Return a debugging prompt for the given error and optional context."""
    body = f"Help me debug the following error.\n\nError:\n{error}"
    if context:
        body += f"\n\nContext:\n{context}"
    return [
        PromptMessage(role="user", text=body),
    ]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("prompts://index", mime_type="application/json")
def prompts_index() -> str:
    """Return a JSON list of available prompt names and descriptions."""
    entries = [{"name": name, "description": desc} for name, desc in _DESCRIPTIONS.items()]
    return json.dumps(entries)


@mcp.resource("prompts://template/{name}", mime_type="text/plain")
def prompt_template(name: str) -> str:
    """Return the raw template text for a named prompt."""
    template = _TEMPLATES.get(name)
    if template is None:
        raise ResourceError(f"Unknown prompt template: {name!r}")
    return template


if __name__ == "__main__":
    mcp.run()
