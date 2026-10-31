"""Stdio MCP server whose read-only tools fail transiently, used by tests."""

import importlib

from mcp.types import ToolAnnotations

try:  # MCP SDK v1
    _server_module = importlib.import_module("mcp.server.fastmcp")
    MCPServer = getattr(_server_module, "FastMCP")  # noqa: B009 - v1/v2 compatibility
except ModuleNotFoundError:  # MCP SDK v2
    _server_module = importlib.import_module("mcp.server")
    MCPServer = getattr(_server_module, "MCPServer")  # noqa: B009 - v1/v2 compatibility

_annotation_field = (
    "read_only_hint"
    if "read_only_hint" in ToolAnnotations.model_fields
    else "readOnlyHint"
)
READ_ONLY = ToolAnnotations.model_validate({_annotation_field: True})

server = MCPServer("dspy-agent-tools retry test server")

_flaky_read_calls = 0


@server.tool(annotations=READ_ONLY)
def flaky_read(text: str) -> dict:
    """Answer with an error twice, then echo the text."""
    global _flaky_read_calls
    _flaky_read_calls += 1
    if _flaky_read_calls < 3:
        return {"error": "still starting"}
    return {"echo": text}


@server.tool(annotations=READ_ONLY)
def always_fails(text: str) -> dict:
    """Always answer with an error payload."""
    return {"error": "still unavailable", "text": text}


@server.tool()
def flaky_write(text: str) -> dict:
    """Reject a write with an error payload; it must never be replayed."""
    return {"error": "write refused", "text": text}


if __name__ == "__main__":
    server.run()
