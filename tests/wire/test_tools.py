"""Tests for dspy_agent_tools.wire.tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import dspy
import pytest

from dspy_agent_tools import Offloader, Session, deny_tools, read_file, wrap_tools
from dspy_agent_tools.patch.guide import family_of
from dspy_agent_tools.wire import observe
from tests.patch.helpers import QA
from tests.wire.test_observe import FakeTrace


def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


async def async_echo(text: str) -> str:
    """Echo the text back asynchronously."""
    return f"async:{text}"


def explode() -> str:
    """Always fail."""
    raise RuntimeError("boom")


class TestWrapTools:
    def test_preserves_name_description_and_schema(self):
        original = dspy.Tool(echo)

        [wrapped] = wrap_tools([original])

        assert wrapped.name == "echo"
        assert wrapped.desc == "Echo the text back."
        assert wrapped.args == original.args
        assert wrapped.arg_types == original.arg_types

    def test_calls_through(self):
        [wrapped] = wrap_tools([echo])

        assert wrapped(text="hi") == "echo:hi"

    def test_digest_falls_back_to_repr(self):
        def circular() -> list[object]:
            """Return a value json cannot serialise."""
            value: list[object] = []
            value.append(value)
            return value

        [wrapped] = wrap_tools([circular])

        assert wrapped() is not None

    def test_accepts_a_mapping(self):
        agent = dspy.ReAct(QA, tools=[echo])

        wrapped = wrap_tools(agent.tools)

        assert any(tool.name == "echo" for tool in wrapped)

    def test_keeps_the_original_module_for_family_guidance(self):
        [wrapped] = wrap_tools([read_file])

        family = family_of(wrapped)
        assert family is not None
        assert family.key == "files"

    def test_offloads_an_oversized_result(self, tmp_path: Path):
        offload = Offloader(tmp_path, limit=10)
        [wrapped] = wrap_tools([echo], offload=offload)

        handle = wrapped(text="x" * 50)

        assert handle["offloaded"] is True
        assert Path(handle["path"]).read_text().startswith("echo:")

    def test_policy_denies_the_call(self):
        def deny(name: str, arguments: dict[str, Any]) -> str | None:
            return "not allowed" if name == "echo" else None

        [wrapped] = wrap_tools([echo], policy=deny)

        with pytest.raises(PermissionError, match="not allowed"):
            wrapped(text="hi")

    def test_policy_allows_the_call(self):
        seen: list[tuple[str, dict[str, Any]]] = []

        def allow(name: str, arguments: dict[str, Any]) -> None:
            seen.append((name, arguments))

        [wrapped] = wrap_tools([echo], policy=allow)

        assert wrapped(text="hi") == "echo:hi"
        assert seen == [("echo", {"text": "hi"})]

    def test_session_records_success_and_error(self, session: Session):
        [good, bad] = wrap_tools([echo, explode], session=session)

        assert good(text="hi") == "echo:hi"
        with pytest.raises(RuntimeError):
            bad()

        events = session.events()
        assert events[0]["output"] == "echo:hi"
        assert events[0]["data"] == {"tool": "echo"}
        assert events[1]["error"] == "RuntimeError: boom"
        assert events[1]["data"] == {"tool": "explode"}

    def test_async_tool_keeps_both_paths(self):
        [wrapped] = wrap_tools([async_echo])

        assert asyncio.run(wrapped.acall(text="hi")) == "async:hi"
        assert wrapped(text="hi") == "async:hi"

    def test_async_tool_policy_and_session(self, session: Session):
        def deny(name: str, arguments: dict[str, Any]) -> str | None:
            return "no async" if name == "async_echo" else None

        [wrapped] = wrap_tools([async_echo], session=session, policy=deny)

        with pytest.raises(PermissionError, match="no async"):
            asyncio.run(wrapped.acall(text="hi"))
        assert session.events() == []

    def test_async_error_is_recorded(self, session: Session):
        async def fail() -> str:
            raise RuntimeError("async boom")

        [wrapped] = wrap_tools([fail], session=session)

        with pytest.raises(RuntimeError):
            asyncio.run(wrapped.acall())

        [event] = session.events()
        assert event["error"] == "RuntimeError: async boom"

    def test_wrapping_twice_uses_the_original(self, session: Session):
        [once] = wrap_tools([echo], session=session)

        [twice] = wrap_tools([once], session=session)

        assert twice(text="hi") == "echo:hi"
        assert len(session.events()) == 1

    def test_policy_list_is_supported(self):
        [wrapped] = wrap_tools(
            [echo], policy=[lambda name, arguments: None, deny_tools("echo")]
        )

        with pytest.raises(PermissionError):
            wrapped(text="hi")

    def test_trace_false_skips_spans(self, monkeypatch: pytest.MonkeyPatch):
        fake = FakeTrace()
        monkeypatch.setattr(observe, "_trace", lambda: fake)
        [wrapped] = wrap_tools([echo], trace=False)

        wrapped(text="hi")

        assert fake.tracer.spans == []

    def test_trace_emits_a_span(self, monkeypatch: pytest.MonkeyPatch):
        fake = FakeTrace()
        monkeypatch.setattr(observe, "_trace", lambda: fake)
        [wrapped] = wrap_tools([echo])

        wrapped(text="hi")

        [span] = fake.tracer.spans
        assert span.name == "execute_tool"
        assert span.attributes["gen_ai.tool.name"] == "echo"
        assert span.attributes["gen_ai.tool.result.chars"] == len("echo:hi")
        assert span.ended

    def test_trace_marks_an_offloaded_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        fake = FakeTrace()
        monkeypatch.setattr(observe, "_trace", lambda: fake)
        [wrapped] = wrap_tools([echo], offload=Offloader(tmp_path, limit=10))

        wrapped(text="x" * 50)

        [span] = fake.tracer.spans
        assert span.attributes["gen_ai.tool.result.offloaded"] is True
        assert span.attributes["gen_ai.tool.result.chars"] > 10
