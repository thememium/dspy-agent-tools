"""Behavioural tests for Durable, Human, Run, and worker tools."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import dspy
import pytest
from dbos import DBOS

from dspy_agent_tools import (
    ApprovalRequest,
    Durable,
    EventType,
    Human,
    QuestionRequest,
    Run,
    Worker,
)
from dspy_agent_tools.durable import human as human_module
from dspy_agent_tools.durable.exceptions import HumanInputRequired

from .conftest import unique_name
from .helpers import KeyedLM, wait_for_pending


def _react(signature: str, tools: list[object]) -> Any:
    # DSPy's runtime accepts signature strings; its annotation only advertises
    # Signature classes.
    return dspy.ReAct(signature, tools=tools)  # ty: ignore[invalid-argument-type]


REACT_ANSWERS = {
    "[[ ## answer ## ]]": {"reasoning": "looked it up", "answer": "Sunny in Paris"},
    "observation_1": {
        "next_thought": "done",
        "next_tool_name": "finish",
        "next_tool_args": {},
    },
    "observation_0": {
        "next_thought": "done",
        "next_tool_name": "finish",
        "next_tool_args": {},
    },
    "next_tool_name": {
        "next_thought": "weather",
        "next_tool_name": "get_weather",
        "next_tool_args": {"city": "Paris"},
    },
}

WRITE_ANSWERS = {
    "[[ ## answer ## ]]": {"reasoning": "wrote", "answer": "written"},
    "observation_0": {
        "next_thought": "done",
        "next_tool_name": "finish",
        "next_tool_args": {},
    },
    "next_tool_name": {
        "next_thought": "write",
        "next_tool_name": "write_file",
        "next_tool_args": {"path": "x.txt"},
    },
}

DIRECT_ANSWERS = {
    "[[ ## answer ## ]]": {"reasoning": "direct", "answer": "Sunny in Paris"},
    "next_tool_name": {
        "next_thought": "done",
        "next_tool_name": "finish",
        "next_tool_args": {},
    },
}

ASK_ANSWERS = {
    "[[ ## answer ## ]]": {"reasoning": "asked", "answer": "used develop"},
    "observation_1": {
        "next_thought": "done",
        "next_tool_name": "finish",
        "next_tool_args": {},
    },
    "observation_0": {
        "next_thought": "done",
        "next_tool_name": "finish",
        "next_tool_args": {},
    },
    "next_tool_name": {
        "next_thought": "ask",
        "next_tool_name": "ask_human",
        "next_tool_args": {
            "question": "Which branch?",
            "choices": ["main", "develop"],
        },
    },
}


def test_tool_wrapper_preserves_metadata_and_runs_outside_workflow():
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    wrapped = Durable.tool(add)
    assert wrapped.__name__ == "add"
    assert wrapped.__doc__ == "Add two numbers."
    assert list(inspect.signature(wrapped).parameters) == ["a", "b"]
    assert wrapped(2, 3) == 5


def test_async_tool_outside_workflow():
    async def double(value: int) -> int:
        return value * 2

    wrapped = Durable.tool(double)
    assert wrapped(4) == 8


def test_react_agent_checkpoints_llm_and_tool_calls():
    calls: list[str] = []

    def get_weather(city: str) -> str:
        """Get the weather for a city."""
        calls.append(city)
        return "sunny"

    lm = KeyedLM(REACT_ANSWERS)
    dspy.configure(lm=lm)
    module = _react("question -> answer", tools=[get_weather])
    agent = Durable(module, name=unique_name("react"))
    run = agent.start(question="What is the weather?")

    assert run.result().answer == "Sunny in Paris"
    assert calls == ["Paris"]
    assert lm.calls == 3  # two ReAct turns and the final extract

    events = list(run.events())
    types = [event.type for event in events]
    assert types[0] == EventType.RUN_STARTED
    assert types[-1] == EventType.RUN_COMPLETED
    assert events[0].data["input"]["question"] == "What is the weather?"
    assert events[-1].data["output"]["answer"] == "Sunny in Paris"
    assert EventType.TOOL_COMPLETED in types
    assert [
        event.data["tool"] for event in events if event.type == EventType.TOOL_STARTED
    ] == ["get_weather"]

    tool_started = next(
        event for event in events if event.type == EventType.TOOL_STARTED
    )
    assert tool_started.data["input"] == {
        "args": [],
        "kwargs": {"city": "Paris"},
    }
    tool_completed = next(
        event for event in events if event.type == EventType.TOOL_COMPLETED
    )
    assert tool_completed.data["output"] == "sunny"

    llm_completed = next(
        event for event in events if event.type == EventType.LLM_COMPLETED
    )
    assert llm_completed.data["output"]["next_tool_name"] == "get_weather"
    assert llm_completed.data["llm"]

    steps = run.steps()
    assert not any(
        step["function_name"] in {"DBOS.writeStream", "DBOS.setEvent"} for step in steps
    )
    assert DBOS.get_event(run.id, "agent.input", timeout_seconds=0) == {
        "question": "What is the weather?"
    }
    assert DBOS.get_event(run.id, "agent.output", timeout_seconds=0)["answer"] == (
        "Sunny in Paris"
    )
    tool_step = next(step for step in steps if step["function_name"] == "get_weather")
    assert tool_step["output"]["input"] == {
        "args": [],
        "kwargs": {"city": "Paris"},
    }
    assert tool_step["output"]["output"] == "sunny"

    llm_steps = [
        step for step in steps if step["function_name"].startswith(f"{agent.name}:llm:")
    ]
    assert len(llm_steps) == 3
    first_llm = llm_steps[0]["output"]
    assert first_llm["predictor"] == "react"
    assert first_llm["output_type"] == "prediction"
    assert first_llm["input"]["kwargs"]["question"] == "What is the weather?"
    assert first_llm["output"]["next_tool_name"] == "get_weather"
    assert first_llm["llm"]
    # The DBOS viewer renders this envelope as plain data, not a Python pickle.
    json.dumps(first_llm)


def test_agent_call_is_synchronous():
    lm = KeyedLM(DIRECT_ANSWERS)
    dspy.configure(lm=lm)
    module = _react("question -> answer", tools=[])
    agent = Durable(module, name=unique_name("sync"))
    assert agent(question="?").answer == "Sunny in Paris"


def test_agent_acall():
    lm = KeyedLM(DIRECT_ANSWERS)
    dspy.configure(lm=lm)
    module = _react("question -> answer", tools=[])
    agent = Durable(module, name=unique_name("async"))
    assert asyncio.run(agent.acall(question="?")).answer == "Sunny in Paris"


def test_human_approval_is_persisted_and_resumes():
    calls: list[str] = []

    def write_file(path: str) -> str:
        """Write a file."""
        calls.append(path)
        return "ok"

    lm = KeyedLM(WRITE_ANSWERS)
    dspy.configure(lm=lm)
    module = _react("question -> answer", tools=[Human(write_file)])
    agent = Durable(module, name=unique_name("approve"))
    run = agent.start(question="write?")

    pending = wait_for_pending(run)
    assert len(pending) == 1
    request = pending[0]
    assert isinstance(request, ApprovalRequest)
    assert request.tool == "write_file"
    assert request.arguments == {"path": "x.txt"}
    assert request.id in [item.id for item in Human.pending()]

    run.approve(request.id)
    assert run.result().answer == "written"
    assert calls == ["x.txt"]


def test_rejected_tool_becomes_an_observation():
    calls: list[str] = []

    def write_file(path: str) -> str:
        """Write a file."""
        calls.append(path)
        return "ok"

    lm = KeyedLM(WRITE_ANSWERS)
    dspy.configure(lm=lm)
    module = _react("question -> answer", tools=[Human(write_file)])
    agent = Durable(module, name=unique_name("reject"))
    run = agent.start(question="write?")

    request = wait_for_pending(run)[0]
    run.reject(request.id, reason="not allowed")
    run.result()

    assert calls == []
    rejections = [
        event for event in run.events() if event.type == EventType.APPROVAL_RESOLVED
    ]
    assert rejections[0].data["response"] == {
        "approved": False,
        "reason": "not allowed",
    }


def test_human_question_is_answered_from_another_client():
    lm = KeyedLM(ASK_ANSWERS)
    dspy.configure(lm=lm)
    module = _react("task -> answer", tools=[Human.ask])
    agent = Durable(module, name=unique_name("ask"))
    run = agent.start(task="do it")

    request = wait_for_pending(run)[0]
    assert isinstance(request, QuestionRequest)
    assert request.question == "Which branch?"
    assert request.choices == ["main", "develop"]

    run.respond(request.id, "develop")
    assert run.result().answer == "used develop"


def test_interactive_ask_and_approve(monkeypatch):
    monkeypatch.setattr(human_module, "_can_prompt", lambda: True)
    answers = iter(["develop", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert Human.ask("Which branch?", choices=["main", "develop"]) == "develop"
    assert Human.approve("Proceed?") is True


def test_interactive_rejection(monkeypatch):
    monkeypatch.setattr(human_module, "_can_prompt", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert Human.approve("Proceed?") is False


def test_human_requires_terminal_when_not_interactive(monkeypatch):
    monkeypatch.setattr(human_module, "_can_prompt", lambda: False)
    with pytest.raises(HumanInputRequired, match="human input required"):
        Human.ask("Which branch?")


def test_cancel_stops_a_waiting_run():
    lm = KeyedLM(WRITE_ANSWERS)
    dspy.configure(lm=lm)

    def write_file(path: str) -> str:
        return "ok"

    module = _react("question -> answer", tools=[Human(write_file)])
    agent = Durable(module, name=unique_name("cancel"))
    run = agent.start(question="write?")
    wait_for_pending(run)

    run.cancel()
    with pytest.raises(Exception):  # noqa: B017 - DBOS reports cancellation
        run.result()
    assert run.status() == "CANCELLED"


def test_operation_id_is_stable_inside_a_step():
    seen: list[str] = []

    def identify() -> str:
        """Record the durable operation ID."""
        seen.append(Durable.operation_id())
        return "ok"

    lm = KeyedLM(REACT_ANSWERS)
    dspy.configure(lm=lm)
    module = _react("question -> answer", tools=[identify])
    # Make the model call `identify` instead of `get_weather`.
    lm.answers = {
        "[[ ## answer ## ]]": {"reasoning": "r", "answer": "done"},
        "observation_0": {
            "next_thought": "done",
            "next_tool_name": "finish",
            "next_tool_args": {},
        },
        "next_tool_name": {
            "next_thought": "identify",
            "next_tool_name": "identify",
            "next_tool_args": {},
        },
    }
    agent = Durable(module, name=unique_name("operation"))
    run = agent.start(question="?")
    run.result()
    assert len(seen) == 1
    assert seen[0].startswith(run.id)


def test_worker_runs_and_exposes_tools():
    @Durable.worker(name=unique_name("worker"))
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    assert add(a=2, b=3) == 5

    worker = Worker(add)
    assert worker(a=2, b=3) == 5
    tools = Durable.worker_tools([worker])
    assert {tool.name for tool in tools} >= {
        "list_workers",
        "start_worker",
        "worker_status",
        "worker_result",
    }
    listed = next(tool for tool in tools if tool.name == "list_workers")
    assert listed() == [worker.name]


def test_worker_tools_reject_unknown_worker():
    @Durable.worker(name=unique_name("allowed"))
    def allowed() -> str:
        """Allowed worker."""
        return "ok"

    tools = Durable.worker_tools([Worker(allowed)])
    start = next(tool for tool in tools if tool.name == "start_worker")
    with pytest.raises(Exception, match="not in the allowlist"):
        start(name="other", inputs={})


def test_run_get_reconnects():
    lm = KeyedLM(DIRECT_ANSWERS)
    dspy.configure(lm=lm)
    module = _react("question -> answer", tools=[])
    agent = Durable(module, name=unique_name("reconnect"))
    run = agent.start(question="?")
    reconnect = Run.get(run.id)
    assert reconnect.result().answer == "Sunny in Paris"
    assert reconnect.status() == "SUCCESS"
    assert reconnect.steps()


def test_schedule_lifecycle():
    @Durable.worker(name=unique_name("tick"))
    def tick() -> str:
        """Tick."""
        return "ok"

    schedule = tick.schedule(cron="0 0 0 1 1 *", name=unique_name("schedule"))
    info = schedule.info()
    assert info["schedule_name"] == schedule.name
    assert schedule.trigger().result() == "ok"
    schedule.pause()
    assert schedule.info()["status"] == "PAUSED"
    schedule.resume()
    schedule.delete()
    with pytest.raises(Exception, match="no schedule"):
        schedule.info()


def test_conditional_tool_approval_outside_workflow(monkeypatch):
    monkeypatch.setattr(human_module, "_can_prompt", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    calls: list[str] = []

    def deploy(environment: str) -> str:
        """Deploy."""
        calls.append(environment)
        return f"deployed {environment}"

    guarded = Durable.tool(deploy, approval=lambda environment: environment == "prod")
    assert guarded(environment="dev") == "deployed dev"
    assert guarded(environment="prod")["status"] == "rejected"
    assert calls == ["dev"]


def test_human_tools_wraps_a_collection():
    def first() -> str:
        return "first"

    def second() -> str:
        return "second"

    wrapped = Human.tools([first, second], when=lambda: False)
    assert [item() for item in wrapped] == ["first", "second"]
    assert all(item.__name__ in {"first", "second"} for item in wrapped)
