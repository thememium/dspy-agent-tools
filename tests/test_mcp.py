"""Tests for dspy_agent_tools.mcp."""

import asyncio
import concurrent.futures
import gc
import importlib
import sys
import threading
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from dspy_agent_tools import MCPTools, mcp
from dspy_agent_tools.mcp import (
    _accepted_kwargs,
    _close_all,
    _Connection,
    _http_transport,
    _read_write,
    _SessionProxy,
)

mcp_module = importlib.import_module("dspy_agent_tools.mcp")

SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"


def start(**kwargs):
    return mcp(sys.executable, str(SERVER), **kwargs)


@pytest.fixture()
def tools():
    server = start()
    try:
        yield server
    finally:
        server.close()


class TestFactory:
    def test_returns_mcp_tools(self, tools):
        assert isinstance(tools, MCPTools)
        assert isinstance(tools, list)
        assert {tool.name for tool in tools} == {"add", "echo"}
        assert all(isinstance(tool, dspy.Tool) for tool in tools)

    def test_exposes_descriptions_and_schemas(self, tools):
        by_name = {tool.name: tool for tool in tools}
        assert by_name["add"].desc == "Add two integers and return the sum."
        assert by_name["add"].args["a"]["type"] == "integer"
        assert by_name["echo"].args["text"]["type"] == "string"

    def test_name_prefix(self):
        with start(name_prefix="srv_") as tools:
            assert {tool.name for tool in tools} == {"srv_add", "srv_echo"}


class TestToolCalls:
    def test_calls_server_through_dspy_tool(self, tools):
        by_name = {tool.name: tool for tool in tools}

        async def call():
            added = await by_name["add"].acall(a=2, b=3)
            echoed = await by_name["echo"].acall(text="hello")
            return added, echoed

        assert asyncio.run(call()) == ("5", "hello")

    def test_sync_call_without_extra_configuration(self, tools):
        by_name = {tool.name: tool for tool in tools}
        assert by_name["add"](a=40, b=2) == "42"
        assert not dspy.settings.allow_tool_async_sync_conversion


class TestComposition:
    def test_adds_regular_tools_and_keeps_handle(self, tools):
        def shout(text: str) -> str:
            """Uppercase the text."""
            return text.upper()

        combined = tools + [shout]
        assert isinstance(combined, MCPTools)
        assert [tool.name for tool in tools] == ["add", "echo"]
        assert combined[-1] is shout

        reversed_combined = [shout] + tools
        assert isinstance(reversed_combined, MCPTools)
        assert reversed_combined[0] is shout
        assert [tool.name for tool in reversed_combined[1:]] == ["add", "echo"]

    def test_combines_multiple_servers(self):
        with start(name_prefix="a_") as first, start(name_prefix="b_") as second:
            combined = first + second
            assert isinstance(combined, MCPTools)
            assert [tool.name for tool in combined] == [
                "a_add",
                "a_echo",
                "b_add",
                "b_echo",
            ]

            async def call():
                by_name = {tool.name: tool for tool in combined}
                return await by_name["b_add"].acall(a=1, b=1)

            assert asyncio.run(call()) == "2"

    def test_react_accepts_mcp_tools(self, tools):
        def shout(text: str) -> str:
            """Uppercase the text."""
            return text.upper()

        class QA(dspy.Signature):
            question: str = dspy.InputField()
            answer: str = dspy.OutputField()

        agent = dspy.ReAct(
            QA,
            tools=tools + [shout],
            max_iters=2,
        )
        assert isinstance(agent, dspy.ReAct)

    def test_react_runs_mcp_tool_synchronously(self, tools):
        class QA(dspy.Signature):
            question: str = dspy.InputField()
            answer: str = dspy.OutputField()

        lm = DummyLM(
            [
                {
                    "next_thought": "add them",
                    "next_tool_name": "add",
                    "next_tool_args": {"a": 2, "b": 3},
                },
                {
                    "next_thought": "done",
                    "next_tool_name": "finish",
                    "next_tool_args": {},
                },
                {"reasoning": "observed the sum", "answer": "5"},
            ]
        )
        agent = dspy.ReAct(QA, tools=tools, max_iters=3)

        with dspy.context(lm=lm):
            prediction = agent(question="what is 2+3?")

        assert prediction.trajectory["observation_0"] == "5"
        assert prediction.answer == "5"


class TestLifecycle:
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
        slow = Path(__file__).parent / "fixtures" / "slow_server.py"
        with pytest.raises(TimeoutError, match="did not initialize"):
            mcp(sys.executable, str(slow), startup_timeout=0.5)

    def test_startup_failure_propagates(self):
        broken = Path(__file__).parent / "fixtures" / "broken_server.py"
        with pytest.raises(ExceptionGroup):
            mcp(sys.executable, str(broken))

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

        monkeypatch.setattr(mcp_module, "_Connection", NoSessionConnection)

        with pytest.raises(RuntimeError, match="failed to initialize"):
            mcp("irrelevant")

        assert closed == [True]


class TestHttpTransport:
    def _fake_import(self, monkeypatch, module):
        real_import = importlib.import_module

        def fake_import(name, package=None):
            if name == "mcp.client.streamable_http":
                return module
            return real_import(name, package)

        monkeypatch.setattr(
            mcp_module, "importlib", SimpleNamespace(import_module=fake_import)
        )

    def test_v1_client_fallback(self, monkeypatch):
        seen = {}

        @asynccontextmanager
        async def legacy_client(**kwargs):
            seen.update(kwargs)
            yield ("read", "write")

        self._fake_import(
            monkeypatch, SimpleNamespace(streamablehttp_client=legacy_client)
        )
        transport = _http_transport(
            "http://example/mcp",
            headers={"Authorization": "Bearer x"},
            timeout=1.0,
            sse_read_timeout=2.0,
            auth=None,
            http_client_factory=lambda: "client",
            terminate_on_close=False,
        )

        async def run():
            async with transport() as streams:
                assert streams == ("read", "write")

        asyncio.run(run())

        assert seen["url"] == "http://example/mcp"
        assert seen["headers"] == {"Authorization": "Bearer x"}
        assert seen["timeout"] == 1.0
        assert seen["sse_read_timeout"] == 2.0
        assert seen["terminate_on_close"] is False
        assert seen["httpx_client_factory"]() == "client"

    def test_missing_client_raises(self, monkeypatch):
        self._fake_import(monkeypatch, SimpleNamespace())
        transport = _http_transport(
            "http://example/mcp",
            headers=None,
            timeout=1.0,
            sse_read_timeout=2.0,
            auth=None,
            http_client_factory=None,
            terminate_on_close=True,
        )

        async def run():
            async with transport():
                pass

        with pytest.raises(ImportError, match="Streamable HTTP client"):
            asyncio.run(run())
