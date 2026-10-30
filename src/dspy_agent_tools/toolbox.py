"""Search- and code-mode discovery facades over any tool catalog.

``Toolbox`` collects MCP tools, regular ``dspy.Tool`` objects, and plain sync
or async Python functions into one catalog, then exposes a three-tool discovery
interface instead of every definition:

- ``search_tools`` ranks the catalog locally with BM25.
- ``get_tool_details`` returns one tool's description and input schema.
- ``call_tool`` runs one tool by name.

``mode="search"`` returns text results for ``dspy.ReAct`` / ``dspy.ReActV2``;
``mode="code"`` returns structured results for code written by ``dspy.RLM``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal, Self, cast

if TYPE_CHECKING:
    from dspy import Tool

_TOOLBOX_MODES = ("search", "code")
_SEARCH_DESCRIPTION_LIMIT = 300

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


def _load_tool() -> type[Tool]:
    """Import ``dspy.Tool`` on first use."""
    try:
        from dspy import Tool
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "dspy_agent_tools.Toolbox requires DSPy. "
            "Install it with: uv add 'dspy-agent-tools[mcp]'"
        ) from exc
    return Tool


_SYNC_TOOL_CLASSES: dict[bool, Any] = {}


def _sync_tool_class(*, drop_nulls: bool = False) -> type[Tool]:
    """Build once a ``Tool`` whose sync path converts async results.

    ``dspy.Tool.__call__`` refuses to run an async function unless
    ``allow_tool_async_sync_conversion`` is set. Enabling it inside the tool
    call keeps ``agent(...)`` working without mutating global DSPy settings,
    and leaves ``acall`` fully asynchronous.

    ``drop_nulls`` treats ``None`` as "not supplied", which MCP input schemas
    require; local Python tools keep explicit ``None`` values.
    """
    import dspy
    from dspy import Tool as DspyTool

    cached = _SYNC_TOOL_CLASSES.get(drop_nulls)
    if cached is not None:
        return cast(type[DspyTool], cached)

    class _SyncCapableTool(DspyTool):
        def __call__(self, **kwargs: Any) -> Any:
            with dspy.context(allow_tool_async_sync_conversion=True):
                return super().__call__(**self._prepare(kwargs))

        async def acall(self, **kwargs: Any) -> Any:
            return await super().acall(**self._prepare(kwargs))

        @staticmethod
        def _prepare(kwargs: dict[str, Any]) -> dict[str, Any]:
            if not drop_nulls:
                return kwargs
            return {name: value for name, value in kwargs.items() if value is not None}

    _SYNC_TOOL_CLASSES[drop_nulls] = _SyncCapableTool
    return _SyncCapableTool


_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _matches(token: str, term: str) -> bool:
    """Match a query term at token boundaries, with cheap prefix stemming."""
    if token == term:
        return True
    short, long = (token, term) if len(token) < len(term) else (term, token)
    return len(short) >= 3 and long.startswith(short)


class CatalogEntry:
    """One catalogued tool whose definition stays out of the model context."""

    __slots__ = (
        "body_tokens",
        "description",
        "name",
        "name_tokens",
        "required",
        "schema",
        "tool",
    )

    def __init__(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        tool: Tool,
    ) -> None:
        self.name = name
        self.description = description
        self.schema = schema
        self.required = [key for key in schema.get("required") or ()]
        self.tool = tool
        properties = schema.get("properties") or {}
        self.name_tokens = _tokenize(name.replace("_", " ").replace("-", " "))
        self.body_tokens = _tokenize(description) + _tokenize(" ".join(properties))


def _bm25(documents: Sequence[Sequence[str]], terms: set[str]) -> list[float]:
    """Score every document with BM25 over ``terms``."""
    lengths = [len(document) for document in documents]
    average = sum(lengths) / len(lengths) if lengths else 0.0
    scores = [0.0] * len(documents)
    for term in terms:
        frequencies = [
            sum(1 for token in document if _matches(token, term))
            for document in documents
        ]
        document_frequency = sum(1 for frequency in frequencies if frequency)
        if not document_frequency:
            continue
        idf = math.log(
            1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5)
        )
        for index, frequency in enumerate(frequencies):
            if not frequency:
                continue
            norm = 1 - 0.75 + 0.75 * (lengths[index] / average if average else 1.0)
            scores[index] += idf * frequency * 2.5 / (frequency + 1.5 * norm)
    return scores


def _search(
    entries: Sequence[CatalogEntry], query: str, limit: int
) -> list[CatalogEntry]:
    """Rank entries against ``query`` by name first, description second.

    Tool descriptions repeat their domain's vocabulary (almost every GitHub
    tool says "repository"), so scoring names and descriptions as one corpus
    lets generic description hits drown out the tool name. Names are therefore
    ranked on their own, with description/parameter hits at a lower weight.
    """
    limit = max(1, min(int(limit), 50))
    terms = set(_tokenize(query))
    if not terms:
        return list(entries[:limit])

    name_scores = _bm25([entry.name_tokens for entry in entries], terms)
    body_scores = _bm25([entry.body_tokens for entry in entries], terms)
    scored = [
        (name + 0.35 * body, entry)
        for name, body, entry in zip(name_scores, body_scores, entries)
    ]
    ranked = sorted(
        ((score, entry) for score, entry in scored if score),
        key=lambda pair: (-pair[0], pair[1].name),
    )
    return [entry for _, entry in ranked[:limit]]


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _SEARCH_DESCRIPTION_LIMIT:
        return text
    return text[: _SEARCH_DESCRIPTION_LIMIT - 3].rstrip() + "..."


def _search_text(entries: Sequence[CatalogEntry]) -> str:
    if not entries:
        return "[no matching tools]"
    lines = []
    for index, entry in enumerate(entries, 1):
        params = f"({', '.join(entry.required)})" if entry.required else ""
        lines.append(f"{index}. {entry.name}{params}: {_shorten(entry.description)}")
    return "\n".join(lines)


def _details_text(entry: CatalogEntry) -> str:
    description = entry.description.strip()
    header = f"{entry.name}: {description}" if description else entry.name
    return f"{header}\n\nInput schema:\n{json.dumps(entry.schema, indent=2)}"


def _search_records(entries: Sequence[CatalogEntry]) -> list[dict[str, Any]]:
    return [
        {
            "name": entry.name,
            "description": entry.description,
            "required": entry.required,
        }
        for entry in entries
    ]


def _details_record(entry: CatalogEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "description": entry.description,
        "required": entry.required,
        "input_schema": entry.schema,
    }


def _as_sync_capable(tool: Tool) -> Tool:
    """Rebuild a facade tool so its synchronous path also works."""
    cls = _sync_tool_class()
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
    Tool = _load_tool()
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


def _tool_entry(tool: Tool, name_prefix: str) -> CatalogEntry:
    """Catalog one ``dspy.Tool`` using the schema DSPy derived for it."""
    properties = tool.args if isinstance(tool.args, dict) else {}
    schema = {
        "type": "object",
        "properties": dict(properties),
        "required": [
            name
            for name, spec in properties.items()
            if not (isinstance(spec, dict) and "default" in spec)
        ],
    }
    return CatalogEntry(
        name=f"{name_prefix}{tool.name}",
        description=cast(str, tool.desc or ""),
        schema=schema,
        tool=tool,
    )


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
    """Search- or code-mode discovery facade over any mix of tools.

    ``Toolbox`` applies the same progressive-discovery methodology to an
    arbitrary list: MCP servers (pass the result of ``MCP(...)``), regular
    ``dspy.Tool`` objects, and plain sync or async Python functions. The
    returned list holds the three discovery tools instead of every definition,
    so the model sees only a few schemas.

    ``mode="search"`` returns text results for ``dspy.ReAct`` / ``dspy.ReActV2``.
    ``mode="code"`` returns structured ``list[dict]`` / ``dict`` results so
    ``dspy.RLM`` code can compose calls without parsing text.

    Nested lists and generators are flattened. Tool names must be unique
    (case-insensitively); use ``name_prefix`` or rename the function. Sources
    that own resources, such as ``MCPTools`` and nested ``Toolbox`` instances,
    are closed by :meth:`close`.

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
        mode: Literal["search", "code"] = "search",
        name_prefix: str = "",
    ) -> None:
        if mode not in _TOOLBOX_MODES:
            raise ValueError(f"Unknown mode {mode!r}: expected 'search' or 'code'")
        Tool = _load_tool()
        entries, sources = _collect_entries(tools, Tool, name_prefix)
        super().__init__(build_facade(entries, mode=mode, name_prefix=name_prefix))
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


__all__ = ["Toolbox"]
