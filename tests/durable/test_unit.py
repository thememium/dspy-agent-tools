"""Unit-level coverage for durable helpers and error paths."""

from __future__ import annotations

import asyncio
import inspect
import pickle
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM

import dspy_agent_tools.durable.human as human_module
import dspy_agent_tools.durable.runtime as runtime_module
from dspy_agent_tools import (
    ApprovalRequest,
    Durable,
    Event,
    EventType,
    Human,
    QuestionRequest,
    Run,
    Schedule,
    Worker,
)
from dspy_agent_tools.durable import context
from dspy_agent_tools.durable import tool as tool_module
from dspy_agent_tools.durable._dbos import load_dbos
from dspy_agent_tools.durable.events import event_from_payload, event_payload
from dspy_agent_tools.durable.exceptions import (
    AgentExecutionError,
    DuplicateAgentName,
    DurableError,
    DurableExecutionError,
    HumanInputRequired,
    HumanTimeout,
    NotInWorkflow,
)
from dspy_agent_tools.durable.function import DurableFunction, _function_name
from dspy_agent_tools.durable.worker import worker_tools

from .conftest import unique_name
from .helpers import KeyedLM, wait_for_pending

DIRECT_ANSWERS = {
    "[[ ## answer ## ]]": {"reasoning": "direct", "answer": "ok"},
    "next_tool_name": {
        "next_thought": "done",
        "next_tool_name": "finish",
        "next_tool_args": {},
    },
}


def _module() -> dspy.Predict:
    return dspy.Predict("question -> answer")


def _react(signature: str, tools: list[Any]) -> Any:
    return dspy.ReAct(signature, tools=tools)  # ty: ignore[invalid-argument-type]


def test_dbos_loader():
    assert load_dbos().__name__ == "dbos"


class _FakeRun:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def approve(self, request_id: str) -> None:
        self.calls.append(("approve", request_id))

    def reject(self, request_id: str, reason: str | None = None) -> None:
        self.calls.append(("reject", request_id, reason))

    def respond(self, request_id: str, answer: Any) -> None:
        self.calls.append(("respond", request_id, answer))


def test_human_request_actions(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(Run, "get", classmethod(lambda _cls, _id: fake))
    approval = ApprovalRequest(
        {
            "id": "a",
            "run_id": "run",
            "kind": "approval",
            "event": "approval.requested",
            "message": "ok?",
        },
        "run",
        0.0,
    )
    approval.approve()
    approval.reject("no")
    question = QuestionRequest(
        {
            "id": "q",
            "run_id": "run",
            "kind": "question",
            "event": "question.requested",
            "message": "which?",
        },
        "run",
        0.0,
    )
    question.respond("yes")
    assert fake.calls == [
        ("approve", "a"),
        ("reject", "a", "no"),
        ("respond", "q", "yes"),
    ]


def test_event_helpers():
    event = Event(EventType.RUN_STARTED, "run-1", {"value": 1}, 1.5)
    assert event.to_dict() == {
        "type": "run.started",
        "run_id": "run-1",
        "at": 1.5,
        "data": {"value": 1},
    }
    assert event == Event(EventType.RUN_STARTED, "run-1", {"value": 1}, 1.5)
    assert event != Event(EventType.RUN_STARTED, "run-1", {}, 1.5)
    assert event.__eq__(object()) is NotImplemented
    assert "run-1" in repr(event)
    assert event_payload(event)["type"] == "run.started"

    plain = event_from_payload(event.to_dict())
    assert isinstance(plain, Event)
    assert not isinstance(plain, (ApprovalRequest, QuestionRequest))

    approval = ApprovalRequest(
        {
            "id": "r1",
            "run_id": "run-1",
            "kind": "approval",
            "event": "approval.requested",
            "message": "ok?",
            "tool": "bash",
            "arguments": {"command": "ls"},
        },
        "run-1",
        1.0,
    )
    assert approval.id == "r1"
    assert approval.tool == "bash"
    assert approval.arguments == {"command": "ls"}
    assert approval.choices is None
    assert approval.defer() is approval
    assert isinstance(event_from_payload(approval.to_dict()), ApprovalRequest)

    question = QuestionRequest(
        {
            "id": "q1",
            "run_id": "run-1",
            "kind": "question",
            "event": "question.requested",
            "message": "Which?",
            "choices": ["a", "b"],
        },
        "run-1",
        1.0,
    )
    assert question.question == "Which?"
    assert question.choices == ["a", "b"]
    assert isinstance(event_from_payload(question.to_dict()), QuestionRequest)


def test_exceptions_are_portable():
    errors = [
        DurableExecutionError("run-1", "boom"),
        AgentExecutionError("agent", "ValueError", "bad"),
        NotInWorkflow("nope"),
        HumanInputRequired({"kind": "approval", "message": "ok?"}),
        HumanTimeout({"kind": "question", "id": "q"}, 1.0),
        DuplicateAgentName("dupe"),
    ]
    for error in errors:
        restored = pickle.loads(pickle.dumps(error))
        assert str(restored) == str(error)
    restored = pickle.loads(pickle.dumps(errors[1]))
    assert restored.agent == "agent"
    assert restored.error_type == "ValueError"
    assert restored.message == "bad"


def test_context_outside_workflow():
    assert context.in_workflow() is False
    assert context.in_workflow_body() is False
    assert context.current_run_id() is None
    assert context.emit(EventType.RUN_STARTED) is None
    with pytest.raises(NotInWorkflow), context.workflow_state("agent"):
        pass
    with pytest.raises(NotInWorkflow):
        context.state()
    with pytest.raises(NotInWorkflow):
        Durable.operation_id()


def test_runtime_helpers(monkeypatch):
    rt = runtime_module._Runtime()
    assert rt.name == runtime_module.DEFAULT_APP_NAME
    assert rt.resolve_database_url().endswith(".sqlite")
    rt.configure(manage_runtime=False)
    rt.ensure_launched()
    assert rt.launched is False

    conflict = runtime_module._Runtime()
    conflict.register_queue("q", concurrency=1)
    with pytest.raises(DurableError, match="different options"):
        conflict.register_queue("q", concurrency=2)

    live = runtime_module.runtime()
    Durable.launch()
    Durable.launch()
    with pytest.raises(DurableError, match="cannot change"):
        live.configure(name=unique_name("other"))

    created: dict[str, Any] = {}

    class FakeDBOS:
        def __init__(self, *, config: dict[str, Any]) -> None:
            created["config"] = config

        @staticmethod
        def launch() -> None:
            created["launched"] = True

        @classmethod
        def register_queue(cls, name: str, **options: Any) -> None:
            created.setdefault("queues", {})[name] = options

    import dbos

    monkeypatch.setattr(dbos, "DBOS", FakeDBOS)
    fake = runtime_module._Runtime(
        config=runtime_module.RuntimeConfig(
            name="fake-app",
            database_url="sqlite:///fake.sqlite",
            application_version="v1",
            executor_id="exec",
            log_level="WARNING",
        ),
        queues={"research": {"concurrency": 3}},
    )
    fake.launch()
    assert created["launched"] is True
    assert created["config"]["application_version"] == "v1"
    assert created["config"]["executor_id"] == "exec"
    assert created["queues"] == {"research": {"concurrency": 3}}
    assert fake.launched is True


def test_auto_names_and_duplicate_guards():
    first = Durable(_module())
    second = Durable(_module())
    assert first.name != second.name
    assert second.name.endswith("#2")

    with pytest.raises(DurableError, match="already wrapped"):
        Durable(first.module)
    with pytest.raises(DuplicateAgentName):
        Durable(_module(), name=first.name)
    with pytest.raises(TypeError):
        Durable(42)  # ty: ignore[no-matching-overload]
    with pytest.raises(TypeError):
        Durable()


def test_agent_outside_workflow_and_repr():
    dspy.configure(lm=KeyedLM(DIRECT_ANSWERS))
    agent = Durable(_module(), name=unique_name("outside"))
    assert "outside" in repr(agent)
    assert agent._execute(question="?").answer == "ok"

    with pytest.raises(TypeError):
        agent.__class__(42)  # ty: ignore[invalid-argument-type]
    alias = unique_name("alias")
    assert Durable.agent(_module(), name=alias).name == alias
    with_lm = Durable(
        _module(), name=unique_name("explicit-lm"), lm=KeyedLM(DIRECT_ANSWERS)
    )
    assert with_lm(question="?").answer == "ok"


def test_agent_queue_approvals_and_rlm_patch():
    queue = unique_name("queue")

    def guarded() -> str:
        """Guarded tool."""
        return "ok"

    module = _react("question -> answer", tools=[guarded])
    agent = Durable(
        module, name=unique_name("queued"), queue=queue, approvals=[guarded]
    )
    assert queue in runtime_module.runtime().queues
    tool = module.tools["guarded"]
    assert getattr(tool.func, "__human__", None) is not None

    # Re-patching an instrumented module is a no-op.
    agent._patch_predictors()
    agent._patch_tools()

    helper_calls: list[int] = []

    class FakeRLM(dspy.Module):
        def __init__(self) -> None:
            super().__init__()
            self.generate_action = dspy.Predict("question -> answer")

        def forward(self, **kwargs: Any) -> Any:
            return self.generate_action(**kwargs)

        def _prepare_execution_tools(self, budget: Any = None) -> dict[str, Any]:
            helper_calls.append(1)
            return {
                "llm_query": lambda prompt: f"echo:{prompt}",
                "llm_query_batched": lambda prompts: prompts,
            }

    fake = FakeRLM()
    Durable(fake, name=unique_name("rlm"))
    tools = fake._prepare_execution_tools()
    assert tools["llm_query"]("hi") == "echo:hi"
    assert helper_calls == [1]
    assert tool_module.is_durable_tool(tools["llm_query"])
    assert getattr(fake, "__durable_rlm__", False) is True


def test_llm_failure_is_recorded():
    dspy.configure(lm=DummyLM([{"wrong": "x"}]))
    agent = Durable(_module(), name=unique_name("llm-failure"))
    run = agent.start(question="?")
    with pytest.raises(AgentExecutionError):
        run.result()
    assert EventType.LLM_FAILED in [event.type for event in run.events()]


def test_agent_failure_is_recorded():
    class Boom(dspy.Module):
        def __init__(self) -> None:
            super().__init__()
            self.predictor = dspy.Predict("question -> answer")

        def forward(self, **kwargs: Any) -> Any:
            raise ValueError("boom")

    agent = Durable(Boom(), name=unique_name("failing"))
    run = agent.start(question="?")
    with pytest.raises(AgentExecutionError, match="ValueError"):
        run.result()
    types = [event.type for event in run.events()]
    assert EventType.RUN_FAILED in types


def test_agent_enqueue_schedule_and_enqueue_requires_queue():
    dspy.configure(lm=KeyedLM(DIRECT_ANSWERS))
    queue = unique_name("agent-queue")
    agent = Durable(_module(), name=unique_name("enqueued"), queue=queue)
    assert agent.enqueue(question="?").result().answer == "ok"

    no_queue = Durable(_module(), name=unique_name("no-queue"))
    with pytest.raises(DurableError):
        no_queue.enqueue(question="?")

    schedule = agent.schedule(
        cron="0 0 0 1 1 *",
        name=unique_name("agent-schedule"),
        question="?",
    )
    assert schedule.trigger().result().answer == "ok"
    schedule.delete()
    assert (
        Durable.schedule(
            agent,
            cron="0 0 0 1 1 *",
            name=unique_name("agent-schedule-2"),
            question="?",
        )
        .trigger()
        .result()
        .answer
        == "ok"
    )


def test_durable_helpers():
    @Durable.tool(retries=1)
    def decorated(value: int) -> int:
        """Decorated tool."""
        return value

    assert decorated(value=2) == 2
    queued = unique_name("explicit-queue")
    Durable.queue(queued, concurrency=1)
    assert queued in runtime_module.runtime().queues
    direct = Durable.worker(lambda value=1: value, name=unique_name("direct-worker"))
    assert direct(value=2) == 2
    assert isinstance(Durable(lambda value=1: value), DurableFunction)
    assert Durable.tools([decorated])[0](value=3) == 3
    Durable.sleep(0)
    sleeper = DurableFunction(lambda: Durable.sleep(0.001), name=unique_name("sleep"))
    sleeper()
    assert Durable.get_schedule("missing").name == "missing"
    assert Durable.get_run("missing").id == "missing"
    assert Durable.get_schedule("missing").name == "missing"
    with pytest.raises(TypeError):
        Durable(1.5)  # ty: ignore[no-matching-overload]


def test_function_helpers():
    def plain(value: int) -> int:
        return value

    assert _function_name(plain, None).endswith("plain")
    with pytest.raises(TypeError):
        DurableFunction(42)  # ty: ignore[invalid-argument-type]
    first = DurableFunction(plain, name=unique_name("fn"))
    with pytest.raises(DurableError, match="already exists"):
        DurableFunction(plain, name=first.name)
    assert first._execute(value=3) == 3
    assert "fn" in repr(first)

    async def double(value: int) -> int:
        return value * 2

    async_fn = DurableFunction(double, name=unique_name("async-fn"))
    assert async_fn._execute(value=2) == 4
    assert asyncio.run(async_fn.acall(value=3)) == 6

    def fail() -> None:
        raise ValueError("boom")

    failing = DurableFunction(fail, name=unique_name("fail-fn"))
    with pytest.raises(AgentExecutionError, match="ValueError"):
        failing()

    no_queue = DurableFunction(plain, name=unique_name("no-queue-fn"))
    with pytest.raises(DurableError):
        no_queue.enqueue(value=1)

    queued = DurableFunction(
        plain, name=unique_name("queued-fn"), queue=unique_name("fn-queue")
    )
    assert queued.enqueue(value=4).result() == 4


def test_tool_helpers(monkeypatch):
    class Anonymous:
        pass

    assert tool_module.tool_name(Anonymous()).startswith("Anonymous")
    assert tool_module.summarize(None) is None
    assert tool_module.summarize(1) == 1
    assert tool_module.summarize("x" * 600).endswith("...")
    assert tool_module.summarize([1, "x"]) == [1, "x"]
    assert tool_module.summarize({"a": 1}) == {"a": 1}
    assert tool_module.summarize(object()).startswith("<object>")

    async def value() -> int:
        return 1

    async def outer() -> int:
        return tool_module.run_coroutine(value())

    assert asyncio.run(outer()) == 1

    async def broken() -> int:
        raise ValueError("boom")

    async def outer_error() -> None:
        with pytest.raises(ValueError):
            tool_module.run_coroutine(broken())

    asyncio.run(outer_error())

    def add(a: int, b: int) -> int:
        """Add."""
        return a + b

    tool = dspy.Tool(add)
    wrapped = tool_module.wrap_tool(tool)
    assert wrapped.name == "add"
    assert wrapped(a=1, b=2) == 3
    assert tool_module.wrap_tool(wrapped) is wrapped

    human = Human(add)
    assert tool_module.wrap_tool(human) is human

    governed = tool_module.wrap_tool(tool, approval=True)
    assert getattr(governed.func, "__human__", None) is not None
    monkeypatch.setattr(human_module, "_can_prompt", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert governed(a=1, b=2) == 3

    with pytest.raises(TypeError):
        tool_module.wrap_tool(42)


def test_async_tool_and_failing_tool_in_react():
    calls: list[str] = []

    async def fetch(value: str) -> str:
        """Fetch a value."""
        calls.append(value)
        return "fetched"

    def broken() -> str:
        """Always fail."""
        raise RuntimeError("boom")

    answers = {
        "[[ ## answer ## ]]": {"reasoning": "done", "answer": "ok"},
        "observation_1": {
            "next_thought": "done",
            "next_tool_name": "finish",
            "next_tool_args": {},
        },
        "observation_0": {
            "next_thought": "broken",
            "next_tool_name": "broken",
            "next_tool_args": {},
        },
        "next_tool_name": {
            "next_thought": "fetch",
            "next_tool_name": "fetch",
            "next_tool_args": {"value": "x"},
        },
    }
    dspy.configure(lm=KeyedLM(answers))
    agent = Durable(
        _react("question -> answer", tools=[fetch, broken]),
        name=unique_name("async-tool"),
    )
    run = agent.start(question="?")
    assert run.result().answer == "ok"
    assert calls == ["x"]
    types = [event.type for event in run.events()]
    assert EventType.TOOL_FAILED in types


def test_human_prompt_helpers(monkeypatch):
    assert human_module._can_prompt() in {True, False}
    with human_module._interactive(True):
        assert human_module._can_prompt() is True
    with human_module._interactive(False):
        assert human_module._can_prompt() is False

    request = {
        "id": "q",
        "run_id": "r",
        "kind": "question",
        "event": "question.requested",
        "message": "Pick",
        "choices": ["a", "b"],
        "created_at": 0.0,
    }
    assert isinstance(human_module._request_object(request), QuestionRequest)
    with pytest.raises(NotInWorkflow):
        human_module._wait_for_human("question", EventType.QUESTION_REQUESTED, "q")

    answers = iter(["9", "2"])
    monkeypatch.setattr(human_module, "_can_prompt", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert human_module._prompt("Pick", request) == "b"

    monkeypatch.setattr("builtins.input", lambda _prompt: "free")
    assert human_module._prompt("Open", {"choices": None}) == "free"

    class BrokenClient:
        def get_event(self, *_args: Any, **_kwargs: Any) -> Any:
            raise ValueError("missing")

    monkeypatch.setattr(
        human_module, "runtime", lambda: SimpleNamespace(client=lambda: BrokenClient())
    )
    assert human_module._pending("missing") == []

    monkeypatch.setattr(human_module, "_can_prompt", lambda: False)
    with pytest.raises(HumanInputRequired):
        human_module._confirm("ok?", request)
    with pytest.raises(HumanInputRequired):
        human_module._prompt("Open", {"choices": None})


def test_human_tool_and_predicate():
    def add(a: int) -> int:
        """Add."""
        return a + 1

    tool = Human(dspy.Tool(add))
    assert isinstance(tool, dspy.Tool)
    assert tool.name == "add"
    with pytest.raises(TypeError):
        Human(None)
    with pytest.raises(TypeError):
        Human(42)  # ty: ignore[no-matching-overload]

    assert human_module._matches(lambda value: value > 0, add, (1,), {}) is True

    class NoSignature:
        def __call__(self) -> bool:
            return True

        @property
        def __signature__(self) -> Any:
            raise TypeError("no signature")

    assert human_module._matches(lambda: True, NoSignature(), (), {}) is True


def test_human_in_workflow_and_timeout():
    def ask() -> str:
        return Human.ask("Which branch?", choices=["main", "develop"])

    asker = DurableFunction(ask, name=unique_name("ask-fn"))
    run = asker.start()
    request = wait_for_pending(run)[0]
    assert [item.id for item in Human.pending(run.id)] == [request.id]
    run.respond(request.id, "develop")
    assert run.result() == "develop"

    def approve() -> bool:
        return Human.approve("Proceed?")

    approver = DurableFunction(approve, name=unique_name("approve-fn"))
    run = approver.start()
    request = wait_for_pending(run)[0]
    run.approve(request.id)
    assert run.result() is True

    def timeout() -> str:
        return Human.ask("Anyone?", timeout=0.01)

    timed = DurableFunction(timeout, name=unique_name("timeout-fn"))
    with pytest.raises(AgentExecutionError, match="HumanTimeout"):
        timed()


def test_run_control_helpers(monkeypatch):
    assert "Run" in repr(Run("x"))
    assert Run.get("x").id == "x"

    class Missing:
        def get_status(self) -> Any:
            raise ValueError("missing")

        def get_result(self) -> Any:
            raise TypeError("unpicklable")

    class Fake:
        def retrieve_workflow(self, _run_id: str) -> Missing:
            return Missing()

        def get_event(self, *_args: Any, **_kwargs: Any) -> Any:
            raise ValueError("missing")

    monkeypatch.setattr(Run, "_client", lambda self: Fake())
    with pytest.raises(DurableExecutionError):
        Run("x").status()
    with pytest.raises(DurableExecutionError):
        Run("x").result()

    class DurableFailure:
        def get_result(self) -> Any:
            raise DurableExecutionError("x", "boom")

    class DurableFake:
        def retrieve_workflow(self, _run_id: str) -> DurableFailure:
            return DurableFailure()

    monkeypatch.setattr(Run, "_client", lambda self: DurableFake())
    with pytest.raises(DurableExecutionError):
        Run("x").result()
    assert Run("x").pending() == []

    class CancelFake(Fake):
        def cancel_workflow(self, _run_id: str) -> None:
            return None

        def send(self, *_args: Any, **_kwargs: Any) -> None:
            raise ValueError("cannot send")

    monkeypatch.setattr(Run, "_client", lambda self: CancelFake())
    monkeypatch.setattr(Run, "pending", lambda self: [SimpleNamespace(id="request")])
    Run("x").cancel()

    class ResumeFake(Fake):
        def resume_workflow(self, _run_id: str) -> None:
            return None

    monkeypatch.setattr(Run, "_client", lambda self: ResumeFake())
    assert Run("x").resume().id == "x"


def test_run_async_events():
    dspy.configure(lm=KeyedLM(DIRECT_ANSWERS))
    agent = Durable(_module(), name=unique_name("events"))
    run = agent.start(question="?")
    run.result()

    async def consume() -> list[Any]:
        return [event async for event in run.aevents()]

    assert asyncio.run(consume())
    assert list(run)


def test_schedule_repr():
    assert "nightly" in repr(Schedule("nightly"))
    assert Schedule.get("nightly").name == "nightly"


def test_worker_variants_and_tools(monkeypatch):
    dspy.configure(lm=KeyedLM(DIRECT_ANSWERS))
    agent = Durable(_module(), name=unique_name("worker-agent"))
    worker = Worker(agent)
    assert "question" in inspect.signature(worker).parameters

    def plain(value: int) -> int:
        return value

    function_worker = Worker(plain)
    assert function_worker.name.endswith("plain")
    with pytest.raises(TypeError):
        Worker(42)

    @Durable.worker(name=unique_name("echo"), queue=unique_name("echo-queue"))
    def echo(value: int) -> int:
        """Echo."""
        return value

    worker = Worker(echo)
    tools = {tool.name: tool for tool in worker_tools([worker])}
    assert "schedule_worker" in tools
    assert tools["list_workers"]() == [worker.name]
    run_id = tools["start_worker"](name=worker.name, inputs={"value": 5})
    assert tools["worker_result"](run_id=run_id) == 5
    assert tools["worker_status"](run_id=run_id) == "SUCCESS"
    assert tools["wait_for_workers"](run_ids=[run_id]) == {run_id: 5}
    assert "cancelled" in tools["cancel_worker"](run_id=run_id)
    schedule_name = tools["schedule_worker"](
        name=worker.name, cron="0 0 0 1 1 *", inputs={"value": 1}
    )
    assert any(
        row["schedule_name"] == schedule_name for row in tools["list_schedules"]()
    )
    Durable.get_schedule(schedule_name).delete()

    limited = worker_tools([worker], schedules=False)
    assert {tool.name for tool in limited} == {
        "list_workers",
        "start_worker",
        "worker_status",
        "worker_result",
        "cancel_worker",
        "wait_for_workers",
    }
    with pytest.raises(DurableError):
        worker_tools([])
