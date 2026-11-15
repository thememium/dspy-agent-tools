"""The :class:`Toolbox` tool catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Self

from ..utils.dspy import load_tool
from .catalog import CatalogEntry, _tool_entry
from .facade import _renamed, build_facade

if TYPE_CHECKING:
    from dspy import Tool

_TOOLBOX_MODES = ("search", "code")


def _collect_entries(
    tools: Any, Tool: type[Tool], name_prefix: str
) -> tuple[list[CatalogEntry], list[Any]]:
    """Flatten mixed tool sources into catalog entries and owned sources."""
    entries: list[CatalogEntry] = []
    sources: list[Any] = []
    seen: set[str] = set()

    def add(entry: CatalogEntry) -> None:
        key = entry.name.casefold()
        if key in seen:
            raise ValueError(
                f"Duplicate tool name {entry.name!r}; rename one tool or pass "
                "name_prefix=... to Toolbox."
            )
        seen.add(key)
        entries.append(entry)

    def supplied_entries(node: Any) -> tuple[CatalogEntry, ...] | None:
        """Use an existing catalog (for example ``MCPTools.entries``)."""
        items = getattr(node, "entries", None)
        if (
            not isinstance(items, (list, tuple))
            or not items
            or not isinstance(node, (list, tuple))
            or len(items) != len(node)
        ):
            return None
        if all(isinstance(item, CatalogEntry) for item in items):
            return tuple(items)
        return None

    def walk(node: Any) -> None:
        if isinstance(node, Toolbox):
            sources.append(node)
            for entry in node.entries:
                add(
                    CatalogEntry(
                        name=f"{name_prefix}{entry.name}",
                        description=entry.description,
                        schema=entry.schema,
                        tool=entry.tool,
                    )
                )
            return
        catalog = supplied_entries(node)
        if catalog is not None:
            sources.append(node)
            for entry in catalog:
                add(
                    CatalogEntry(
                        name=f"{name_prefix}{entry.name}",
                        description=entry.description,
                        schema=entry.schema,
                        tool=entry.tool,
                    )
                )
            return
        if isinstance(node, Tool):
            add(_tool_entry(node, name_prefix))
            return
        if callable(node) and not isinstance(node, (str, bytes)):
            add(_tool_entry(Tool(node), name_prefix))
            return
        if isinstance(node, (str, bytes)):
            raise TypeError(
                "Toolbox entries must be dspy.Tool, callables, or iterables of "
                f"them; got {type(node).__name__}"
            )
        close = getattr(node, "close", None)
        if callable(close):
            sources.append(node)
        try:
            items = iter(node)
        except TypeError as exc:
            raise TypeError(
                "Toolbox entries must be dspy.Tool, callables, or iterables of "
                f"them; got {type(node).__name__}"
            ) from exc
        for item in items:
            walk(item)

    walk(tools)
    return entries, sources


class Toolbox(list):
    """Tool catalog that loads eagerly or exposes a discovery facade.

    ``Toolbox`` accepts MCP servers (pass the result of ``MCP(...)``), regular
    ``dspy.Tool`` objects, and plain sync or async Python functions, and
    flattens nested lists and generators.

    With the default ``mode=None`` every tool is returned as its own
    ``dspy.Tool`` — the full load. ``mode="search"`` returns three discovery
    tools with text results for ``dspy.ReAct`` / ``dspy.ReActV2``.
    ``mode="code"`` returns the same three tools with structured
    ``list[dict]`` / ``dict`` results for ``dspy.RLM``.

    Tool names must be unique (case-insensitively); use ``name_prefix`` or
    rename the function. Sources that own resources, such as ``MCPTools`` and
    nested ``Toolbox`` instances, are closed by :meth:`close`.

    Example:
        >>> from dspy_agent_tools import MCP, Toolbox, bash_exec, read_file
        >>> catalog = Toolbox(
        ...     [MCP(url="https://example.com/mcp"), bash_exec, read_file],
        ...     mode="search",
        ... )
        >>> agent = dspy.ReActV2("question -> answer", tools=catalog)
    """

    def __init__(
        self,
        tools: Any = (),
        *,
        mode: Literal["search", "code"] | None = None,
        name_prefix: str = "",
    ) -> None:
        if mode is not None and mode not in _TOOLBOX_MODES:
            raise ValueError(
                f"Unknown mode {mode!r}: expected 'search', 'code', or None"
            )
        Tool = load_tool(
            "dspy_agent_tools.Toolbox requires DSPy. "
            "Install it with: uv add 'dspy-agent-tools[mcp]'"
        )
        entries, sources = _collect_entries(tools, Tool, name_prefix)
        if mode is None:
            loaded = [_renamed(entry.tool, entry.name) for entry in entries]
        else:
            loaded = build_facade(entries, mode=mode, name_prefix=name_prefix)
        super().__init__(loaded)
        self.mode = mode
        self.name_prefix = name_prefix
        self.entries = tuple(entries)
        self._sources = sources

    def close(self) -> None:
        """Close every source that owns resources. Safe to call repeatedly."""
        for source in self._sources:
            close = getattr(source, "close", None)
            if callable(close):
                close()
        self._sources.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def __add__(self, other: Any) -> Toolbox:
        return Toolbox([self, other], mode=self.mode)

    def __radd__(self, other: Any) -> Toolbox:
        return Toolbox([other, self], mode=self.mode)

    def __iadd__(self, other: Any) -> Self:
        return self + other
