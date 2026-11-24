"""Expose durable workers to Python and to an LLM."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Iterable
from typing import Any, Protocol, cast

from .agent import DurableAgent
from .exceptions import DurableError
from .function import DurableFunction
from .run import Run
from .schedule import Schedule


class WorkerHandle(Protocol):
    """A callable worker plus the control surface it exposes."""

    name: str
    target: DurableAgent | DurableFunction

    def __call__(self, **inputs: Any) -> Any: ...

    def start(self, **inputs: Any) -> Run: ...

    def enqueue(self, *, queue: str | None = ..., **inputs: Any) -> Run: ...

    def schedule(self, **options: Any) -> Schedule: ...

    def result(self, run_id: str) -> Any: ...


def _target_signature(
    target: DurableAgent | DurableFunction,
) -> inspect.Signature:
    if isinstance(target, DurableFunction):
        return inspect.signature(target.function)
    if isinstance(target, DurableAgent):
        fields = getattr(target.module.signature, "input_fields", {})
        return inspect.Signature(
            [
                inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=Any)
                for name in fields
            ]
        )
    return inspect.signature(target)  # pragma: no cover - Worker wraps callables


def Worker(target: Any, *, name: str | None = None) -> WorkerHandle:
    """Return a callable that runs *target* durably.

    The returned function also exposes ``start``, ``enqueue``, ``schedule``,
    and ``result``, so it can be used either as a normal tool or as a handle.

    Example:
        >>> from dspy_agent_tools import Worker
        >>> research = Worker(researcher_agent)
        >>> run = research.start(question="What is new in DBOS?")
        >>> research.result(run.id)
    """
    if getattr(target, "__durable_worker__", False):
        return target
    if isinstance(target, (DurableAgent, DurableFunction)):
        durable = target
    elif callable(target):
        durable = DurableFunction(target, name=name)
    else:
        raise TypeError(
            f"Worker() expected a durable agent, function, or callable, got "
            f"{type(target).__name__}"
        )
    worker_name = name or durable.name

    @functools.wraps(getattr(durable, "function", durable))
    def run(**inputs: Any) -> Any:
        return durable(**inputs)

    run.__name__ = worker_name.replace(".", "_")
    run.__qualname__ = run.__name__
    vars(run)["__signature__"] = _target_signature(durable)
    run.__doc__ = f"Run the {worker_name} worker durably and return its result."
    vars(run)["target"] = durable
    vars(run)["name"] = worker_name
    vars(run)["start"] = durable.start
    vars(run)["enqueue"] = durable.enqueue
    vars(run)["schedule"] = durable.schedule
    vars(run)["result"] = lambda run_id: Run.get(run_id).result()
    vars(run)["__durable_worker__"] = True
    return cast(WorkerHandle, run)


def _dspy_tool(function: Any) -> Any:
    try:
        from dspy import Tool
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("worker_tools() needs DSPy; install dspy>=3.4.1") from exc
    return Tool(function)


def worker_tools(
    allowed: Iterable[Any],
    *,
    schedules: bool = True,
) -> list[Any]:
    """Return orchestration tools for a fixed set of workers.

    Only workers in *allowed* can be started; the LLM cannot register arbitrary
    code or unbounded schedules.
    """
    registry = {worker.name: worker for worker in (Worker(item) for item in allowed)}
    if not registry:
        raise DurableError("worker_tools() needs at least one allowed worker")

    def list_workers() -> list[str]:
        """List the workers this agent may start."""
        return sorted(registry)

    def start_worker(name: str, inputs: dict[str, Any]) -> str:
        """Start a worker in the background and return its run ID."""
        worker = _lookup(registry, name)
        return worker.start(**inputs).id

    def worker_status(run_id: str) -> str:
        """Return a run's status: PENDING, SUCCESS, ERROR, or CANCELLED."""
        return Run.get(run_id).status()

    def worker_result(run_id: str) -> Any:
        """Wait for a run to finish and return its result."""
        return Run.get(run_id).result()

    def cancel_worker(run_id: str) -> str:
        """Cancel a run."""
        Run.get(run_id).cancel()
        return f"cancelled {run_id}"

    def wait_for_workers(run_ids: list[str]) -> dict[str, Any]:
        """Wait for several runs and return their results keyed by run ID."""
        return {run_id: Run.get(run_id).result() for run_id in run_ids}

    def schedule_worker(
        name: str, cron: str, inputs: dict[str, Any] | None = None
    ) -> str:
        """Create a recurring run for a worker and return the schedule name."""
        return _lookup(registry, name).schedule(cron=cron, inputs=inputs or {}).name

    def list_schedules() -> list[dict[str, Any]]:
        """List registered schedules."""
        from .runtime import runtime

        return [dict(row) for row in runtime().client().list_schedules()]

    tools = [
        list_workers,
        start_worker,
        worker_status,
        worker_result,
        cancel_worker,
        wait_for_workers,
    ]
    if schedules:
        tools.extend([schedule_worker, list_schedules])
    return [_dspy_tool(function) for function in tools]


def _lookup(registry: dict[str, Any], name: str) -> Any:
    worker = registry.get(name)
    if worker is None:
        raise DurableError(
            f"worker {name!r} is not in the allowlist; available: {sorted(registry)}"
        )
    return worker


__all__ = ["Schedule", "Worker", "worker_tools"]
