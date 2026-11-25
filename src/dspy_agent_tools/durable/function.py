"""Durable wrappers for plain Python functions and workers."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from ._dbos import load_dbos
from .context import (
    check_pause,
    emit,
    in_workflow_body,
    set_workflow_event,
    workflow_state,
)
from .events import EventType
from .exceptions import AgentExecutionError, DurableError
from .runtime import runtime
from .schedule import Schedule
from .tool import jsonable, tool_name

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
        version: str | None = None,
        tenant: str | None = None,
        queue: str | None = None,
        retries: int = 0,
        concurrency: int | None = None,
        worker: bool = False,
        verbose: bool | None = None,
    ) -> None:
        if not callable(function):
            raise TypeError(
                f"Durable() expected a callable, got {type(function).__name__}"
            )
        load_dbos()
        runtime().set_verbose(
            verbose if verbose is not None else runtime().config.verbose
        )
        self.function: Callable[..., Any] = function
        self.name = _function_name(function, name)
        self.version = version
        self.tenant = tenant
        self.queue = queue or (self.name if worker else None)
        if retries:
            raise DurableError(
                "workflow retries are not supported; wrap retryable operations "
                "with Durable.step(retries=...) instead"
            )
        self.retries = 0
        self.concurrency = concurrency
        self.worker = worker
        functools.update_wrapper(self, function)

        key = f"{self.name}@{self.version}" if self.version else self.name
        existing = _FUNCTIONS.get(key)
        if existing is not None and existing is not self:
            raise DurableError(
                f"a durable function named {self.name!r} already exists; pass name=..."
            )
        _FUNCTIONS[key] = self

        self._register_workflows()
        if self.queue:
            runtime().register_queue(self.queue, concurrency=concurrency)

    def _register_workflows(self) -> None:
        from dbos import DBOS

        base = f"dspy-agent:fn:{self.name}"
        if self.version:
            base = f"{base}@{self.version}"

        @DBOS.workflow(name=base)
        def run(**inputs: Any) -> Any:
            return self._execute(**inputs)

        @DBOS.workflow(name=f"{base}:scheduled")
        def scheduled(_scheduled_at: datetime, context: dict[str, Any]) -> Any:
            return self._execute(**(context or {}))

        self._run_workflow = run
        self._scheduled_workflow = scheduled

    def _execute(self, **inputs: Any) -> Any:
        if not in_workflow_body():
            return self._call(inputs)
        with workflow_state(self.name, self.tenant):
            check_pause()
            input_data = jsonable(inputs)
            emit(EventType.RUN_STARTED, agent=self.name, input=input_data)
            set_workflow_event("function.input", input_data)
            try:
                result = self._call(inputs)
            except Exception as exc:
                if type(exc).__name__ == "DBOSWorkflowCancelledError":
                    raise  # pragma: no cover - DBOS cancellation
                error = f"{type(exc).__name__}: {exc}"
                emit(
                    EventType.RUN_FAILED,
                    agent=self.name,
                    input=input_data,
                    error=error,
                )
                set_workflow_event("function.error", {"error": error})
                raise AgentExecutionError(
                    self.name, type(exc).__name__, str(exc)
                ) from None
            output_data = jsonable(result)
            emit(
                EventType.RUN_COMPLETED,
                agent=self.name,
                input=input_data,
                output=output_data,
            )
            set_workflow_event("function.output", output_data)
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
        run = await self.astart(**inputs)
        return await run.aresult()

    async def astart(
        self,
        *,
        workflow_id: str | None = None,
        timeout: float | None = None,
        **inputs: Any,
    ) -> Run:
        return self.start(workflow_id=workflow_id, timeout=timeout, **inputs)

    def start(
        self,
        *,
        workflow_id: str | None = None,
        timeout: float | None = None,
        **inputs: Any,
    ) -> Run:
        runtime().ensure_launched()
        from dbos import SetWorkflowID, SetWorkflowTimeout

        with ExitStack() as stack:
            if workflow_id is not None:
                stack.enter_context(SetWorkflowID(workflow_id))
            if timeout is not None:
                stack.enter_context(SetWorkflowTimeout(timeout))
            handle = load_dbos().DBOS.start_workflow(self._run_workflow, **inputs)
        from .run import Run

        return Run(handle.get_workflow_id(), agent=self.name, tenant=self.tenant)

    def enqueue(
        self,
        *,
        queue: str | None = None,
        delay: float | None = None,
        priority: int | None = None,
        deduplication_id: str | None = None,
        duplication_policy: Literal["reject", "return-existing"] | None = None,
        workflow_id: str | None = None,
        queue_partition_key: str | None = None,
        timeout: float | None = None,
        **inputs: Any,
    ) -> Run:
        queue_name = queue or self.queue
        if not queue_name:
            raise DurableError(
                "enqueue() needs a queue: pass queue=... or construct the worker "
                "with queue=..."
            )
        runtime().ensure_launched()
        runtime().register_queue(queue_name)
        from dbos import SetEnqueueOptions, SetWorkflowID, SetWorkflowTimeout

        with ExitStack() as stack:
            if any(
                value is not None
                for value in (
                    delay,
                    priority,
                    deduplication_id,
                    duplication_policy,
                    queue_partition_key,
                )
            ):
                stack.enter_context(
                    SetEnqueueOptions(
                        delay_seconds=delay,
                        priority=priority,
                        deduplication_id=deduplication_id,
                        duplication_policy=duplication_policy,
                        queue_partition_key=queue_partition_key,
                    )
                )
            if workflow_id is not None:
                stack.enter_context(SetWorkflowID(workflow_id))
            if timeout is not None:
                stack.enter_context(SetWorkflowTimeout(timeout))
            handle = load_dbos().DBOS.enqueue_workflow(
                queue_name, self._run_workflow, **inputs
            )
        from .run import Run

        return Run(handle.get_workflow_id(), agent=self.name, tenant=self.tenant)

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
