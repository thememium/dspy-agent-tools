"""MCP server transports and the public ``MCP`` / ``MCPTools`` API."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Self, cast

from ..toolbox.catalog import CatalogEntry
from .adapter import (
    _accepted_kwargs,
    _as_sync_capable,
    _dspy_compatible,
    _mcp_signature,
    _read_only,
    _zero_arg_factory,
)
from .connection import (
    _CONNECTIONS,
    DEFAULT_STARTUP_TIMEOUT,
    _Connection,
    _load,
    _SessionProxy,
)
from .retry import _HTTPFailures, _retrying, _watch_http_failures

DEFAULT_TOOL_RETRY_TIMEOUT = 30.0  # seconds


class MCPTools(list):
    """List of ``dspy.Tool`` backed by a live MCP server process.

    Behaves like a normal list, so it can be passed directly as ``tools=``,
    concatenated with regular DSPy tools (``tools + [bash_exec]`` and
    ``[bash_exec] + tools`` both keep the MCP connections attached), or
    unpacked. ``entries`` keeps the exact ``CatalogEntry`` metadata (including
    the server's JSON input schema) for ``Toolbox``. The server is stopped
    automatically once neither this list nor any of its tools is referenced any
    more (and at interpreter exit); call :meth:`close` (or use it as a context
    manager) only to stop it early.
    """

    def __init__(
        self,
        tools: Any = (),
        connections: Any = (),
        entries: Any = (),
    ) -> None:
        super().__init__(tools)
        self._connections: list[_Connection] = list(connections)
        # Exact MCP schemas for Toolbox; empty after concatenation with tools
        # that this list did not catalogue.
        self.entries: tuple[CatalogEntry, ...] = tuple(entries)

    def close(self) -> None:
        """Stop every server owned by this list. Safe to call repeatedly."""
        for connection in self._connections:
            connection.close()
        self._connections.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def __add__(self, other: Any) -> MCPTools:
        connections = [*self._connections, *getattr(other, "_connections", ())]
        return MCPTools(list(self) + list(other), dict.fromkeys(connections))

    def __radd__(self, other: Any) -> MCPTools:
        connections = [*getattr(other, "_connections", ()), *self._connections]
        return MCPTools(list(other) + list(self), dict.fromkeys(connections))


def _stdio_transport(
    command: str,
    *,
    args: Sequence[str],
    env: dict[str, str] | None,
    cwd: str | Path | None,
) -> Callable[[], Any]:
    """Build the stdio transport context-manager factory."""

    @asynccontextmanager
    async def transport():
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        server = StdioServerParameters(
            command=command,
            args=list(args),
            env=env,
            cwd=None if cwd is None else str(cwd),
        )
        async with stdio_client(server) as streams:
            yield streams

    return transport


def _build_tools(
    transport: Callable[[], Any],
    *,
    label: str,
    startup_timeout: float,
    name_prefix: str,
    result_mode: Literal["text", "structured"],
    retry_timeout: float = DEFAULT_TOOL_RETRY_TIMEOUT,
    failures: _HTTPFailures | None = None,
) -> MCPTools:
    """Connect a transport, list its tools, and build the owning list."""
    Tool, ClientSession = _load()
    from mcp.types import PaginatedRequestParams

    deadline = time.monotonic() + startup_timeout
    connection = _Connection(transport, ClientSession, label, startup_timeout, failures)
    try:
        session = connection.session
        if session is None:
            raise RuntimeError("MCP server failed to initialize")
        advertised_tools = []
        cursor = None
        seen_cursors: set[str] = set()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP tool listing exceeded {startup_timeout}s")
            request = (
                session.list_tools()
                if cursor is None
                else session.list_tools(params=PaginatedRequestParams(cursor=cursor))
            )
            listed = connection.run(request, timeout=remaining)
            advertised_tools.extend(listed.tools)
            cursor = getattr(listed, "next_cursor", None)
            if cursor is None:
                cursor = getattr(listed, "nextCursor", None)
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise RuntimeError(
                    f"MCP tool listing repeated pagination cursor {cursor!r}"
                )
            seen_cursors.add(cursor)
        proxy = _SessionProxy(connection)
        entries: list[CatalogEntry] = []
        for advertised in advertised_tools:
            tool = Tool.from_mcp_tool(
                proxy, _dspy_compatible(advertised), result_mode=result_mode
            )
            signature, renames = _mcp_signature(
                advertised, tool.args or {}, tool.arg_types or {}
            )
            func = tool.func
            if _read_only(advertised):
                func = _retrying(func, timeout=retry_timeout)
            tool = _as_sync_capable(tool, signature, func=func, renames=renames)
            if name_prefix:
                tool.name = f"{name_prefix}{tool.name}"
            schema = getattr(advertised, "input_schema", None) or getattr(
                advertised, "inputSchema", None
            )
            entries.append(
                CatalogEntry(
                    name=cast(str, tool.name),
                    description=getattr(advertised, "description", None) or "",
                    schema=schema if isinstance(schema, dict) else {},
                    tool=tool,
                )
            )
        tools = [entry.tool for entry in entries]
    except BaseException:
        connection.close()
        raise

    _CONNECTIONS.add(connection)
    return MCPTools(tools, [connection], entries)


def _http_transport(
    url: str,
    *,
    headers: dict[str, str] | None,
    timeout: float,
    sse_read_timeout: float,
    auth: Any,
    http_client_factory: Any,
    terminate_on_close: bool,
    failures: _HTTPFailures | None = None,
) -> Callable[[], Any]:
    """Build the Streamable HTTP transport context-manager factory."""

    @asynccontextmanager
    async def transport():
        module = importlib.import_module("mcp.client.streamable_http")
        streamable = getattr(module, "streamable_http_client", None)
        factory = getattr(module, "create_mcp_http_client", None)
        if streamable is None or factory is None:
            raise ImportError(
                "The installed MCP SDK does not provide a Streamable HTTP "
                "client; dspy-agent-tools requires mcp>=2"
            )

        if http_client_factory is not None:
            client = http_client_factory()
        else:
            import httpx2

            client = factory(
                headers=headers,
                timeout=httpx2.Timeout(timeout, read=sse_read_timeout),
                auth=auth,
            )
        if failures is not None:
            _watch_http_failures(client, failures)

        async with (
            client,
            streamable(
                url, http_client=client, terminate_on_close=terminate_on_close
            ) as streams,
        ):
            yield streams

    return transport


def _sse_transport(
    url: str,
    *,
    headers: dict[str, str] | None,
    timeout: float,
    sse_read_timeout: float,
    auth: Any,
    httpx_client_factory: Any,
) -> Callable[[], Any]:
    """Build the legacy HTTP+SSE transport context-manager factory."""

    @asynccontextmanager
    async def transport():
        from mcp.client.sse import sse_client

        options = _accepted_kwargs(
            sse_client,
            url=url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
            auth=auth,
        )
        if httpx_client_factory is not None:
            options["httpx_client_factory"] = _zero_arg_factory(httpx_client_factory)

        async with sse_client(**options) as streams:
            yield streams

    return transport


def _transport_kind(transport: str, *, command: str | None, url: str | None) -> str:
    """Validate the request and resolve ``"auto"`` to a concrete transport."""
    if transport == "auto":
        if command is not None and url is not None:
            raise ValueError("Pass either command=... or url=..., not both")
        if command is not None:
            return "stdio"
        if url is not None:
            return "http"
        raise ValueError("MCP() requires command=... or url=...")

    if transport not in ("stdio", "http", "sse"):
        raise ValueError(
            f"Unknown transport {transport!r}: expected 'auto', 'stdio', "
            "'http', 'sse', or a transport factory"
        )
    if transport == "stdio":
        if url is not None:
            raise ValueError("transport='stdio' does not accept url=...")
        if command is None:
            raise ValueError("transport='stdio' requires command=...")
    elif command is not None:
        raise ValueError(f"transport={transport!r} does not accept command=...")
    elif url is None:
        raise ValueError(f"transport={transport!r} requires url=...")
    return transport


def MCP(
    *,
    command: str | None = None,
    args: Sequence[str] = (),
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    url: str | None = None,
    transport: str | Callable[[], Any] = "auto",
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    sse_read_timeout: float = 300.0,
    auth: Any = None,
    http_client_factory: Any = None,
    terminate_on_close: bool = True,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    name_prefix: str = "",
    result_mode: Literal["text", "structured"] = "text",
    retry_timeout: float = DEFAULT_TOOL_RETRY_TIMEOUT,
) -> MCPTools:
    """Connect to an MCP server and return its tools as DSPy tools.

    One entry point for every transport. With the default ``transport="auto"``
    the transport is inferred from the arguments:

    - ``command=...`` starts a local stdio server process; ``args``, ``env``,
      and ``cwd`` configure it.
    - ``url=...`` connects to a remote server over Streamable HTTP (the MCP
      default).

    Pass ``transport="sse"`` with ``url=...`` for the legacy HTTP+SSE
    transport, or pass a zero-argument callable returning an async context
    manager that yields a ``(read, write)`` pair to use any custom transport.
    The explicit string forms are ``"stdio"``, ``"http"``, and ``"sse"``.

    The server starts immediately and stays alive on a background thread while
    the returned tools or list are referenced. Every advertised tool is
    converted with ``dspy.Tool.from_mcp_tool``, so the result can go straight
    into ``dspy.ReAct``, ``dspy.RLM``, or any other DSPy module. MCP tools are
    asynchronous under the hood; the returned tools enable DSPy's
    async-to-sync conversion for their sync path, so both
    ``agent(question=...)`` and ``await agent.acall(question=...)`` work
    without extra configuration.

    Every advertised tool becomes its own ``dspy.Tool``. For progressive
    discovery, wrap the result in ``Toolbox``::

        catalog = Toolbox(MCP(url="https://example.com/mcp"), mode="search")

    Args:
        command: Executable to launch for a stdio server (e.g. ``"npx"``).
        args: Arguments passed to the stdio server command.
        env: Environment for the stdio server; ``None`` inherits the current
            environment.
        cwd: Working directory for the stdio server.
        url: Remote MCP endpoint, e.g. ``"https://example.com/mcp"``.
        transport: ``"auto"`` (default), ``"stdio"``, ``"http"``, ``"sse"``, or
            a custom async transport factory.
        headers: Extra HTTP headers, e.g. an ``Authorization`` bearer token.
        timeout: HTTP timeout in seconds; defaults to 30 for Streamable HTTP
            and 5 for legacy SSE.
        sse_read_timeout: How long to wait for the server stream, in seconds.
        auth: Optional ``httpx``/``httpx2`` auth handler (for example an MCP
            OAuth client provider).
        http_client_factory: Optional zero-argument callable returning an async
            HTTP client. It is created and closed on the connection's event
            loop, so it is safe for clients with loop affinity. For Streamable
            HTTP, supplying it makes ``headers``, ``timeout``, and ``auth``
            ignored; for legacy SSE it is passed to the SDK as its
            ``httpx_client_factory`` alongside those options.
        terminate_on_close: Ask the server to terminate the HTTP session on
            close.
        startup_timeout: Seconds to wait for initialization and tool listing.
        name_prefix: Prepended to every tool name, useful when combining
            servers that expose identically named tools.
        result_mode: ``"text"`` (default) flattens MCP text content to a
            string/list; ``"structured"`` returns ``structuredContent`` when
            the server provides it.
        retry_timeout: Seconds to replay a tool annotated ``readOnlyHint``
            while it answers with an error payload (``{"error": ...}`` or
            ``{"ok": false}``), the usual way a server reports that startup
            work such as an index reconcile is still running. The last payload
            is returned unchanged once the budget runs out; ``0`` disables the
            replay. Tools without the annotation are never replayed.

    Returns:
        An :class:`MCPTools` list of ``dspy.Tool`` owning the live session.
        The server stops automatically when the tools (or the list) are no
        longer referenced, and at interpreter exit; call ``close()`` only to
        stop it earlier.

    Example:
        >>> import dspy
        >>> from dspy_agent_tools import MCP, bash_exec
        >>> files = MCP(
        ...     command="npx",
        ...     args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        ... )
        >>> hosted = MCP(url="https://example.com/mcp")
        >>> legacy = MCP(url="https://example.com/sse", transport="sse")
        >>> agent = dspy.ReAct(
        ...     "question -> answer",
        ...     tools=files + [bash_exec],
        ... )
        >>> result = agent(question="List the files in /tmp")
    """
    factory: Callable[[], Any]
    failures = _HTTPFailures()
    if callable(transport):
        if command is not None or url is not None:
            raise ValueError("Pass either a transport factory or command/url, not both")
        factory = transport
        label = getattr(transport, "__name__", "custom transport")
    else:
        kind = _transport_kind(transport, command=command, url=url)
        if kind == "stdio":
            factory = _stdio_transport(cast(str, command), args=args, env=env, cwd=cwd)
            label = cast(str, command)
        elif kind == "http":
            factory = _http_transport(
                cast(str, url),
                headers=headers,
                timeout=30.0 if timeout is None else timeout,
                sse_read_timeout=sse_read_timeout,
                auth=auth,
                http_client_factory=http_client_factory,
                terminate_on_close=terminate_on_close,
                failures=failures,
            )
            label = cast(str, url)
        else:
            factory = _sse_transport(
                cast(str, url),
                headers=headers,
                timeout=5.0 if timeout is None else timeout,
                sse_read_timeout=sse_read_timeout,
                auth=auth,
                httpx_client_factory=http_client_factory,
            )
            label = cast(str, url)

    return _build_tools(
        factory,
        label=label,
        startup_timeout=startup_timeout,
        name_prefix=name_prefix,
        result_mode=result_mode,
        retry_timeout=retry_timeout,
        failures=failures,
    )
