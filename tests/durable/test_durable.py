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
    DurableFunction,
    EventType,
    Human,
    Patch,
    QuestionRequest,
    Run,
    Toolbox,
    Worker,
)
from dspy_agent_tools.durable import human as human_module
from dspy_agent_tools.durable.exceptions import DurableError, HumanInputRequired

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


def test_rlm_tools_are_instrumented_in_place():
    def lookup(query: str) -> str:
        """Look up a query."""
        return query

    module = dspy.RLM("question -> answer", tools=[lookup])
    original = module._user_tools["lookup"]
    Durable(module, name=unique_name("rlm"))
    wrapped = module._user_tools["lookup"]
    assert wrapped is not original
    assert wrapped.func.__durable_tool__ is True
    assert module.tools["lookup"] is wrapped


def test_rlm_prepare_forwards_newer_signature():
    calls: list[bool] = []

    def prepare(budget: Any = None, typed: bool = True) -> dict[str, Any]:
        calls.append(typed)
        return {
            "llm_query": lambda prompt: prompt,
            "llm_query_batched": lambda prompts: prompts,
        }

    module = dspy.RLM("question -> answer", tools=[])
    vars(module)["_prepare_execution_tools"] = prepare
    Durable(module, name=unique_name("rlm-prepare"))
    tools = module._prepare_execution_tools(None, False)
    assert calls == [False]
    assert tools["llm_query"].__durable_tool__ is True


def test_rlm_batched_queries_checkpoint_each_prompt():
    class FakeRLM(dspy.Module):
        def __init__(self) -> None:
            super().__init__()

        def _prepare_execution_tools(self, budget: Any = None) -> dict[str, Any]:
            return {
                "llm_query": lambda prompt: f"echo:{prompt}",
                "llm_query_batched": lambda prompts: list(prompts),
            }

    fake = FakeRLM()
    Durable(fake, name=unique_name("batched-rlm"))
    assert fake._prepare_execution_tools()["llm_query_batched"](["outside"]) == [
        "outside"
    ]

    @Durable.workflow(name=unique_name("batched-run"))
    def main() -> list[str]:
        tools = fake._prepare_execution_tools()
        return tools["llm_query_batched"](["one", "two"])

    run = main.start()
    assert run.result() == ["one", "two"]
    names = [step["function_name"] for step in run.steps()]
    assert any(name.endswith("llm_query_batched:0") for name in names)
    assert any(name.endswith("llm_query_batched:1") for name in names)


def test_patch_accepts_durable_rlm_and_reinstruments():
    def lookup(query: str) -> str:
        """Look up a query."""
        return query

    agent = Durable(
        dspy.RLM("question -> answer", tools=[]),
        name=unique_name("patched-rlm"),
    )
    returned = Patch(agent, tools=lookup)
    assert returned is agent
    assert "lookup" in agent.module.tools
    assert agent.module.tools["lookup"].func.__durable_tool__ is True
    assert getattr(agent.module.generate_action, "__durable_predictor__", False)


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


def test_start_and_enqueue_control_options():
    workflow_id = unique_name("explicit-run")

    @Durable.workflow(name=unique_name("control"), version="v2")
    def echo(*, value: int) -> int:
        return value

    assert echo.version == "v2"
    run = echo.start(workflow_id=workflow_id, timeout=5, value=1)
    assert run.id == workflow_id
    assert run.result() == 1

    @Durable.worker(name=unique_name("delayed"), queue=unique_name("delay-queue"))
    def delayed(*, value: int) -> int:
        return value

    queued = delayed.enqueue(
        workflow_id=unique_name("delayed-run"),
        delay=60,
        timeout=5,
        value=2,
    )
    assert queued.status() == "DELAYED"
    queued.cancel()

    dspy.configure(lm=KeyedLM(DIRECT_ANSWERS))
    agent = Durable(
        dspy.Predict("question -> answer"),
        name=unique_name("queued-agent"),
        queue=unique_name("agent-queue"),
    )
    assert (
        agent.start(
            workflow_id=unique_name("agent-start"),
            timeout=5,
            question="?",
        )
        .result()
        .answer
        == "Sunny in Paris"
    )
    agent_run = agent.enqueue(
        delay=60,
        timeout=5,
        workflow_id=unique_name("queued-agent-run"),
        question="?",
    )
    assert agent_run.status() == "DELAYED"
    agent_run.cancel()


def test_pause_and_resume_at_boundary():
    import time

    @Durable.workflow(name=unique_name("pausable"))
    def main() -> str:
        Durable.sleep(0.2)
        Durable.pause_point()
        return "ok"

    run = main.start()
    run.pause()
    time.sleep(0.4)
    assert run.is_active()
    run.resume()
    assert run.result(timeout=10) == "ok"
    types = [event.type for event in run.events()]
    assert EventType.RUN_PAUSED in types
    assert EventType.RUN_RESUMED in types


def test_run_timeout_and_list_runs():
    timeout_name = unique_name("timeout")

    @Durable.workflow(name=timeout_name)
    def blocked() -> str:
        return Human.ask("blocked?")

    run = blocked.start()
    request = wait_for_pending(run)[0]
    with pytest.raises(TimeoutError, match="did not finish"):
        run.result(timeout=0.01)
    run.respond(request.id, "done")
    assert run.result() == "done"

    runs = Durable.list_runs(name=f"dspy-agent:fn:{timeout_name}", limit=10)
    assert any(item.id == run.id for item in runs)


def test_async_predictor_is_checkpointed_in_async_workflow():
    dspy.configure(lm=KeyedLM(DIRECT_ANSWERS))
    module = dspy.Predict("question -> answer")
    agent = Durable(module, name=unique_name("async-predictor"))
    name = unique_name("async-workflow")

    @Durable.workflow(name=name)
    async def main(*, question: str) -> Any:
        return await module.acall(question=question)

    run = main.start(question="?")
    assert run.result().answer == "Sunny in Paris"
    names = [step["function_name"] for step in run.steps()]
    assert any(item.startswith(f"{agent.name}:llm:") for item in names)


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


def test_agent_factory_builds_a_fresh_module_per_run():
    calls: list[int] = []

    @Durable.agent(name=unique_name("factory"), version="v1")
    def make_agent():
        calls.append(1)
        return dspy.Predict("question -> answer")

    assert len(calls) == 1
    assert make_agent.version == "v1"
    dspy.configure(lm=KeyedLM(DIRECT_ANSWERS))
    assert make_agent(question="?").answer == "Sunny in Paris"
    assert len(calls) == 2


def test_agent_factory_validation_and_recovery_list():
    from dspy_agent_tools import DurableAgent

    with pytest.raises(TypeError, match="requires"):
        DurableAgent()
    with pytest.raises(TypeError, match="either"):
        DurableAgent(
            dspy.Predict("question -> answer"),
            factory=lambda: dspy.Predict("question -> answer"),
        )
    with pytest.raises(TypeError, match="expected a dspy.Module"):
        Durable.agent(name=unique_name("bad-factory"))(lambda: 42)

    recovered: list[int] = []
    callback = lambda: recovered.append(1)

    @Durable.agent(name=unique_name("factory-recover"), recover=[callback])
    def make_agent():
        return dspy.Predict("question -> answer")

    dspy.configure(lm=KeyedLM(DIRECT_ANSWERS))
    assert make_agent(question="?").answer == "Sunny in Paris"
    assert recovered == [1]


def test_toolbox_dispatches_the_actual_tool():
    calls: list[tuple[int, int]] = []

    def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls.append((a, b))
        return a + b

    toolbox = Toolbox([add], mode="search")
    call_tool = next(tool for tool in toolbox if tool.name == "call_tool")

    class Dispatch(dspy.Module):
        def __init__(self) -> None:
            super().__init__()
            self.tools = {"call_tool": call_tool}

        def forward(self, question: str) -> int:
            with pytest.raises(ValueError, match="Unknown tool"):
                self.tools["call_tool"](name="missing")
            return self.tools["call_tool"](name="ADD", arguments={"a": 2, "b": 3})

    agent = Durable(Dispatch(), name=unique_name("toolbox"))
    run = agent.start(question="?")
    assert run.result() == 5
    assert calls == [(2, 3)]
    names = [step["function_name"] for step in run.steps()]
    assert "add" in names
    assert "call_tool" not in names


def test_agent_factory_can_fail_after_registration():
    calls: list[int] = []

    def factory():
        calls.append(1)
        return dspy.Predict("question -> answer") if len(calls) == 1 else 42

    agent = Durable.agent(name=unique_name("bad-later"))(factory)
    with pytest.raises(DurableError, match="factory returned"):
        agent(question="?")


def test_rlm_prepare_skips_missing_and_durable_helpers():
    durable = Durable.tool(lambda prompt: prompt)

    class FakeRLM(dspy.Module):
        def _prepare_execution_tools(self, budget: Any = None) -> dict[str, Any]:
            return {"llm_query": durable, "llm_query_batched": None}

    fake = FakeRLM()
    Durable(fake, name=unique_name("prepare-skip"))
    tools = fake._prepare_execution_tools()
    assert tools == {"llm_query": durable, "llm_query_batched": None}


def test_toolbox_dispatch_preserves_wrapped_targets():
    def human_target() -> str:
        """Human target."""
        return "human"

    def durable_target() -> str:
        """Durable target."""
        return "durable"

    toolbox = Toolbox(
        [
            Human(human_target, when=lambda: False),
            Durable.tool(durable_target),
        ],
        mode="search",
    )
    call_tool = next(tool for tool in toolbox if tool.name == "call_tool")

    class Dispatch(dspy.Module):
        def __init__(self) -> None:
            super().__init__()
            self.tools = {"call_tool": call_tool}

        def forward(self, question: str) -> list[str]:
            return [
                self.tools["call_tool"](name="human_target", arguments={}),
                self.tools["call_tool"](name="durable_target", arguments={}),
            ]

    agent = Durable(Dispatch(), name=unique_name("toolbox-wrapped"))
    assert agent(question="?") == ["human", "durable"]


def test_tool_retry_classifier_controls_attempts():
    attempts: list[int] = []

    @Durable.tool(retries=2, retry_on=lambda exc: isinstance(exc, ValueError))
    def flaky() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("retry me")
        return "ok"

    @Durable.workflow(name=unique_name("retry-classifier"))
    def main() -> str:
        return flaky()

    assert main() == "ok"
    assert len(attempts) == 2


def test_tool_recovery_hook_runs_at_workflow_start():
    recovered: list[int] = []

    @Durable.tool(recover=lambda: recovered.append(1))
    def lookup() -> str:
        """Look up a value."""
        return "ok"

    class Module(dspy.Module):
        def __init__(self) -> None:
            super().__init__()
            self.tools = {"lookup": dspy.Tool(lookup)}

        def forward(self, question: str) -> str:
            return self.tools["lookup"]()

    agent = Durable(Module(), name=unique_name("recovery-hook"))
    assert agent(question="?") == "ok"
    assert recovered == [1]


def test_worker_tools_start_child_workflow_from_agent():
    @Durable.worker(name=unique_name("child"), queue=unique_name("child-queue"))
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    tools = {tool.name: tool for tool in Durable.worker_tools([Worker(add)])}
    start = tools["start_worker"]
    assert getattr(start.func, "__durable_workflow_tool__", False) is True

    class Orchestrator(dspy.Module):
        def __init__(self) -> None:
            super().__init__()
            self.tools = {"start_worker": start}

        def forward(self, question: str) -> str:
            return self.tools["start_worker"](name=add.name, inputs={"a": 2, "b": 3})

    agent = Durable(Orchestrator(), name=unique_name("orchestrator"))
    run = agent.start(question="?")
    child_id = run.result()
    assert not any(step["function_name"] == "start_worker" for step in run.steps())
    assert Run.get(child_id).result() == 5


def test_sleep_rejects_step_context():
    @Durable.step()
    def sleeper() -> None:
        Durable.sleep(0.001)

    @Durable.workflow(name=unique_name("sleep-step"))
    def main() -> None:
        sleeper()

    with pytest.raises(DurableError, match="workflow level"):
        main()


def test_workflow_and_step_decorators():
    @Durable.step()
    def increment(value: int) -> int:
        """Increment a value."""
        return value + 1

    assert increment(value=1) == 2  # outside a workflow: plain call

    @Durable.workflow(name=unique_name("decorated"))
    def main(*, value: int) -> int:
        return increment(value=value) * 2

    assert isinstance(main, DurableFunction)
    assert main(value=1) == 4

    run = main.start(value=2)
    assert run.result() == 6
    step = next(s for s in run.steps() if s["function_name"] == "increment")
    assert step["output"]["input"]["kwargs"] == {"value": 2}
    assert step["output"]["output"] == 3

    with pytest.raises(DurableError, match="already exists"):
        Durable.workflow(lambda: None, name=main.name)


def test_workflow_and_step_decorators_without_parentheses():
    @Durable.step
    def double(value: int) -> int:
        return value * 2

    @Durable.workflow
    def bare(*, value: int) -> int:
        return double(value=value)

    assert bare(value=3) == 6


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


def test_tool_keyword_names_do_not_collide_with_wrapper_options():
    @Durable.tool
    def identify(name: str, retries: int, retry_on: str) -> tuple[str, int, str]:
        return name, retries, retry_on

    assert identify(name="Ada", retries=2, retry_on="failure") == ("Ada", 2, "failure")

    @Durable.workflow(name=unique_name("tool-keywords"))
    def workflow() -> tuple[str, int, str]:
        return identify(name="Ada", retries=2, retry_on="failure")

    assert workflow() == ("Ada", 2, "failure")


def test_factory_recovery_callbacks_belong_to_each_module():
    callbacks: list[Any] = []

    @Durable.agent(name=unique_name("factory-isolation"))
    def factory():
        def recover() -> None:
            pass

        callbacks.append(recover)

        @Durable.tool(recover=recover)
        def lookup() -> str:
            """Look up a value."""
            return "ok"

        class Module(dspy.Module):
            def __init__(self) -> None:
                super().__init__()
                self.tools = {"lookup": dspy.Tool(lookup)}

        return Module()

    first = factory._module_for_run()
    second = factory._module_for_run()
    assert vars(first)["__durable_recovery_callbacks__"] == [callbacks[-2]]
    assert vars(second)["__durable_recovery_callbacks__"] == [callbacks[-1]]


def test_worker_retries_are_rejected_instead_of_ignored():
    with pytest.raises(DurableError, match="workflow retries are not supported"):
        Durable.worker(lambda: None, name=unique_name("worker-retries"), retries=3)


def test_dspy_tool_keyword_names_do_not_collide():
    def identify(name: str, retries: int, retry_on: str) -> str:
        """Identify an item."""
        return f"{name}:{retries}:{retry_on}"

    wrapped = Durable.tool(dspy.Tool(identify))
    assert wrapped(name="Ada", retries=2, retry_on="failure") == "Ada:2:failure"


def test_unserializable_tool_result_fails_explicitly():
    import threading

    @Durable.tool
    def get_lock() -> Any:
        return threading.Lock()

    @Durable.workflow(name=unique_name("unserializable-output"))
    def workflow() -> Any:
        return get_lock()

    with pytest.raises(Exception, match="cannot serialize"):
        workflow()
