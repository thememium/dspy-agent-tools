"""Tests for dspy_agent_tools.mcp."""

import asyncio
import gc
import sys
import weakref
from pathlib import Path

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from dspy_agent_tools import MCPTools, mcp

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
