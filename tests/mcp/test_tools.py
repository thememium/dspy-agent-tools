"""Tests for dspy_agent_tools.mcp.tools."""

import asyncio
import importlib
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from dspy_agent_tools import MCP, MCPTools
from dspy_agent_tools.mcp.retry import _HTTPFailures
from dspy_agent_tools.mcp.tools import _http_transport, _sse_transport

from .helpers import start, tools_module


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

            monkeypatch.setattr(tools_module, name, factory)
        monkeypatch.setattr(
            tools_module, "_build_tools", lambda factory, **kwargs: factory
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

    def test_custom_transport_is_used_directly(self, monkeypatch):
        def custom():
            return None

        monkeypatch.setattr(
            tools_module, "_build_tools", lambda factory, **kwargs: factory
        )

        assert MCP(transport=custom) is custom


class TestHttpTransport:
    def _fake_import(self, monkeypatch, module):
        real_import = importlib.import_module

        def fake_import(name, package=None):
            if name == "mcp.client.streamable_http":
                return module
            return real_import(name, package)

        monkeypatch.setattr(
            tools_module, "importlib", SimpleNamespace(import_module=fake_import)
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

    def test_builds_sdk_http_client_and_yields_streams(self, monkeypatch):
        self._fake_import(monkeypatch, _fake_streamable_sdk())
        failures = _HTTPFailures()
        transport = _http_transport(
            "http://example/mcp",
            headers=None,
            timeout=1.0,
            sse_read_timeout=2.0,
            auth=None,
            http_client_factory=None,
            terminate_on_close=True,
            failures=failures,
        )

        async def run():
            async with transport() as streams:
                return streams

        assert asyncio.run(run()) == ("read", "write")

    def test_uses_supplied_httpclient_factory(self, monkeypatch):
        self._fake_import(monkeypatch, _fake_streamable_sdk())
        transport = _http_transport(
            "http://example/mcp",
            headers=None,
            timeout=1.0,
            sse_read_timeout=2.0,
            auth=None,
            http_client_factory=_FakeHttpClient,
            terminate_on_close=True,
        )

        async def run():
            async with transport() as streams:
                return streams

        assert asyncio.run(run()) == ("read", "write")

    def test_sse_transport_wraps_client_factory(self, monkeypatch):
        import mcp.client.sse as sse_module

        seen = {}
        built = []

        @asynccontextmanager
        async def sse_client(**kwargs):
            seen.update(kwargs)
            yield ("read", "write")

        monkeypatch.setattr(sse_module, "sse_client", sse_client)

        def factory():
            built.append(True)
            return object()

        transport = _sse_transport(
            "http://example/sse",
            headers={"X-Test": "1"},
            timeout=5.0,
            sse_read_timeout=2.0,
            auth=None,
            httpx_client_factory=factory,
        )

        async def run():
            async with transport() as streams:
                return streams

        assert asyncio.run(run()) == ("read", "write")
        assert seen["url"] == "http://example/sse"
        assert callable(seen["httpx_client_factory"])
        assert seen["httpx_client_factory"](url="ignored") is not None
        assert built == [True]


class _FakeHttpClient:
    def __init__(self):
        self.event_hooks = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _fake_streamable_sdk():
    @asynccontextmanager
    async def streamable_http_client(url, *, http_client=None, terminate_on_close=True):
        yield ("read", "write")

    def create_mcp_http_client(**kwargs):
        return _FakeHttpClient()

    return SimpleNamespace(
        streamable_http_client=streamable_http_client,
        create_mcp_http_client=create_mcp_http_client,
    )
