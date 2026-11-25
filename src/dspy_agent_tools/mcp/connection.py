"""One MCP session living on its own event-loop thread."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import threading
import weakref
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

from .retry import _HTTPFailures

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


def _read_write(streams: Any) -> tuple[Any, Any]:
    """Normalize transport output to its read and write streams.

    Transports yield ``(read, write)`` tuples; older versions of the
    Streamable HTTP client added a session-id getter as a third item.
    """
    if isinstance(streams, tuple):
        return streams[0], streams[1]
    return streams.read, streams.write


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
        failures: _HTTPFailures | None = None,
    ) -> None:
        self.transport = transport
        self.client_session = client_session
        self.ready = ready
        self.failures = failures
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
        note = state.failures.describe() if state.failures is not None else None
        if note:
            exc.add_note(note)
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
        failures: _HTTPFailures | None = None,
    ) -> None:
        self._label = label
        self._transport = transport
        self._client_session = client_session
        self._startup_timeout = startup_timeout
        self._failures = failures
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
            failures,
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
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    def recover(self) -> bool:
        """Reconnect the session after this object was closed."""
        if not self._closed and self._session is not None:
            return True
        if not self._closed:
            raise RuntimeError(f"MCP connection {self._label!r} lost its session")
        self._closed = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=_run_loop,
            args=(self._loop,),
            name=f"mcp:{self._label}",
            daemon=True,
        )
        self._state = _ServeState(
            self._transport,
            self._client_session,
            concurrent.futures.Future(),
            self._failures,
        )
        self._serve_future = None
        self._session = None
        self._thread.start()
        self._start(startup_timeout=self._startup_timeout)
        _CONNECTIONS.add(self)
        return True

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


_CONNECTIONS: weakref.WeakSet[_Connection] = weakref.WeakSet()


def _close_all() -> None:
    for connection in list(_CONNECTIONS):
        connection.close()


atexit.register(_close_all)
