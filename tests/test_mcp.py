"""Tests for dspy_agent_tools.mcp."""

import asyncio
import concurrent.futures
import gc
import importlib
import inspect
import json
import sys
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import pytest
from dspy.utils.dummies import DummyLM
from mcp.types import Tool as MCPTool

from dspy_agent_tools import MCP, MCPTools
from dspy_agent_tools.mcp import (
    _accepted_kwargs,
    _close_all,
    _Connection,
    _error_payload,
    _http_transport,
    _read_only,
    _read_write,
    _SessionProxy,
)

mcp_module = importlib.import_module("dspy_agent_tools.mcp")

SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"
RETRY_SERVER = Path(__file__).parent / "fixtures" / "retry_server.py"


def start(**kwargs):
    return MCP(command=sys.executable, args=[str(SERVER)], **kwargs)


def start_retry(**kwargs):
    return MCP(command=sys.executable, args=[str(RETRY_SERVER)], **kwargs)


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

    def test_exposes_real_python_signatures(self, tools):
        by_name = {tool.name: tool for tool in tools}
        assert str(inspect.signature(by_name["add"].func)) == "(a: int, b: int)"
        assert str(inspect.signature(by_name["echo"].func)) == "(text: str)"

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


class TestReadOnlyRetry:
    def test_replays_read_only_tool_until_it_answers(self):
        with start_retry(retry_timeout=5) as tools:
            by_name = {tool.name: tool for tool in tools}
            assert json.loads(by_name["flaky_read"](text="hi")) == {"echo": "hi"}

    def test_async_path_replays_too(self):
        with start_retry(retry_timeout=5) as tools:
            by_name = {tool.name: tool for tool in tools}
            result = asyncio.run(by_name["flaky_read"].acall(text="hi"))
            assert json.loads(result) == {"echo": "hi"}

    def test_structured_mode_replays_mapping_payloads(self):
        with start_retry(retry_timeout=5, result_mode="structured") as tools:
            by_name = {tool.name: tool for tool in tools}
            result = by_name["flaky_read"](text="hi")
            payload = json.loads(result) if isinstance(result, str) else result
            assert payload == {"echo": "hi"}

    def test_write_tools_are_never_replayed(self):
        with start_retry(retry_timeout=5) as tools:
            by_name = {tool.name: tool for tool in tools}
            started = time.monotonic()
            result = by_name["flaky_write"](text="hi")
            assert time.monotonic() - started < 1.0
            assert json.loads(result)["error"] == "write refused"

    def test_zero_retry_timeout_disables_replay(self):
        with start_retry(retry_timeout=0) as tools:
            by_name = {tool.name: tool for tool in tools}
            result = by_name["flaky_read"](text="hi")
            assert json.loads(result)["error"] == "still starting"

    def test_retry_budget_bounds_a_persistent_error(self):
        with start_retry(retry_timeout=0.2) as tools:
            by_name = {tool.name: tool for tool in tools}
            started = time.monotonic()
            result = by_name["always_fails"](text="hi")
            elapsed = time.monotonic() - started
            assert 0.2 <= elapsed < 2.0
            assert json.loads(result)["error"] == "still unavailable"


class TestErrorPayload:
    @pytest.mark.parametrize(
        "value",
        [
            {"error": "still starting"},
            '{"error": "still starting"}',
            {"ok": False, "condition": "index_updating"},
            ['{"ok": false}'],
        ],
    )
    def test_detects_error_shapes(self, value):
        assert _error_payload(value)

    @pytest.mark.parametrize(
        "value",
        [
            {"results": []},
            '{"results": []}',
            "plain text",
            {"ok": True},
            {"error": ""},
            b"bytes",
        ],
    )
    def test_ignores_success_shapes(self, value):
        assert not _error_payload(value)


class TestReadOnlyAnnotation:
    def test_reads_both_sdk_spellings(self):
        assert _read_only(
            SimpleNamespace(annotations=SimpleNamespace(read_only_hint=True))
        )
        assert _read_only(
            SimpleNamespace(annotations=SimpleNamespace(readOnlyHint=True))
        )
        assert _read_only(SimpleNamespace(annotations={"readOnlyHint": True}))

    def test_rejects_missing_or_false_annotations(self):
        assert not _read_only(SimpleNamespace(annotations=None))
        assert not _read_only(SimpleNamespace(annotations=SimpleNamespace()))
        assert not _read_only(
            SimpleNamespace(annotations=SimpleNamespace(read_only_hint=False))
        )
        assert not _read_only(SimpleNamespace())


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
            MCP(command=sys.executable, args=[str(slow)], startup_timeout=0.5)

    def test_startup_failure_propagates(self):
        broken = Path(__file__).parent / "fixtures" / "broken_server.py"
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

        monkeypatch.setattr(mcp_module, "_Connection", NoSessionConnection)

        with pytest.raises(RuntimeError, match="failed to initialize"):
            MCP(command="irrelevant")

        assert closed == [True]

    def test_mcp_tools_expose_schema_signatures(self):
        class Session:
            def __init__(self):
                self.arguments = None

            async def call_tool(self, name, arguments=None):
                self.arguments = arguments
                return SimpleNamespace(content=[], is_error=False)

        advertised = MCPTool(
            name="search",
            description="Search things.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "note": {"type": "string"},
                },
                "required": ["query"],
            },
        )
        session = Session()
        base = dspy.Tool.from_mcp_tool(session, advertised)
        tool = mcp_module._as_sync_capable(
            base,
            mcp_module._mcp_signature(
                advertised, base.args or {}, base.arg_types or {}
            ),
        )

        signature = inspect.signature(tool.func)
        assert list(signature.parameters) == ["query", "limit", "note"]
        assert signature.parameters["query"].default is inspect.Parameter.empty
        assert signature.parameters["limit"].default == 10
        assert signature.parameters["note"].default is None

        assert tool(query="dspy", limit=None, note=None) == []
        assert session.arguments == {"query": "dspy"}
        assert asyncio.run(tool.acall(query="rlm", limit=None, note=None)) == []
        assert session.arguments == {"query": "rlm"}

    def test_mcp_signature_tolerates_alternate_schemas(self):
        signature = mcp_module._mcp_signature(
            SimpleNamespace(inputSchema={}),
            {"query": {"type": "string"}},
            {"query": str},
        )
        assert list(signature.parameters) == ["query"]

        assert (
            mcp_module._mcp_signature(
                SimpleNamespace(input_schema="not a schema"), {}, {}
            )
            == inspect.Signature()
        )

        flagged = mcp_module._mcp_signature(
            SimpleNamespace(input_schema={"properties": {"flag": True}}),
            {"flag": {"type": "boolean"}},
            {"flag": bool},
        )
        assert flagged.parameters["flag"].default is None


class TestTransportSelection:
    @pytest.fixture()
    def selected(self, monkeypatch):
        calls = []

        for kind, name in (
            ("stdio", "_stdio_transport"),
            ("http", "_http_transport"),
            ("sse", "_sse_transport"),
        ):

            def factory(*args, _kind=kind, **kwargs):
                calls.append(_kind)
                return lambda: None

            monkeypatch.setattr(mcp_module, name, factory)
        monkeypatch.setattr(
            mcp_module, "_build_tools", lambda factory, **kwargs: factory
        )
        return calls

    def test_infers_stdio_from_command(self, selected):
        MCP(command="npx", args=["server"])
        assert selected == ["stdio"]

    def test_infers_http_from_url(self, selected):
        MCP(url="https://example.com/mcp")
        assert selected == ["http"]

    def test_explicit_http(self, selected):
        MCP(url="https://example.com/mcp", transport="http")
        assert selected == ["http"]

    def test_explicit_sse(self, selected):
        MCP(url="https://example.com/sse", transport="sse")
        assert selected == ["sse"]

    def test_requires_command_or_url(self):
        with pytest.raises(ValueError, match="requires command"):
            MCP()

    def test_rejects_command_and_url(self):
        with pytest.raises(ValueError, match="not both"):
            MCP(command="npx", url="https://example.com/mcp")

    def test_rejects_unknown_transport(self):
        with pytest.raises(ValueError, match="Unknown transport"):
            MCP(command="npx", transport="websocket")

    def test_stdio_requires_command(self):
        with pytest.raises(ValueError, match="requires command"):
            MCP(transport="stdio")

    def test_stdio_rejects_url(self):
        with pytest.raises(ValueError, match="does not accept url"):
            MCP(url="https://example.com/mcp", transport="stdio")

    def test_http_requires_url(self):
        with pytest.raises(ValueError, match="requires url"):
            MCP(transport="http")

    def test_http_rejects_command(self):
        with pytest.raises(ValueError, match="does not accept command"):
            MCP(command="npx", transport="http")

    def test_sse_requires_url(self):
        with pytest.raises(ValueError, match="requires url"):
            MCP(transport="sse")

    def test_custom_transport_rejects_command_or_url(self):
        with pytest.raises(ValueError, match="not both"):
            MCP(transport=lambda: None, command="npx")


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
