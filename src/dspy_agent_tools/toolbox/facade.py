"""Discovery facade tools built over a tool catalog."""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from ..utils.dspy import load_tool, sync_tool_class
from .catalog import (
    CatalogEntry,
    _details_record,
    _details_text,
    _search,
    _search_records,
    _search_text,
)

if TYPE_CHECKING:
    from dspy import Tool

_SEARCH_DESC = (
    "Search this tool catalog by name, description, and parameter names. "
    "Returns ranked names with their required arguments. Call "
    "get_tool_details before call_tool when argument shapes are unclear."
)
_DETAILS_DESC = (
    "Return the full description and JSON input schema for one tool found "
    "with search_tools."
)
_CALL_DESC = (
    "Run one tool by name. Pass arguments as an object matching the tool's "
    "input schema (see get_tool_details)."
)
_CODE_SEARCH_DESC = (
    "Search this tool catalog by name, description, and parameter names. "
    "Returns a list of {name, description, required} dicts, best match first."
)
_CODE_DETAILS_DESC = (
    "Return {name, description, required, input_schema} for one tool found "
    "with search_tools."
)
_CODE_CALL_DESC = (
    "Run one tool by name and return its result unchanged. Pass arguments as "
    "a dict matching the tool's input schema."
)


def _as_sync_capable(tool: Tool) -> Tool:
    """Rebuild a facade tool so its synchronous path also works."""
    cls = sync_tool_class()
    return cls(
        func=tool.func,
        name=tool.name,
        desc=tool.desc,
        args=tool.args,
        arg_types=tool.arg_types,
        arg_desc=tool.arg_desc,
    )


def _facade_tool(
    tool_cls: type[Tool],
    func: Callable[..., Any],
    name: str,
    desc: str,
    prefix: str,
) -> Tool:
    """Wrap a discovery function as a sync-capable tool with a stable name."""
    return _as_sync_capable(tool_cls(func, name=f"{prefix}{name}", desc=desc))


def build_facade(
    entries: Sequence[CatalogEntry],
    *,
    mode: Literal["search", "code"],
    name_prefix: str = "",
) -> list[Tool]:
    """Build the three-tool discovery facade over ``entries``."""
    Tool = load_tool(
        "dspy_agent_tools.Toolbox requires DSPy. "
        "Install it with: uv add 'dspy-agent-tools[mcp]'"
    )
    by_name = {entry.name: entry for entry in entries}
    folded = {entry.name.casefold(): entry for entry in entries}

    def resolve(name: str) -> CatalogEntry:
        entry = by_name.get(name) or folded.get(str(name).casefold())
        if entry is None:
            raise ValueError(
                f"Unknown tool {name!r}; call search_tools to find a valid name."
            )
        return entry

    async def run(name: str, arguments: dict[str, Any] | None) -> Any:
        return await resolve(name).tool.acall(**(arguments or {}))

    if mode == "search":

        def search(query: str, limit: int = 5) -> str:
            return _search_text(_search(entries, query, limit))

        def details(name: str) -> str:
            return _details_text(resolve(name))

        async def call(name: str, arguments: dict[str, Any] | None = None) -> Any:
            return await run(name, arguments)

        return [
            _facade_tool(Tool, search, "search_tools", _SEARCH_DESC, name_prefix),
            _facade_tool(Tool, details, "get_tool_details", _DETAILS_DESC, name_prefix),
            _facade_tool(Tool, call, "call_tool", _CALL_DESC, name_prefix),
        ]

    def search_records(query: str, limit: int = 5) -> list[dict[str, Any]]:
        return _search_records(_search(entries, query, limit))

    def details_record(name: str) -> dict[str, Any]:
        return _details_record(resolve(name))

    async def call_record(name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await run(name, arguments)

    return [
        _facade_tool(
            Tool, search_records, "search_tools", _CODE_SEARCH_DESC, name_prefix
        ),
        _facade_tool(
            Tool, details_record, "get_tool_details", _CODE_DETAILS_DESC, name_prefix
        ),
        _facade_tool(Tool, call_record, "call_tool", _CODE_CALL_DESC, name_prefix),
    ]


def _renamed(tool: Tool, name: str) -> Tool:
    """Return ``tool`` under ``name`` without mutating the caller's object."""
    if tool.name == name:
        return tool
    renamed = copy.copy(tool)
    renamed.name = name
    return renamed
