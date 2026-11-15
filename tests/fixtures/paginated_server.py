"""Low-level MCP server exercising cursors and slow tool discovery."""

import asyncio
import sys
from typing import Any

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)


async def list_tools(
    ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
) -> ListToolsResult:
    if "--slow-list" in sys.argv:
        await asyncio.sleep(60)
    cursor = None if params is None else params.cursor
    name = "first" if cursor is None else "second"
    return ListToolsResult(
        tools=[Tool(name=name, input_schema={"type": "object"})],
        next_cursor="second-page" if cursor is None else None,
    )


async def call_tool(
    ctx: ServerRequestContext[Any], params: CallToolRequestParams
) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=params.name)])


server = Server("paginated test", on_list_tools=list_tools, on_call_tool=call_tool)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
