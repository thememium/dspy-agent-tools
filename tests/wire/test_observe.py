"""Tests for dspy_agent_tools.wire.observe."""

from __future__ import annotations

from typing import Any

import pytest

from dspy_agent_tools.wire import observe


class FakeSpan:
    def __init__(self, name: str):
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.errors: list[BaseException] = []
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, exception: BaseException) -> None:
        self.errors.append(exception)

    def set_status(self, status: Any) -> None:
        self.status = status

    def end(self) -> None:
        self.ended = True


class FakeTracer:
    def __init__(self):
        self.spans: list[FakeSpan] = []
        self.scope: str | None = None

    def start_span(self, name: str) -> FakeSpan:
        span = FakeSpan(name)
        self.spans.append(span)
        return span


class FakeTrace:
    def __init__(self):
        self.tracer = FakeTracer()

    def get_tracer(self, scope: str) -> FakeTracer:
        self.tracer.scope = scope
        return self.tracer


@pytest.fixture
def fake_trace(monkeypatch: pytest.MonkeyPatch) -> FakeTrace:
    fake = FakeTrace()
    monkeypatch.setattr(observe, "_trace", lambda: fake)
    return fake


class TestWithoutOpenTelemetry:
    def test_span_is_a_noop(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(observe, "_trace", lambda: None)

        with observe.span("x", {"a": 1}) as current:
            current.set_attribute("b", 2)
            current.record_exception(ValueError("ignored"))

        assert current is observe.NOOP

    def test_start_span_is_a_noop(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(observe, "_trace", lambda: None)

        current = observe.start_span("x")

        current.set_status(None)
        current.end()
        assert current is observe.NOOP


def test_trace_returns_the_installed_api():
    assert observe._trace() is not None


class TestWithOpenTelemetry:
    def test_span_sets_attributes_and_ends(self, fake_trace: FakeTrace):
        with observe.span("execute_tool", {"gen_ai.tool.name": "bash"}) as current:
            current.set_attribute("extra", 1)

        [span] = fake_trace.tracer.spans
        assert span.name == "execute_tool"
        assert span.attributes == {"gen_ai.tool.name": "bash", "extra": 1}
        assert span.ended
        assert fake_trace.tracer.scope == observe.SCOPE

    def test_span_records_exception_and_reraises(self, fake_trace: FakeTrace):
        with (
            pytest.raises(RuntimeError),
            observe.span("execute_tool"),
        ):
            raise RuntimeError("boom")

        [span] = fake_trace.tracer.spans
        assert isinstance(span.errors[0], RuntimeError)
        assert span.ended

    def test_start_span_is_ended_by_the_caller(self, fake_trace: FakeTrace):
        current = observe.start_span("rlm.iteration", {"dspy.rlm.iteration": 1})

        assert not current.ended
        current.set_attribute("dspy.rlm.code.chars", 10)
        current.end()

        assert current.ended
        assert fake_trace.tracer.spans[0].attributes == {
            "dspy.rlm.iteration": 1,
            "dspy.rlm.code.chars": 10,
        }
