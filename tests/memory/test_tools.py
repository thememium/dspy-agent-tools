"""Tests for dspy_agent_tools.memory."""

from __future__ import annotations

from typing import Any, ClassVar, cast

import dspy
import pytest

from dspy_agent_tools import Memory, MemoryTools, Patch, Toolbox
from dspy_agent_tools.memory.tools import MAX_CONTENT, MAX_RECORDS
from dspy_agent_tools.patch.guide import family_of
from tests.patch.helpers import QA, react_instructions


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.turns: list[list[dict[str, str]]] = []
        self.deleted: list[str] = []

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
            "relevance_score": 0.9,
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
        return [self.create_memory(user_id=user_id, content=contents[0]["content"])]

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

    def delete_memory(self, *, memory_id: str) -> None:
        self.deleted.append(memory_id)


class StoreWithoutDelete(FakeStore):
    delete_memory = None  # type: ignore[assignment]


class PydanticLike:
    def model_dump(self) -> dict[str, Any]:
        return {
            "id": "p1",
            "content": "from a model",
            "memory_type": "semantic",
            "relevance_score": 0.5,
        }


class VarsOnly:
    def __init__(self) -> None:
        self.id = "v1"
        self.content = "from vars"
        self.memory_type = "episodic"
        self.relevance_score = 0.4


class StringOnly:
    def __str__(self) -> str:
        return "just text"


class TestConstruction:
    def test_requires_store_methods(self):
        with pytest.raises(TypeError, match="create_memory"):
            Memory(object(), user_id="u1")

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError, match="Unknown mode"):
            Memory(FakeStore(), user_id="u1", mode=cast(Any, "bad"))

    def test_requires_a_user(self):
        with pytest.raises(ValueError, match="user_id"):
            Memory(FakeStore(), user_id="")

    def test_builds_three_tools(self):
        memory = Memory(FakeStore(), user_id="u1")

        assert isinstance(memory, MemoryTools)
        assert [tool.name for tool in memory] == [
            "memory_recall",
            "memory_remember",
            "memory_forget",
        ]

    def test_name_prefix(self):
        memory = Memory(FakeStore(), user_id="u1", name_prefix="kb_")

        assert [tool.name for tool in memory] == [
            "kb_memory_recall",
            "kb_memory_remember",
            "kb_memory_forget",
        ]

    def test_forget_is_omitted_without_delete(self):
        memory = Memory(StoreWithoutDelete(), user_id="u1")

        assert [tool.name for tool in memory] == [
            "memory_recall",
            "memory_remember",
        ]

    def test_with_tools_keeps_the_source(self):
        memory = Memory(FakeStore(), user_id="u1", session_id="s1")

        replaced = memory.with_tools([dspy.Tool(lambda: "x", name="memory_recall")])

        assert isinstance(replaced, MemoryTools)
        assert [tool.name for tool in replaced] == ["memory_recall"]
        assert replaced.user_id == "u1"
        assert replaced.session_id == "s1"
        assert "user `u1`" in replaced.section()

    def test_family_is_recognised(self):
        memory = Memory(FakeStore(), user_id="u1")

        family = family_of(memory[0])
        assert family is not None
        assert family.key == "memory"


class TestRecall:
    def test_search_mode_returns_text(self):
        memory = Memory(FakeStore(), user_id="u1")
        memory.remember("prefers tabs")

        result = memory[0](query="tabs")

        assert "prefers tabs" in result
        assert "[semantic]" in result

    def test_search_mode_reports_no_match(self):
        memory = Memory(FakeStore(), user_id="u1")

        assert memory[0](query="nothing") == "No memories matched that query."

    def test_code_mode_returns_records(self):
        memory = Memory(FakeStore(), user_id="u1", mode="code")
        memory.remember("prefers tabs")

        [record] = memory[0](query="tabs")

        assert record["content"] == "prefers tabs"
        assert record["type"] == "semantic"
        assert record["score"] == 0.9

    def test_empty_query_skips_the_store(self):
        store = FakeStore()
        memory = Memory(store, user_id="u1")

        assert memory.recall("   ") == []

    def test_non_numeric_limit_defaults(self):
        memory = Memory(FakeStore(), user_id="u1", limit=cast(Any, "many"))

        assert memory.limit == 5

    def test_limit_is_clamped(self):
        store = FakeStore()
        memory = Memory(store, user_id="u1", limit=0)

        assert memory.limit == 1
        memory.recall("x", limit=1_000)
        assert memory.limit == 1

        memory = Memory(store, user_id="u1", limit=1_000)
        assert memory.limit == 50

    def test_session_scope_is_passed(self):
        class RecordingStore(FakeStore):
            seen: ClassVar[dict[str, Any]] = {}

            def search_memories(self, **kwargs: Any) -> list[dict[str, Any]]:
                RecordingStore.seen = kwargs
                return []

        memory = Memory(RecordingStore(), user_id="u1", session_id="s1")

        memory.recall("x")

        assert RecordingStore.seen["session_id"] == "s1"
        assert RecordingStore.seen["user_id"] == "u1"

    def test_empty_session_becomes_none(self):
        class RecordingStore(FakeStore):
            seen: ClassVar[dict[str, Any]] = {}

            def search_memories(self, **kwargs: Any) -> list[dict[str, Any]]:
                RecordingStore.seen = kwargs
                return []

        Memory(RecordingStore(), user_id="u1").recall("x")

        assert RecordingStore.seen["session_id"] is None


class TestRemember:
    def test_remember_returns_a_record(self):
        store = FakeStore()
        memory = Memory(store, user_id="u1")

        record = memory.remember("likes dark mode", memory_type="preference")

        assert record == {
            "id": "m0",
            "content": "likes dark mode",
            "type": "preference",
            "score": 0.9,
        }
        assert store.rows[0]["session_id"] == ""

    def test_remember_tool_uses_the_store(self):
        memory = Memory(FakeStore(), user_id="u1")

        record = memory[1](content="likes dark mode", memory_type="preference")

        assert record["type"] == "preference"

    def test_remember_turn_extracts(self):
        store = FakeStore()
        memory = Memory(store, user_id="u1")

        records = memory.remember_turn([{"role": "user", "content": "I like tabs"}])

        assert records[0]["content"] == "I like tabs"
        assert store.turns[0][0]["role"] == "user"


class TestForget:
    def test_forget_calls_the_store(self):
        store = FakeStore()
        memory = Memory(store, user_id="u1")

        assert memory.forget("m7") == {"forgotten": "m7"}
        assert store.deleted == ["m7"]

    def test_forget_tool(self):
        store = FakeStore()
        memory = Memory(store, user_id="u1")

        assert memory[2](memory_id="m7") == {"forgotten": "m7"}


class TestRecords:
    def test_model_dump_is_used(self):
        class Store(FakeStore):
            def search_memories(self, **kwargs: Any) -> list[Any]:
                return [PydanticLike()]

        [record] = Memory(Store(), user_id="u1").recall("x")

        assert record["id"] == "p1"
        assert record["score"] == 0.5

    def test_vars_is_used(self):
        class Store(FakeStore):
            def search_memories(self, **kwargs: Any) -> list[Any]:
                return [VarsOnly()]

        [record] = Memory(Store(), user_id="u1").recall("x")

        assert record["type"] == "episodic"

    def test_slots_object_falls_back_to_string(self):
        class Slotted:
            __slots__ = ()

            def __str__(self) -> str:
                return "slotted text"

        class Store(FakeStore):
            def search_memories(self, **kwargs: Any) -> list[Any]:
                return [Slotted()]

        [record] = Memory(Store(), user_id="u1").recall("x")

        assert record["content"] == "slotted text"

    def test_string_fallback(self):
        class Store(FakeStore):
            def search_memories(self, **kwargs: Any) -> list[Any]:
                return [StringOnly()]

        [record] = Memory(Store(), user_id="u1").recall("x")

        assert record["content"] == "just text"
        assert record["id"] == ""


class TestFormat:
    def test_caps_records_and_content(self):
        memory = Memory(FakeStore(), user_id="u1")
        records = [
            {"content": "c" * (MAX_CONTENT + 10), "type": "semantic"}
            for _ in range(MAX_RECORDS + 2)
        ]

        text = memory.format(records)

        assert text.count("\n- ") == MAX_RECORDS
        assert "..." in text
        assert len(text) < MAX_RECORDS * (MAX_CONTENT + 20)

    def test_empty_records_render_empty(self):
        assert Memory(FakeStore(), user_id="u1").format([]) == ""

    def test_section_names_scope_and_tools(self):
        memory = Memory(FakeStore(), user_id="u1", session_id="s1")

        section = memory.section()

        assert "## Memory" in section
        assert "user `u1`" in section
        assert "session `s1`" in section
        assert "`memory_recall`" in section


class TestIntegration:
    def test_patch_adds_the_section_and_tools(self):
        agent = dspy.ReAct(QA, tools=[])
        memory = Memory(FakeStore(), user_id="u1")

        Patch(agent, tools=memory)

        assert "## Memory" in react_instructions(agent)
        assert "memory_recall" in cast(Any, agent.tools)

    def test_toolbox_catalogs_memory(self):
        toolbox = Toolbox(Memory(FakeStore(), user_id="u1"), mode="search")

        assert {tool.name for tool in toolbox} == {
            "search_tools",
            "get_tool_details",
            "call_tool",
        }
        assert {entry.name for entry in toolbox.entries} == {
            "memory_recall",
            "memory_remember",
            "memory_forget",
        }
