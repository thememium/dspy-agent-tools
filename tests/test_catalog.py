"""Tests for the generic Toolbox discovery facade."""

import asyncio
import sys
from pathlib import Path
from typing import Any, cast

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from dspy_agent_tools import MCP, MCPTools, Toolbox

SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


async def greet(name: str = "world") -> str:
    """Greet someone."""
    return f"hello {name}"


def local_echo(text: str) -> str:
    """Echo text locally."""
    return f"local:{text}"


def by_name(tools):
    return {tool.name: tool for tool in tools}


class _Closer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestSearchMode:
    def test_exposes_discovery_facade_for_plain_functions(self):
        box = Toolbox([add, greet], mode="search")
        assert set(by_name(box)) == {
            "search_tools",
            "get_tool_details",
            "call_tool",
        }
        assert all(isinstance(tool, dspy.Tool) for tool in box)
        assert {entry.name for entry in box.entries} == {"add", "greet"}

    def test_searches_and_calls_local_tools(self):
        box = Toolbox([add, dspy.Tool(greet)], mode="search")
        tools = by_name(box)

        assert tools["search_tools"](query="add integers").startswith("1. add(a, b)")
        assert "Add two integers." in tools["get_tool_details"](name="add")
        assert tools["call_tool"](name="add", arguments={"a": 2, "b": 3}) == 5
        assert (
            asyncio.run(tools["call_tool"].acall(name="greet", arguments={}))
            == "hello world"
        )

    def test_name_prefix_applies_to_tools_and_facades(self):
        box = Toolbox([add], mode="search", name_prefix="local_")
        tools = by_name(box)

        assert set(tools) == {
            "local_search_tools",
            "local_get_tool_details",
            "local_call_tool",
        }
        assert "local_add" in tools["local_search_tools"](query="add")
        assert (
            tools["local_call_tool"](name="local_add", arguments={"a": 1, "b": 2}) == 3
        )

    def test_flattens_nested_iterables_and_generators(self):
        tools = [greet]
        box = Toolbox([[dspy.Tool(add)], (tool for tool in tools)], mode="search")
        assert {entry.name for entry in box.entries} == {"add", "greet"}
        assert (
            by_name(box)["call_tool"](name="greet", arguments={"name": "DSPy"})
            == "hello DSPy"
        )

    def test_rejects_duplicate_tool_names(self):
        with pytest.raises(ValueError, match="Duplicate tool name 'add'"):
            Toolbox([add, dspy.Tool(add)], mode="search")

    def test_runs_local_tools_in_a_react_loop(self):
        class QA(dspy.Signature):
            question: str = dspy.InputField()
            answer: str = dspy.OutputField()

        box = Toolbox([add], mode="search")
        lm = DummyLM(
            [
                {
                    "next_thought": "find the tool",
                    "next_tool_name": "search_tools",
                    "next_tool_args": {"query": "add integers"},
                },
                {
                    "next_thought": "call it",
                    "next_tool_name": "call_tool",
                    "next_tool_args": {"name": "add", "arguments": {"a": 2, "b": 3}},
                },
                {
                    "next_thought": "done",
                    "next_tool_name": "finish",
                    "next_tool_args": {},
                },
                {"reasoning": "the tool returned the sum", "answer": "5"},
            ]
        )
        agent = dspy.ReAct(QA, tools=box, max_iters=3)

        with dspy.context(lm=lm):
            prediction = agent(question="what is 2+3?")

        assert "add" in prediction.trajectory["observation_0"]
        assert prediction.answer == "5"

    def test_accepts_external_tool_metadata(self):
        tool = dspy.Tool(add, name="sum", desc="Add numbers.", arg_desc={"a": "left"})
        box = Toolbox(tool, mode="search")

        assert "sum(a, b): Add numbers." in by_name(box)["search_tools"](query="sum")


class TestCodeMode:
    def test_returns_structured_search_and_details(self):
        box = Toolbox([add, greet], mode="code")
        tools = by_name(box)

        records = tools["search_tools"](query="greet")
        assert records[0]["name"] == "greet"
        assert records[0]["required"] == []

        details = tools["get_tool_details"](name="greet")
        assert details["input_schema"]["properties"]["name"]["default"] == "world"

    def test_call_returns_the_tool_result_unchanged(self):
        box = Toolbox([add], mode="code")
        assert by_name(box)["call_tool"](name="add", arguments={"a": 4, "b": 5}) == 9

    def test_runs_local_tools_in_an_rlm_interpreter(self):
        box = Toolbox([add], mode="code")
        program = dspy.RLM("question -> answer", tools=box, max_iters=2)
        lm = DummyLM(
            [
                {
                    "reasoning": "call add",
                    "code": "print(call_tool('add', {'a': 2, 'b': 3}))",
                },
                {"reasoning": "done", "code": "print('answer is 5')"},
                {"answer": "5"},
            ]
        )

        with dspy.context(lm=lm):
            result = program(question="what is 2+3?")

        assert result.trajectory[0]["output"] == "5\n"
        assert result.answer == "5"

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError, match="Unknown mode"):
            Toolbox([add], mode=cast(Any, "default"))


class TestSources:
    def test_closes_owned_sources(self):
        closer = _Closer()
        box = Toolbox(MCPTools([dspy.Tool(add)], [closer]), mode="search")

        box.close()
        box.close()
        assert closer.closed

    def test_addition_keeps_sources_alive(self):
        closer = _Closer()
        box = Toolbox(MCPTools([dspy.Tool(add)], [closer]), mode="search")

        combined = box + [greet]
        combined.close()
        assert closer.closed
        assert {entry.name for entry in combined.entries} == {"add", "greet"}

    def test_mcp_and_local_tools_share_one_catalog(self):
        server = MCP(command=sys.executable, args=[str(SERVER)], name_prefix="mcp_")
        box = Toolbox([server, local_echo], mode="search")
        tools = by_name(box)

        try:
            response = tools["search_tools"](query="echo")
            assert "local_echo(text)" in response
            assert "mcp_echo(text)" in response
            assert tools["call_tool"](name="mcp_add", arguments={"a": 1, "b": 2}) == "3"
            assert (
                tools["call_tool"](name="local_echo", arguments={"text": "hi"})
                == "local:hi"
            )
        finally:
            box.close()

        assert server._connections == []

    def test_rejects_unusable_entries(self):
        with pytest.raises(TypeError, match="Toolbox entries must be"):
            Toolbox([42], mode="search")
