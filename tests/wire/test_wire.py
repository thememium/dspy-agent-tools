"""Tests for dspy_agent_tools.Wire."""

from __future__ import annotations

import sys
from typing import Any, cast

import dspy
import pytest

from dspy_agent_tools import (
    Memory,
    Offloader,
    Session,
    Wire,
    deny_tools,
)
from tests.patch.helpers import QA, react_instructions


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.turns: list[list[dict[str, str]]] = []

    def create_memory(
        self,
        *,
        user_id: str,
        content: str,
        session_id: str = "",
        memory_type: str = "semantic",
    ) -> dict[str, Any]:
        row = {
            "id": f"m{len(self.rows)}",
            "user_id": user_id,
            "session_id": session_id,
            "content": content,
            "memory_type": memory_type,
            "relevance_score": None,
        }
        self.rows.append(row)
        return row

    def create_memories(
        self,
        *,
        user_id: str,
        contents: list[dict[str, str]],
        session_id: str = "",
        extract: bool = True,
    ) -> list[dict[str, Any]]:
        self.turns.append(contents)
        return [
            self.create_memory(user_id=user_id, content=contents[0]["content"][:40])
        ]

    def search_memories(
        self,
        *,
        user_id: str,
        query: str,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        term = query.split()[0].lower()
        return [row for row in self.rows if term in row["content"].lower()][:limit]


def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


def long_echo(text: str) -> str:
    """Return a long echo."""
    return "x" * 100


def huge() -> str:
    """Return more than the default offload limit."""
    return "x" * 25_000


class TestReAct:
    def test_retools_in_place(self):
        agent = dspy.ReAct(QA, tools=[echo], max_iters=3)
        original = cast(Any, agent.tools)["echo"]

        returned = Wire(agent)

        assert returned is agent
        assert agent.max_iters == 3
        assert set(cast(Any, agent.tools)) == {"echo", "finish"}
        assert cast(Any, agent.tools)["echo"] is not original
        assert cast(Any, agent.tools)["echo"](text="hi") == "echo:hi"

    def test_adds_memory_and_recalled_guidance(self):
        store = FakeStore()
        memory = Memory(store, user_id="u1")
        memory.remember("prefers tabs")
        agent = dspy.ReAct(QA, tools=[echo])

        Wire(agent, memory=memory, query="tabs")

        assert set(cast(Any, agent.tools)) >= {"memory_recall", "memory_remember"}
        assert "## Memory" in react_instructions(agent)
        assert "prefers tabs" in react_instructions(agent)

    def test_without_query_no_recall_is_injected(self):
        store = FakeStore()
        memory = Memory(store, user_id="u1")
        memory.remember("prefers tabs")
        agent = dspy.ReAct(QA, tools=[echo])

        Wire(agent, memory=memory)

        assert "## Memory" in react_instructions(agent)
        assert "prefers tabs" not in react_instructions(agent)

    def test_policy_denies_a_tool_call(self):
        agent = dspy.ReAct(QA, tools=[echo])

        def deny(name: str, arguments: dict[str, Any]) -> str | None:
            return "blocked" if name == "echo" else None

        Wire(agent, policy=deny)

        with pytest.raises(PermissionError, match="blocked"):
            cast(Any, agent.tools)["echo"](text="hi")

    def test_policy_list_is_conjuncted(self):
        agent = dspy.ReAct(QA, tools=[echo])
        Wire(
            agent,
            policy=[lambda name, arguments: None, deny_tools("echo", reason="no")],
        )

        with pytest.raises(PermissionError, match="no"):
            cast(Any, agent.tools)["echo"](text="hi")

    def test_true_flags_build_defaults(self, session: Session):
        agent = dspy.ReAct(QA, tools=[huge])

        Wire(agent, offload=True, session=session)

        handle = cast(Any, agent.tools)["huge"]()
        assert handle["offloaded"] is True
        [event] = session.events()
        assert event["data"] == {"tool": "huge"}

    def test_false_flags_mean_none(self):
        agent = dspy.ReAct(QA, tools=[echo])

        returned = Wire(agent, offload=False, session=False)

        assert returned is agent

    def test_memory_store_is_accepted_with_a_user(self):
        store = FakeStore()
        agent = dspy.ReAct(QA, tools=[echo])

        Wire(agent, memory=store, user_id="u9")

        assert "memory_recall" in cast(Any, agent.tools)
        assert "user `u9`" in react_instructions(agent)

    def test_memory_store_needs_a_user(self):
        with pytest.raises(TypeError, match="needs user_id"):
            Wire(dspy.ReAct(QA, tools=[echo]), memory=FakeStore())

    def test_memory_rejects_an_unrelated_object(self):
        with pytest.raises(TypeError, match="takes Memory"):
            Wire(dspy.ReAct(QA, tools=[echo]), memory=object())

    def test_rewiring_replaces_middleware(self, session: Session):
        agent = dspy.ReAct(QA, tools=[echo])
        Wire(agent, session=session, policy=lambda name, arguments: None)
        assert cast(Any, agent.tools)["echo"](text="one") == "echo:one"

        Wire(agent, session=session, policy=deny_tools("echo", reason="second"))
        with pytest.raises(PermissionError, match="second"):
            cast(Any, agent.tools)["echo"](text="two")

        Wire(agent, session=session, policy=lambda name, arguments: None)
        assert cast(Any, agent.tools)["echo"](text="three") == "echo:three"
        assert len(session.events()) == 2

    def test_offload_and_session(self, tmp_path, session: Session):
        agent = dspy.ReAct(QA, tools=[long_echo])

        Wire(
            agent,
            offload=Offloader(tmp_path, limit=10, session=session),
            session=session,
        )

        handle = cast(Any, agent.tools)["long_echo"](text="go")

        assert handle["offloaded"] is True
        [event] = session.events()
        assert event["data"] == {"tool": "long_echo"}
        [artifact] = session.artifacts()
        assert artifact["note"] == "long_echo"


class TestRLM:
    def test_installs_all_four_hooks(self):
        agent = dspy.RLM(QA)

        returned = Wire(agent)

        assert returned is agent
        for name in (
            "_hook_pre_iteration",
            "_hook_pre_execution",
            "_hook_post_execution",
            "_hook_post_iteration",
        ):
            assert hasattr(agent, name)

    def test_installs_with_every_option(self, tmp_path):
        store = FakeStore()
        agent = dspy.RLM(QA)

        Wire(
            agent,
            memory=Memory(store, user_id="u1"),
            session=Session(),
            offload=Offloader(tmp_path, limit=10),
            policy=lambda name, arguments: None,
            query="tabs",
            trace=False,
        )

        assert hasattr(agent, "_hook_pre_iteration")
        assert set(cast(Any, agent._user_tools)) == {
            "memory_recall",
            "memory_remember",
        }

    def test_memory_tools_are_wrapped_by_the_policy(self):
        store = FakeStore()
        agent = dspy.RLM(QA)

        Wire(
            agent,
            memory=Memory(store, user_id="u1"),
            policy=deny_tools("memory_recall"),
        )

        with pytest.raises(PermissionError, match="memory_recall"):
            agent._user_tools["memory_recall"](query="x")

    def test_missing_hooks_package_explains_the_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        agent = dspy.RLM(QA)
        monkeypatch.setitem(sys.modules, "dspy_rlm_hooks", None)

        with pytest.raises(ImportError, match=r"dspy-agent-tools\[hooks\]"):
            Wire(agent)


class TestEndToEnd:
    def test_react_loop_calls_the_wrapped_tool(self, session: Session):
        from dspy.utils.dummies import DummyLM

        agent = dspy.ReAct(QA, tools=[echo], max_iters=3)
        Wire(agent, session=session, policy=lambda name, arguments: None)
        lm = DummyLM(
            [
                {
                    "next_thought": "echo it",
                    "next_tool_name": "echo",
                    "next_tool_args": {"text": "hi"},
                },
                {
                    "next_thought": "done",
                    "next_tool_name": "finish",
                    "next_tool_args": {},
                },
                {"reasoning": "done", "answer": "echo:hi"},
            ]
        )

        with dspy.context(lm=lm):
            prediction = agent(question="echo hi")

        assert prediction.answer == "echo:hi"
        [event] = session.events()
        assert event["output"] == "echo:hi"
        assert event["data"] == {"tool": "echo"}

    def test_rlm_hooks_fire_end_to_end(self, tmp_path, session: Session):
        from dspy.utils.dummies import DummyLM

        store = FakeStore()
        memory = Memory(store, user_id="u1")
        memory.remember("prefers tabs")
        agent = dspy.RLM(QA, max_iters=2, max_output_chars=50)
        Wire(
            agent,
            memory=memory,
            session=session,
            offload=Offloader(tmp_path, limit=20),
            query="tabs",
        )
        lm = DummyLM(
            [
                {
                    "reasoning": "make a big value",
                    "code": "```python\nbig = 'x' * 100\nbig\n```",
                },
                {"reasoning": "done", "code": "```python\nprint('done')\n```"},
                {"answer": "ok"},
            ]
        )

        with dspy.context(lm=lm):
            prediction = agent(question="what do I prefer?")

        assert prediction.answer == "ok"
        events = session.events()
        assert [event["iteration"] for event in events] == [0, 1]
        assert events[0]["output"].startswith("{'offloaded'")
        assert store.turns and "what do I prefer?" in store.turns[0][0]["content"]
        [artifact] = session.artifacts()
        assert artifact["note"] == "rlm-0"


class TestUnsupported:
    def test_rejects_other_modules(self):
        with pytest.raises(TypeError, match="supports dspy.ReAct"):
            Wire(object())
