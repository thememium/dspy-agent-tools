"""Run stdio MCP servers and expose their tools as ``dspy.Tool`` lists.

An MCP session is asynchronous and must stay open for as long as its tools
are used. :func:`mcp` hides that lifecycle: it starts the server on a
background event loop, converts every advertised tool with
``dspy.Tool.from_mcp_tool``, and returns the tools as an :class:`MCPTools`
list that owns the live session.

The returned tools work from both DSPy paths without extra configuration:
``agent(question=...)`` uses DSPy's async-to-sync conversion for the MCP call
only (the global setting is untouched), and ``await agent.acall(...)`` runs
fully asynchronously.

Sessions clean themselves up: the server stays alive while the returned tools
or list are referenced, then stops when they are garbage-collected, with an
interpreter-exit fallback. ``MCPTools.close()`` is only needed to release the
server earlier.

Requires the optional ``mcp`` extra::

    uv add "dspy-agent-tools[mcp]"
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import threading
import weakref
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, TypeVar

if TYPE_CHECKING:
    from dspy import Tool
    from mcp import ClientSession, StdioServerParameters

T = TypeVar("T")

DEFAULT_STARTUP_TIMEOUT = 60.0  # seconds
_CLOSE_TIMEOUT = 10.0  # seconds

_IMPORT_HINT = (
    "dspy_agent_tools.mcp requires DSPy and the MCP SDK. "
    "Install them with: uv add 'dspy-agent-tools[mcp]'"
)


def _load() -> tuple[type[Tool], type[ClientSession], type[StdioServerParameters], Any]:
    """Import the optional dependencies on first use."""
    try:
        from dspy import Tool
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(_IMPORT_HINT) from exc
    return Tool, ClientSession, StdioServerParameters, stdio_client


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
        server: StdioServerParameters,
        stdio_client: Any,
        client_session: type[ClientSession],
        ready: concurrent.futures.Future[Any],
    ) -> None:
        self.server = server
        self.stdio_client = stdio_client
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
            state.stdio_client(state.server) as (read, write),
            state.client_session(read, write) as session,
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
    """One MCP server process living on its own event-loop thread.

    Tool calls are independent tasks on that loop. The connection is closed
    automatically when it is garbage-collected: as long as a tool or an
    :class:`MCPTools` list references it the server stays up, and once the last
    reference goes away the session exits and the server process stops.
    """

    def __init__(
        self,
        server: StdioServerParameters,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        _, client_session, _, stdio_client = _load()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=_run_loop,
            args=(self._loop,),
            name=f"mcp:{server.command}",
            daemon=True,
        )
        self._state = _ServeState(
            server,
            stdio_client,
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
                f"MCP server {self._state.server.command!r} did not initialize "
                f"within {startup_timeout}s"
            ) from exc
        except BaseException:
            self.close()
            raise

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run a coroutine on the server's loop and wait for its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        """Stop the session, the server process, and the loop thread."""
        if getattr(self, "_closed", True):
            return
        self._closed = True

        if self._state.stop is not None:
            self._loop.call_soon_threadsafe(self._state.stop.set)
        if self._serve_future is not None:
            if not self._serve_future.done():
                self._serve_future.cancel()
            try:
                self._serve_future.result(timeout=_CLOSE_TIMEOUT)
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


def mcp(
    command: str,
    *args: str,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    name_prefix: str = "",
    result_mode: Literal["text", "structured"] = "text",
) -> MCPTools:
    """Start a stdio MCP server and return its tools as DSPy tools.

    The server process is launched immediately (``command`` + ``args``) and
    kept alive on a background thread. Every tool the server advertises is
    converted with ``dspy.Tool.from_mcp_tool``, so the returned list can go
    straight into ``dspy.ReAct``, ``dspy.RLM``, or any other DSPy module.

    MCP tools are asynchronous under the hood; the returned tools enable
    DSPy's async-to-sync conversion for their sync path, so both
    ``agent(question=...)`` and ``await agent.acall(question=...)`` work
    without extra configuration.

    Args:
        command: Executable to launch (e.g. ``"npx"`` or ``"python"``).
        *args: Arguments passed to the server command.
        env: Environment for the server process; ``None`` inherits the
            current environment.
        cwd: Working directory for the server process.
        startup_timeout: Seconds to wait for initialization and tool listing.
        name_prefix: Prepended to every tool name, useful when combining
            servers that expose identically named tools.
        result_mode: ``"text"`` (default) flattens MCP text content to a
            string/list; ``"structured"`` returns ``structuredContent``
            when the server provides it.

    Returns:
        An :class:`MCPTools` list of ``dspy.Tool`` owning the live session.
        The server stops automatically when the tools (or the list) are no
        longer referenced, and at interpreter exit; call ``close()`` only to
        stop it earlier.

    Example:
        >>> import dspy
        >>> from dspy_agent_tools import bash_exec, mcp
        >>> files = mcp(
        ...     "npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"
        ... )
        >>> agent = dspy.ReAct(
        ...     "question -> answer",
        ...     tools=files + [bash_exec],
        ... )
        >>> result = agent(question="List the files in /tmp")
    """
    Tool, _, StdioServerParameters, _ = _load()

    connection = _Connection(
        StdioServerParameters(
            command=command,
            args=list(args),
            env=env,
            cwd=None if cwd is None else str(cwd),
        ),
        startup_timeout=startup_timeout,
    )
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


__all__ = ["MCPTools", "mcp"]
