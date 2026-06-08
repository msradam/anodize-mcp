"""An agentic server: sampling, elicitation, completions, and HTTP.

This file is written in FastMCP's async style (``async def`` tools, ``await
ctx.*``), so it runs on anodize today and on FastMCP later by changing only the
import line below. Run over Streamable HTTP:

    python examples/agentic.py

The server listens on http://127.0.0.1:8000/mcp. A client that declares the
``sampling`` and ``elicitation`` capabilities and keeps a GET SSE stream open
can drive the round-trips.
"""

from dataclasses import dataclass

from anodize_mcp import AnodizeMCP as FastMCP  # swap for `from fastmcp import FastMCP`
from anodize_mcp import Context

mcp = FastMCP("agentic", version="1.0.0")


@mcp.tool
async def summarize(text: str, ctx: Context) -> str:
    """Summarize text by delegating to the client's LLM (sampling)."""
    result = await ctx.sample(
        f"Summarize in one sentence:\n\n{text}",
        system_prompt="You are concise.",
        max_tokens=100,
    )
    return result.text or "(no summary)"


@dataclass
class Confirmation:
    confirmed: bool
    reason: str


@mcp.tool
async def deploy(service: str, ctx: Context) -> str:
    """Ask the user to confirm before 'deploying' (elicitation)."""
    answer = await ctx.elicit(f"Really deploy {service!r}?", Confirmation)
    if answer.action != "accept":
        return f"Cancelled ({answer.action})."
    if not answer.data.confirmed:
        return f"Declined: {answer.data.reason or 'no reason given'}"
    return f"Deployed {service}."


@mcp.tool
async def progressive(steps: int, ctx: Context) -> str:
    """Report progress as it works (notifications/progress)."""
    for i in range(steps):
        await ctx.report_progress(i + 1, total=steps, message=f"step {i + 1}")
    return f"Completed {steps} steps."


@mcp.prompt
def greeting(language: str) -> str:
    """A greeting in the requested language."""
    return f"Write a friendly greeting in {language}."


@mcp.complete_prompt("greeting")
def complete_language(argument: str, value: str) -> list[str]:
    languages = ["english", "spanish", "french", "german", "japanese"]
    return [lang for lang in languages if lang.startswith(value.lower())]


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000)
