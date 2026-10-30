"""Streamable HTTP / SSE MCP server used by tests/test_mcp_remote.py."""

import argparse

from mcp.server import MCPServer

server = MCPServer("dspy-agent-tools remote test server")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


@server.tool()
def echo(text: str) -> str:
    """Echo the supplied text back unchanged."""
    return text


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("transport", choices=["streamable-http", "sse"])
    parser.add_argument("port", type=int)
    args = parser.parse_args()
    server.run(transport=args.transport, host="127.0.0.1", port=args.port)
