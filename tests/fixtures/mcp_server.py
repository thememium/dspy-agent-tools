"""Minimal stdio MCP server used by tests/test_mcp.py."""

import importlib

try:  # MCP SDK v1
    _server_module = importlib.import_module("mcp.server.fastmcp")
    MCPServer = getattr(_server_module, "FastMCP")  # noqa: B009 - v1/v2 compatibility
except ModuleNotFoundError:  # MCP SDK v2
    _server_module = importlib.import_module("mcp.server")
    MCPServer = getattr(_server_module, "MCPServer")  # noqa: B009 - v1/v2 compatibility

server = MCPServer("dspy-agent-tools test server")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


@server.tool()
def echo(text: str) -> str:
    """Echo the supplied text back unchanged."""
    return text


if __name__ == "__main__":
    server.run()
