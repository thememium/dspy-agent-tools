"""Run MCP servers and expose their tools as ``dspy.Tool`` lists.

An MCP session is asynchronous and must stay open for as long as its tools
are used. The factories here hide that lifecycle: they connect on a background
event loop, convert every advertised tool with ``dspy.Tool.from_mcp_tool``,
and return the tools as an :class:`MCPTools` list that owns the live session.

Transports are chosen by :func:`MCP`:

- ``command=`` launches a local stdio server process (``npx``, a script, ...).
- ``url=`` connects to a remote Streamable HTTP endpoint (the MCP default).
- ``transport="sse"`` selects the legacy HTTP+SSE transport for an ``url``.
- ``transport=factory`` uses any custom async transport yielding a read/write
  pair (WebSocket, in-memory, ...).

The returned tools work from both DSPy paths without extra configuration:
``agent(question=...)`` uses DSPy's async-to-sync conversion for the MCP call
only (the global setting is untouched), and ``await agent.acall(...)`` runs
fully asynchronously.

Sessions clean themselves up: the server stays alive while the returned tools
or list are referenced, then stops when they are garbage-collected, with an
interpreter-exit fallback. ``MCPTools.close()`` is only needed to release the
server earlier.

Requires the optional ``mcp`` extra (MCP SDK 2.x)::

    uv add "dspy-agent-tools[mcp]"
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import importlib
import inspect
import threading
import weakref
from collections.abc import Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, TypeVar, cast

if TYPE_CHECKING:
    from dspy import Tool
    from mcp import ClientSession

T = TypeVar("T")

DEFAULT_STARTUP_TIMEOUT = 60.0  # seconds
_CLOSE_TIMEOUT = 10.0  # seconds

_IMPORT_HINT = (
    "dspy_agent_tools.mcp requires DSPy and the MCP SDK. "
    "Install them with: uv add 'dspy-agent-tools[mcp]'"
)


def _load() -> tuple[type[Tool], type[ClientSession]]:
    """Import the optional dependencies on first use."""
    try:
        from dspy import Tool
        from mcp import ClientSession
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(_IMPORT_HINT) from exc
    return Tool, ClientSession


def _accepted_kwargs(func: Any, **kwargs: Any) -> dict[str, Any]:
    """Drop keyword arguments the installed MCP SDK version does not accept."""
    parameters = inspect.signature(func).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return kwargs
    return {name: value for name, value in kwargs.items() if name in parameters}


def _zero_arg_factory(factory: Any) -> Any:
    """Adapt a zero-argument client factory to the SDK's keyword convention."""
    return lambda **_kwargs: factory()


def _read_write(streams: Any) -> tuple[Any, Any]:
    """Normalize transport output to its read and write streams.

    Transports yield ``(read, write)`` tuples; older versions of the
    Streamable HTTP client added a session-id getter as a third item.
    """
    if isinstance(streams, tuple):
        return streams[0], streams[1]
    return streams.read, streams.write


_SYNC_TOOL_CLASS: Any = None


def _sync_tool_class() -> type[Tool]:
    """Build once a ``Tool`` whose sync path converts async results.

    ``dspy.Tool.__call__`` refuses to run an async function unless
    ``allow_tool_async_sync_conversion`` is set. Enabling it inside the tool
    call keeps ``agent(...)`` working without mutating global DSPy settings,
    and leaves ``acall`` fully asynchronous.
    """
    global _SYNC_TOOL_CLASS
    if _SYNC_TOOL_CLASS is None:
        import dspy
        from dspy import Tool

        class _SyncCapableTool(Tool):
            def __call__(self, **kwargs: Any) -> Any:
                with dspy.context(allow_tool_async_sync_conversion=True):
                    return super().__call__(**kwargs)

        _SYNC_TOOL_CLASS = _SyncCapableTool
    return _SYNC_TOOL_CLASS


def _as_sync_capable(tool: Tool) -> Tool:
    """Rebuild an MCP tool so its synchronous path also works."""
    cls = _sync_tool_class()
    return cls(
        func=tool.func,
        name=tool.name,
        desc=tool.desc,
        args=tool.args,
        arg_types=tool.arg_types,
        arg_desc=tool.arg_desc,
    )


class _ServeState:
    """State owned by the serve task; deliberately references no connection.

    Keeping this object free of a back-reference lets the background thread
    and event loop stay alive without keeping :class:`_Connection` alive, so
    the connection can be garbage-collected (and its server stopped) when the
    last tool or tool list is dropped.
    """

    def __init__(
        self,
        transport: Callable[[], Any],
        client_session: type[ClientSession],
        ready: concurrent.futures.Future[Any],
    ) -> None:
        self.transport = transport
        self.client_session = client_session
        self.ready = ready
        self.stop: asyncio.Event | None = None
        self.session: ClientSession | None = None


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Thread entry point: run the connection's loop forever."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def _serve(state: _ServeState) -> None:
    """Enter the MCP session and hold it open until ``state.stop`` is set.

    One task enters and exits both async context managers because anyio cancel
    scopes must be exited by the task that entered them.
    """
    try:
        async with (
            state.transport() as streams,
            state.client_session(*_read_write(streams)) as session,
        ):
            await session.initialize()
            state.session = session
            state.stop = asyncio.Event()
            state.ready.set_result(session)
            await state.stop.wait()
    except BaseException as exc:
        if not state.ready.done():
            state.ready.set_exception(exc)
        raise
    finally:
        state.session = None


class _Connection:
    """One MCP session living on its own event-loop thread.

    Tool calls are independent tasks on that loop. The connection is closed
    automatically when it is garbage-collected: as long as a tool or an
    :class:`MCPTools` list references it the session stays up, and once the
    last reference goes away the transport closes (stopping a stdio server
    process or an HTTP session).
    """

    def __init__(
        self,
        transport: Callable[[], Any],
        client_session: type[ClientSession],
        label: str,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        self._label = label
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=_run_loop,
            args=(self._loop,),
            name=f"mcp:{label}",
            daemon=True,
        )
        self._state = _ServeState(
            transport,
            client_session,
            concurrent.futures.Future(),
        )
        self._serve_future: concurrent.futures.Future[None] | None = None
        self._session: ClientSession | None = None
        self._closed = False
        self._thread.start()
        self._start(startup_timeout=startup_timeout)

    @property
    def session(self) -> ClientSession | None:
        return self._session

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    @property
    def closed(self) -> bool:
        return self._closed

    def _start(self, startup_timeout: float) -> None:
        self._serve_future = asyncio.run_coroutine_threadsafe(
            _serve(self._state), self._loop
        )
        try:
            self._session = self._state.ready.result(timeout=startup_timeout)
        except concurrent.futures.TimeoutError as exc:
            self.close()
            raise TimeoutError(
                f"MCP connection {self._label!r} did not initialize within "
                f"{startup_timeout}s"
            ) from exc
        except BaseException:
            self.close()
            raise

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run a coroutine on the server's loop and wait for its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        """Stop the session, the transport, and the loop thread."""
        if getattr(self, "_closed", True):
            return
        self._closed = True

        graceful = self._state.stop is not None
        if graceful:
            self._loop.call_soon_threadsafe(self._state.stop.set)
        elif self._serve_future is not None:
            self._serve_future.cancel()

        if self._serve_future is not None:
            try:
                self._serve_future.result(timeout=_CLOSE_TIMEOUT)
            except concurrent.futures.TimeoutError:
                self._serve_future.cancel()
                try:
                    self._serve_future.result(timeout=_CLOSE_TIMEOUT)
                except Exception:  # noqa: BLE001, S110 - shutdown is best-effort
                    pass
            except Exception:  # noqa: BLE001, S110 - shutdown is best-effort
                pass  # startup errors already surfaced

        self._loop.call_soon_threadsafe(self._loop.stop)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=_CLOSE_TIMEOUT)
        if not self._thread.is_alive():
            self._loop.close()

        self._session = None
        _CONNECTIONS.discard(self)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 - interpreter shutdown, best-effort
            pass


class _SessionProxy:
    """Async ``call_tool`` facade for a session owned by another loop/thread.

    ``dspy.Tool.from_mcp_tool`` only needs an object with an async
    ``call_tool``; this forwards each call to the server's loop so the tool
    works from any caller loop (including ``asyncio.run``).
    """

    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        session = None if self._connection.closed else self._connection.session
        if session is None:
            raise RuntimeError("MCP server is closed")

        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(name, arguments=arguments, **kwargs),
            self._connection.loop,
        )
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise


class MCPTools(list):
    """List of ``dspy.Tool`` backed by a live MCP server process.

    Behaves like a normal list, so it can be passed directly as ``tools=``,
    concatenated with regular DSPy tools (``tools + [bash_exec]`` and
    ``[bash_exec] + tools`` both keep the MCP connections attached), or
    unpacked. The server is stopped automatically once neither this list nor
    any of its tools is referenced any more (and at interpreter exit); call
    :meth:`close` (or use it as a context manager) only to stop it early.
    """

    def __init__(self, tools: Any = (), connections: Any = ()) -> None:
        super().__init__(tools)
        self._connections: list[_Connection] = list(connections)

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
        return MCPTools(list(self) + list(other), self._connections)

    def __radd__(self, other: Any) -> MCPTools:
        return MCPTools(list(other) + list(self), self._connections)


_CONNECTIONS: weakref.WeakSet[_Connection] = weakref.WeakSet()


def _close_all() -> None:
    for connection in list(_CONNECTIONS):
        connection.close()


atexit.register(_close_all)


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
) -> MCPTools:
    """Connect a transport, list its tools, and build the owning list."""
    Tool, ClientSession = _load()

    connection = _Connection(transport, ClientSession, label, startup_timeout)
    try:
        session = connection.session
        if session is None:
            raise RuntimeError("MCP server failed to initialize")
        listed = connection.run(session.list_tools())
        proxy = _SessionProxy(connection)
        tools = [
            _as_sync_capable(Tool.from_mcp_tool(proxy, tool, result_mode=result_mode))
            for tool in listed.tools
        ]
        if name_prefix:
            for tool in tools:
                tool.name = f"{name_prefix}{tool.name}"
    except BaseException:
        connection.close()
        raise

    _CONNECTIONS.add(connection)
    return MCPTools(tools, [connection])


def _http_transport(
    url: str,
    *,
    headers: dict[str, str] | None,
    timeout: float,
    sse_read_timeout: float,
    auth: Any,
    http_client_factory: Any,
    terminate_on_close: bool,
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
    )


__all__ = ["MCP", "MCPTools"]
