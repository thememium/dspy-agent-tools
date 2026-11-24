"""Durable wrappers for plain Python functions and workers."""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ._dbos import load_dbos
from .context import emit, in_workflow_body, workflow_state
from .events import EventType
from .exceptions import AgentExecutionError, DurableError
from .runtime import runtime
from .schedule import Schedule
from .tool import tool_name

if TYPE_CHECKING:
    from .run import Run

_FUNCTIONS: dict[str, DurableFunction] = {}


def _function_name(function: Callable[..., Any], name: str | None) -> str:
    if name:
        return name
    return f"{getattr(function, '__module__', 'function')}.{getattr(function, '__qualname__', tool_name(function))}"


class DurableFunction:
    """A plain callable executed as a DBOS workflow.

    The function body runs at the workflow level, so it can call ``Human``,
    start child agents, or schedule work. Wrap individual side effects with
    :meth:`Durable.tool` when each one needs its own checkpoint.

    Example:
        >>> from dspy_agent_tools import Durable
        >>> @Durable.worker(queue="indexing", concurrency=5)
        ... def index_repository(repo: str) -> dict:
        ...     return {"repo": repo}
        >>> run = index_repository.enqueue(repo="thememium/dspy-agent-tools")
        >>> run.result()
    """

    def __init__(
        self,
        function: Callable[..., Any],
        *,
        name: str | None = None,
        queue: str | None = None,
        retries: int = 0,
        concurrency: int | None = None,
        worker: bool = False,
    ) -> None:
        if not callable(function):
            raise TypeError(
                f"Durable() expected a callable, got {type(function).__name__}"
            )
        load_dbos()
        self.function: Callable[..., Any] = function
        self.name = _function_name(function, name)
        self.queue = queue or (self.name if worker else None)
        self.retries = max(0, int(retries))
        self.concurrency = concurrency
        self.worker = worker
        functools.update_wrapper(self, function)

        existing = _FUNCTIONS.get(self.name)
        if existing is not None and existing is not self:
            raise DurableError(
                f"a durable function named {self.name!r} already exists; pass name=..."
            )
        _FUNCTIONS[self.name] = self

        self._register_workflows()
        if self.queue:
            runtime().register_queue(self.queue, concurrency=concurrency)

    def _register_workflows(self) -> None:
        from dbos import DBOS

        @DBOS.workflow(name=f"dspy-agent:fn:{self.name}")
        def run(**inputs: Any) -> Any:
            return self._execute(**inputs)

        @DBOS.workflow(name=f"dspy-agent:fn:{self.name}:scheduled")
        def scheduled(_scheduled_at: datetime, context: dict[str, Any]) -> Any:
            return self._execute(**(context or {}))

        self._run_workflow = run
        self._scheduled_workflow = scheduled

    def _execute(self, **inputs: Any) -> Any:
        if not in_workflow_body():
            return self._call(inputs)
        with workflow_state(self.name):
            emit(EventType.RUN_STARTED, agent=self.name)
            try:
                result = self._call(inputs)
            except Exception as exc:
                if type(exc).__name__ == "DBOSWorkflowCancelledError":
                    raise  # pragma: no cover - DBOS cancellation
                emit(
                    EventType.RUN_FAILED,
                    agent=self.name,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise AgentExecutionError(
                    self.name, type(exc).__name__, str(exc)
                ) from None
            emit(EventType.RUN_COMPLETED, agent=self.name)
            return result

    def _call(self, inputs: dict[str, Any]) -> Any:
        result = self.function(**inputs)
        if inspect.iscoroutine(result):
            from .tool import run_coroutine

            return run_coroutine(result)
        return result

    def __call__(self, **inputs: Any) -> Any:
        return self.start(**inputs).result()

    async def acall(self, **inputs: Any) -> Any:
        run = self.start(**inputs)
        return await asyncio.to_thread(run.result)

    def start(self, **inputs: Any) -> Run:
        runtime().ensure_launched()
        handle = load_dbos().DBOS.start_workflow(self._run_workflow, **inputs)
        from .run import Run

        return Run(handle.get_workflow_id(), agent=self.name)

    def enqueue(self, *, queue: str | None = None, **inputs: Any) -> Run:
        queue_name = queue or self.queue
        if not queue_name:
            raise DurableError(
                "enqueue() needs a queue: pass queue=... or construct the worker "
                "with queue=..."
            )
        runtime().ensure_launched()
        runtime().register_queue(queue_name)
        handle = load_dbos().DBOS.enqueue_workflow(
            queue_name, self._run_workflow, **inputs
        )
        from .run import Run

        return Run(handle.get_workflow_id(), agent=self.name)

    def schedule(
        self,
        *,
        cron: str,
        inputs: dict[str, Any] | None = None,
        name: str | None = None,
        timezone: str | None = None,
        queue: str | None = None,
        **schedule_inputs: Any,
    ) -> Schedule:
        """Register a recurring durable execution."""
        runtime().ensure_launched()
        schedule_name = name or f"{self.name}:schedule"
        context = {**(inputs or {}), **schedule_inputs}
        load_dbos().DBOS.create_schedule(
            schedule_name=schedule_name,
            workflow_fn=self._scheduled_workflow,
            schedule=cron,
            context=context,
            cron_timezone=timezone,
            queue_name=queue or self.queue,
        )
        return Schedule(schedule_name)

    def __repr__(self) -> str:
        return f"DurableFunction(name={self.name!r})"
