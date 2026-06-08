"""A minimal anodize server: tools, a resource, a template, and a prompt.

Run over stdio (the default):

    python examples/quickstart.py

Then point any MCP client at it, e.g. add to a client config:

    {"command": "python", "args": ["examples/quickstart.py"]}
"""

from dataclasses import dataclass
from typing import Annotated

from anodize_mcp import AnodizeMCP, Field

mcp = AnodizeMCP("quickstart", version="1.0.0", instructions="A small demo server.")


@mcp.tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@dataclass
class Weather:
    temperature: float
    conditions: str


@mcp.tool
def get_weather(city: Annotated[str, Field(description="City name")]) -> Weather:
    """Look up the (fake) weather for a city. Returns structured output."""
    return Weather(temperature=21.5, conditions="partly cloudy")


@mcp.resource("config://app", mime_type="application/json")
def app_config() -> str:
    """A static resource."""
    return '{"theme": "dark", "version": "1.0.0"}'


@mcp.resource("file://{path:path}", mime_type="text/plain")
def read_file(path: str) -> str:
    """A resource template: file://<path> reads a path-shaped variable."""
    return f"(pretend contents of /{path})"


@mcp.prompt
def review(code: str) -> str:
    """Generate a code-review prompt."""
    return f"Please review the following code:\n\n{code}"


if __name__ == "__main__":
    mcp.run()
