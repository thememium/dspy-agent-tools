"""Tests for Patch(replace=True), used by Wire to install middleware."""

from __future__ import annotations

from typing import Any, cast

import dspy
import pytest

from dspy_agent_tools import Patch, read_file

from .helpers import QA, react_instructions


def first(x: int) -> int:
    """The first implementation."""
    return x + 1


def second(x: int) -> int:
    """The second implementation."""
    return x + 2


def same_name(function):
    function.__name__ = "shared"
    return function


class TestReplace:
    def test_different_function_with_same_name_raises(self):
        agent = dspy.ReAct(QA, tools=[dspy.Tool(same_name(first))])

        with pytest.raises(ValueError, match="already has a different tool"):
            Patch(agent, tools=dspy.Tool(same_name(second)))

    def test_replace_installs_the_new_function(self):
        agent = dspy.ReAct(QA, tools=[dspy.Tool(same_name(first))])

        returned = Patch(agent, tools=dspy.Tool(same_name(second)), replace=True)

        assert returned is agent
        assert cast(Any, agent.tools)["shared"].func is second
        assert cast(Any, agent.tools)["shared"](x=1) == 3
        assert "The second implementation." in react_instructions(agent)

    def test_replace_keeps_other_tools_and_settings(self):
        agent = dspy.ReAct(
            QA, tools=[dspy.Tool(same_name(first)), read_file], max_iters=7
        )

        Patch(agent, tools=dspy.Tool(same_name(second)), replace=True)

        assert agent.max_iters == 7
        assert "read_file" in cast(Any, agent.tools)
        assert "## Files" in react_instructions(agent)

    def test_same_function_is_not_a_replacement(self):
        tool = dspy.Tool(same_name(first))
        agent = dspy.ReAct(QA, tools=[tool])

        returned = Patch(agent, tools=tool, replace=True)

        assert returned is agent
        assert cast(Any, agent.tools)["shared"].func is first

    def test_replace_with_drop(self):
        agent = dspy.ReAct(QA, tools=[dspy.Tool(same_name(first)), read_file])

        Patch(
            agent,
            tools=dspy.Tool(same_name(second)),
            drop="read_file",
            replace=True,
        )

        assert cast(Any, agent.tools)["shared"].func is second
        assert "read_file" not in cast(Any, agent.tools)
        assert "## Files" not in react_instructions(agent)

    def test_replace_on_rlm(self):
        agent = dspy.RLM(QA, tools=[dspy.Tool(same_name(first))])

        Patch(agent, tools=dspy.Tool(same_name(second)), replace=True)

        assert agent._user_tools["shared"].func is second
