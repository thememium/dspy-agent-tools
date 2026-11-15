"""Tests for connection and session lifecycle in dspy_agent_tools.mcp."""

import asyncio
import concurrent.futures
import gc
import sys
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import pytest

from dspy_agent_tools import MCP
from dspy_agent_tools.mcp.adapter import _accepted_kwargs
from dspy_agent_tools.mcp.connection import (
    _close_all,
    _Connection,
    _read_write,
    _serve,
    _ServeState,
    _SessionProxy,
)
from dspy_agent_tools.mcp.retry import _HTTPFailures

from .helpers import start, tools_module


class TestLifecycle:
    def test_timed_out_run_cancels_remote_work(self, tools):
        cancelled = threading.Event()

        async def delayed():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with pytest.raises(TimeoutError):
            tools._connections[0].run(delayed(), timeout=0.05)
        assert cancelled.wait(timeout=1)

    def test_paginated_server_exposes_callable_tools_from_every_page(self):
        server = Path(__file__).parents[1] / "fixtures" / "paginated_server.py"
        with MCP(command=sys.executable, args=[str(server)]) as tools:
            assert [tool.name for tool in tools] == ["first", "second"]
            assert tools[1]() == "second"

    def test_startup_timeout_also_bounds_tool_listing(self):
        server = Path(__file__).parents[1] / "fixtures" / "paginated_server.py"
        with pytest.raises(TimeoutError):
            MCP(
                command=sys.executable,
                args=[str(server), "--slow-list"],
                startup_timeout=1,
            )

    def test_server_stops_when_tools_are_collected(self):
        tools = start()
        connection = tools._connections[0]
        thread = connection._thread
        ref = weakref.ref(connection)

        del connection
        del tools
        gc.collect()

        assert ref() is None
        assert not thread.is_alive()

    def test_tool_keeps_server_alive_without_its_list(self):
        tools = start()
        tool = {t.name: t for t in tools}["add"]

        del tools
        gc.collect()

        assert asyncio.run(tool.acall(a=1, b=2)) == "3"

    def test_agent_keeps_server_alive_until_collected(self):
        class QA(dspy.Signature):
            question: str = dspy.InputField()
            answer: str = dspy.OutputField()

        tools = start()
        agent = dspy.ReAct(QA, tools=tools)
        ref = weakref.ref(tools._connections[0])

        del tools
        gc.collect()
        assert ref() is not None

        del agent
        gc.collect()
        assert ref() is None

    def test_close_is_idempotent_and_invalidates_tools(self, tools):
        tool = tools[0]
        tools.close()
        tools.close()

        with pytest.raises(RuntimeError, match="MCP server is closed"):
            asyncio.run(tool.acall(a=1, b=1))

    def test_context_manager_closes(self):
        with start() as tools:
            assert len(tools) == 2
        tools.close()  # already closed; must stay idempotent

    def test_startup_timeout(self):
        slow = Path(__file__).parents[1] / "fixtures" / "slow_server.py"
        with pytest.raises(TimeoutError, match="did not initialize"):
            MCP(command=sys.executable, args=[str(slow)], startup_timeout=0.5)

    def test_startup_failure_propagates(self):
        broken = Path(__file__).parents[1] / "fixtures" / "broken_server.py"
        with pytest.raises(ExceptionGroup):
            MCP(command=sys.executable, args=[str(broken)])

    def test_async_context_manager_closes(self):
        tools = start()

        async def run():
            async with tools as entered:
                assert entered is tools
            with pytest.raises(RuntimeError, match="MCP server is closed"):
                await tools[0].acall(a=1, b=1)

        asyncio.run(run())

    def test_close_all_stops_live_connections(self):
        tools = start()
        _close_all()
        with pytest.raises(RuntimeError, match="MCP server is closed"):
            asyncio.run(tools[0].acall(a=1, b=1))


class _FakeFuture:
    """Stand-in for a ``run_coroutine_threadsafe`` future."""

    def __init__(self, outcomes: list[BaseException | Any]) -> None:
        self.outcomes = outcomes
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True

    def result(self, timeout=None):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _bare_connection(serve_future: Any) -> Any:
    connection: Any = object.__new__(_Connection)
    connection._closed = False
    connection._state = SimpleNamespace(stop=None)
    connection._serve_future = serve_future
    connection._loop = asyncio.new_event_loop()
    connection._thread = threading.current_thread()
    connection._session = object()
    return connection


class TestInternals:
    def test_accepted_kwargs_keeps_kwargs_for_var_keyword(self):
        def accepts_all(**kwargs):
            return kwargs

        assert _accepted_kwargs(accepts_all, a=1, b=2) == {"a": 1, "b": 2}

    def test_accepted_kwargs_filters_unknown_kwargs(self):
        def narrow(url, timeout=1):
            return url

        assert _accepted_kwargs(narrow, url="u", timeout=2, extra=3) == {
            "url": "u",
            "timeout": 2,
        }

    def test_read_write_accepts_stream_objects(self):
        streams = SimpleNamespace(read="read", write="write")
        assert _read_write(streams) == ("read", "write")

    def test_proxy_cancellation_cancels_remote_call(self):
        class HangingSession:
            async def call_tool(self, name, arguments=None):
                await asyncio.Event().wait()

        class FakeConnection:
            closed = False
            session = HangingSession()
            loop = None

        async def run():
            connection = FakeConnection()
            connection.loop = asyncio.get_running_loop()
            proxy = _SessionProxy(cast(Any, connection))
            task = asyncio.create_task(proxy.call_tool("tool", {}))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())

    def test_close_cancels_after_shutdown_timeout(self):
        future = _FakeFuture([concurrent.futures.TimeoutError(), RuntimeError("late")])
        connection = _bare_connection(future)
        connection.close()

        assert future.cancelled
        connection._loop.close()

    def test_close_swallows_shutdown_errors(self):
        future = _FakeFuture([RuntimeError("boom")])
        connection = _bare_connection(future)
        connection.close()

        connection._loop.close()

    def test_del_swallows_close_errors(self):
        connection: Any = object.__new__(_Connection)
        connection._closed = False
        connection._state = object()  # no ``stop`` attribute

        _Connection.__del__(connection)

    def test_build_tools_requires_a_session(self, monkeypatch):
        closed = []

        class NoSessionConnection:
            def __init__(self, *args, **kwargs):
                self.session = None

            def close(self):
                closed.append(True)

        monkeypatch.setattr(tools_module, "_Connection", NoSessionConnection)

        with pytest.raises(RuntimeError, match="failed to initialize"):
            MCP(command="irrelevant")

        assert closed == [True]


class TestServeFailure:
    def test_adds_http_failure_note_and_fails_ready(self):
        failures = _HTTPFailures()
        failures.status = 401
        failures.url = "http://example/mcp"
        ready = concurrent.futures.Future()

        def transport():
            raise RuntimeError("boom")

        state = _ServeState(transport, cast(Any, object), ready, failures)

        with pytest.raises(RuntimeError, match="boom") as info:
            asyncio.run(_serve(state))

        assert any("HTTP 401" in note for note in info.value.__notes__)
        assert isinstance(ready.exception(), RuntimeError)
        assert state.session is None
