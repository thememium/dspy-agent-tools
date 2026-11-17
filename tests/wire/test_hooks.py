"""Tests for dspy_agent_tools.wire.hooks."""

from __future__ import annotations

import logging
import sys
from typing import Any

import pytest

from dspy_agent_tools import Offloader, Session
from dspy_agent_tools.wire import hooks as hook_module
from dspy_agent_tools.wire import observe


class FakeMemory:
    """The MemoryTools surface the hooks use, without a store."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.turns: list[list[dict[str, str]]] = []
        self.fail_recall = False
        self.fail_write = False

    def recall(self, query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        if self.fail_recall:
            raise RuntimeError("store offline")
        self.queries.append(query)
        return [{"id": "m1", "content": "prefers tabs", "type": "preference"}]

    def format(self, records: list[dict[str, Any]]) -> str:
        return "MEMORIES: " + "; ".join(str(row["content"]) for row in records)

    def remember(
        self, content: str, *, memory_type: str = "semantic"
    ) -> dict[str, Any]:
        return {"id": "m2", "content": content, "type": memory_type}

    def remember_turn(self, contents: list[dict[str, str]]) -> list[dict[str, Any]]:
        if self.fail_write:
            raise RuntimeError("extraction down")
        self.turns.append(contents)
        return []


def _iteration(**options: Any) -> dict[str, Any]:
    return hook_module.build_hooks(**options)


class TestPreIteration:
    def test_without_memory_is_empty(self):
        pre = _iteration()["pre_iteration_hook"]

        output = pre(1, [], [], {"question": "hi"})

        assert output.prompt_context == ""
        assert output.extra_vars == {}

    def test_recalls_once_and_injects_context(self):
        memory = FakeMemory()
        pre = _iteration(memory=memory)["pre_iteration_hook"]

        first = pre(1, [], [], {"question": "what editor?"})
        second = pre(2, [], [], {"question": "what editor?"})

        assert first.prompt_context == "MEMORIES: prefers tabs"
        assert second.prompt_context == "MEMORIES: prefers tabs"
        assert memory.queries == ["what editor?"]

    def test_fixed_query_overrides_inputs(self):
        memory = FakeMemory()
        pre = _iteration(memory=memory, recall_query="tabs")["pre_iteration_hook"]

        pre(1, [], [], {"question": "ignored"})

        assert memory.queries == ["tabs"]

    def test_recall_failure_is_logged(self, caplog: pytest.LogCaptureFixture):
        memory = FakeMemory()
        memory.fail_recall = True
        pre = _iteration(memory=memory)["pre_iteration_hook"]

        with caplog.at_level(logging.WARNING, logger="dspy_agent_tools.wire"):
            output = pre(1, [], [], {"question": "hi"})

        assert output.prompt_context == ""
        assert "Memory recall failed" in caplog.text


class TestPreExecution:
    def test_without_policy_keeps_code(self):
        pre = _iteration()["pre_execution_hook"]

        output = pre(1, "x = 1", [], [], {})

        assert output.code == "x = 1"

    def test_allowed_code_is_kept(self):
        pre = _iteration(policy=lambda name, args: None)["pre_execution_hook"]

        output = pre(1, "x = 1", [], [], {})

        assert output.code == "x = 1"

    def test_policy_list_is_supported(self):
        from dspy_agent_tools import deny_code

        pre = _iteration(policy=[lambda name, arguments: None, deny_code()])[
            "pre_execution_hook"
        ]

        output = pre(1, "x = 1", [], [], {})

        with pytest.raises(PermissionError):
            exec(output.code, {})  # noqa: S102

    def test_denied_code_raises_in_the_repl(self):
        def deny(name: str, arguments: dict[str, Any]) -> str | None:
            assert name == "code"
            assert arguments["iteration"] == 2
            return "writes are not allowed"

        pre = _iteration(policy=deny)["pre_execution_hook"]

        output = pre(2, "open('x', 'w')", [], [], {})

        with pytest.raises(PermissionError, match="writes are not allowed"):
            exec(output.code, {})  # noqa: S102


class TestPostExecution:
    def test_without_offload_keeps_result(self):
        post = _iteration()["post_execution_hook"]
        result = ["value"]

        assert post(1, "", result, [], [], {}).result is result

    def test_offloads_an_oversized_result(self, tmp_path):
        post = _iteration(offload=Offloader(tmp_path, limit=10))["post_execution_hook"]

        output = post(1, "", "x" * 100, [], [], {})

        assert output.result["offloaded"] is True


class TestPostIteration:
    def test_records_the_iteration(self, session: Session):
        post = _iteration(session=session)["post_iteration_hook"]

        output = post(1, None, "code", "result", [])

        assert output.history == []
        [event] = session.events()
        assert event["kind"] == "iteration"
        assert event["iteration"] == 1
        assert event["code"] == "code"
        assert event["output"] == "result"

    def test_write_back_runs_once(self):
        memory = FakeMemory()
        hooks = _iteration(memory=memory)
        hooks["pre_iteration_hook"](1, [], [], {"question": "hi"})
        post = hooks["post_iteration_hook"]

        post(1, None, "", "", [])
        post(2, None, "", "", [])

        [turn] = memory.turns
        assert turn[0]["role"] == "user"
        assert "question: hi" in turn[0]["content"]

    def test_write_back_can_be_disabled(self):
        memory = FakeMemory()
        hooks = _iteration(memory=memory, write_back=False)
        hooks["pre_iteration_hook"](1, [], [], {"question": "hi"})

        hooks["post_iteration_hook"](1, None, "", "", [])

        assert memory.turns == []

    def test_write_back_failure_is_logged(self, caplog: pytest.LogCaptureFixture):
        memory = FakeMemory()
        memory.fail_write = True
        hooks = _iteration(memory=memory)
        hooks["pre_iteration_hook"](1, [], [], {"question": "hi"})

        with caplog.at_level(logging.WARNING, logger="dspy_agent_tools.wire"):
            hooks["post_iteration_hook"](1, None, "", "", [])

        assert "Memory write-back failed" in caplog.text

    def test_without_inputs_skips_write_back(self):
        memory = FakeMemory()
        post = _iteration(memory=memory)["post_iteration_hook"]

        post(1, None, "", "", [])

        assert memory.turns == []


class TestSpans:
    def test_iteration_and_execution_spans_end(self, monkeypatch: pytest.MonkeyPatch):
        from tests.wire.test_observe import FakeTrace

        fake = FakeTrace()
        monkeypatch.setattr(observe, "_trace", lambda: fake)
        hooks = _iteration()

        hooks["pre_iteration_hook"](1, [], [], {})
        hooks["pre_execution_hook"](1, "x = 1", [], [], {})
        hooks["post_execution_hook"](1, "x = 1", "1", [], [], {})
        hooks["post_iteration_hook"](1, None, "x = 1", "1", [])

        names = [span.name for span in fake.tracer.spans]
        assert names == ["rlm.iteration", "rlm.execute"]
        assert all(span.ended for span in fake.tracer.spans)
        assert fake.tracer.spans[1].attributes["dspy.rlm.result.chars"] == 1

    def test_trace_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch):
        from tests.wire.test_observe import FakeTrace

        fake = FakeTrace()
        monkeypatch.setattr(observe, "_trace", lambda: fake)
        hooks = _iteration(trace=False)

        hooks["pre_iteration_hook"](1, [], [], {})
        hooks["pre_execution_hook"](1, "", [], [], {})
        hooks["post_execution_hook"](1, "", "", [], [], {})
        hooks["post_iteration_hook"](1, None, "", "", [])

        assert fake.tracer.spans == []


class TestAvailability:
    def test_missing_hooks_package_explains_the_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setitem(sys.modules, "dspy_rlm_hooks", None)

        with pytest.raises(ImportError, match=r"dspy-agent-tools\[hooks\]"):
            hook_module.build_hooks()
