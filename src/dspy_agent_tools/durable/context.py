"""Workflow-scoped state and DBOS context helpers."""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from ._dbos import load_dbos
from .events import Event, EventType, event_payload
from .exceptions import NotInWorkflow

EVENTS_STREAM = "events"
PENDING_EVENT = "human.pending"
_STATE: ContextVar[WorkflowState | None] = ContextVar(
    "durable_workflow_state", default=None
)
_OPERATION_ID: ContextVar[str | None] = ContextVar("durable_operation_id", default=None)


@dataclass
class WorkflowState:
    """Mutable bookkeeping for one workflow execution."""

    agent_name: str
    run_id: str
    human_seq: int = 0
    operation_seq: int = 0
    event_seq: int = 0
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)


_EVENT_FUNCTION_BASE = -1_000_000
_IO_EVENT_IDS = {
    "agent.input": -2_000_001,
    "agent.output": -2_000_002,
    "agent.error": -2_000_003,
    "function.input": -2_000_004,
    "function.output": -2_000_005,
    "function.error": -2_000_006,
}


def _write_event(run_id: str, seq: int, payload: dict[str, Any]) -> None:
    """Append an event to the DBOS stream without creating a step record.

    ``DBOS.write_stream`` records an empty ``DBOS.writeStream`` operation for
    every event. The event sequence is also its stream offset, so a replayed
    workflow can detect the existing event by ``(workflow, key, offset)`` and
    skip the duplicate. The synthetic function ID only distinguishes rows.
    """
    from dbos import _sys_db
    from dbos._dbos import _get_dbos_instance
    from dbos._serialization import WorkflowSerializationFormat

    select = _sys_db.sa.select
    dbos = _get_dbos_instance()
    function_id = _EVENT_FUNCTION_BASE - seq
    streams = dbos._sys_db.tables.streams
    with dbos._sys_db.engine.connect() as conn:
        existing = conn.execute(
            select(streams.c.offset)
            .where(
                streams.c.workflow_uuid == run_id,
                streams.c.key == EVENTS_STREAM,
                streams.c.offset == seq,
            )
            .limit(1)
        ).first()
    if existing is not None:
        return
    dbos._sys_db.write_stream_from_step(
        run_id,
        function_id,
        EVENTS_STREAM,
        payload,
        serialization_type=WorkflowSerializationFormat.DEFAULT,
    )


def set_workflow_event(key: str, value: Any) -> None:
    """Publish a top-level workflow event without creating a step row.

    DBOS's public ``set_event`` records a ``DBOS.setEvent`` operation (an empty
    step) and is only valid in a workflow body. The sys-db writer updates the
    same event tables without the operation record, so Argus shows the data in
    its Events panel while the step list stays limited to real work.
    """
    run_id = current_run_id()
    if run_id is None:
        return
    from dbos._dbos import _get_dbos_instance
    from dbos._serialization import WorkflowSerializationFormat

    _get_dbos_instance()._sys_db.set_event_from_step(
        run_id,
        _IO_EVENT_IDS[key],
        key,
        value,
        serialization_type=WorkflowSerializationFormat.PORTABLE,
    )


def _context() -> Any:
    """Return DBOS's current context, or ``None`` outside a DBOS context."""
    try:
        from dbos._context import get_local_dbos_context
    except ImportError:  # pragma: no cover - DBOS is installed whenever this runs
        return None
    return get_local_dbos_context()


def in_workflow() -> bool:
    """True inside a workflow body or one of its steps."""
    ctx = _context()
    return bool(ctx is not None and ctx.is_within_workflow())


def in_workflow_body() -> bool:
    """True only in the workflow body, not inside a step."""
    ctx = _context()
    return bool(ctx is not None and ctx.is_workflow())


def current_run_id() -> str | None:
    """Return the current workflow ID, if any."""
    ctx = _context()
    if ctx is None or not ctx.is_within_workflow():
        return None
    return str(ctx.workflow_id)


def current_operation_id() -> str | None:
    """Return the ID assigned to the currently executing durable operation."""
    return _OPERATION_ID.get()


@contextmanager
def workflow_state(agent_name: str) -> Generator[WorkflowState]:
    """Install per-run state for the duration of a workflow body."""
    run_id = current_run_id()
    if run_id is None:
        raise NotInWorkflow("workflow state requires a DBOS workflow")
    state = WorkflowState(agent_name=agent_name, run_id=run_id)
    token = _STATE.set(state)
    try:
        yield state
    finally:
        _STATE.reset(token)


def state() -> WorkflowState:
    """Return the state of the current workflow body."""
    current = _STATE.get()
    if current is None:
        raise NotInWorkflow("no durable workflow is active")
    return current


def next_operation_id() -> str:
    """Allocate a replay-stable operation ID."""
    current = state()
    current.operation_seq += 1
    return f"{current.run_id}:op:{current.operation_seq}"


def next_human_id() -> str:
    """Allocate a replay-stable human-request ID."""
    current = state()
    current.human_seq += 1
    return f"{current.run_id}:human:{current.human_seq}"


@contextmanager
def operation_scope(operation_id: str) -> Generator[str]:
    """Expose an operation ID to code running inside a checkpointed step."""
    token = _OPERATION_ID.set(operation_id)
    try:
        yield operation_id
    finally:
        _OPERATION_ID.reset(token)


def emit(event_type: EventType | str, **data: Any) -> Event | None:
    """Append a semantic event to the run stream.

    Events are only written from the workflow body. Calls made inside a step
    (where DBOS would nest the write) or outside a workflow are ignored.
    """
    if not in_workflow_body():
        return None
    run_id = current_run_id()
    if run_id is None:  # pragma: no cover - in_workflow_body implies an ID
        return None
    event = Event(str(event_type), run_id, data, time.time())
    current = state()
    _write_event(run_id, current.event_seq, event_payload(event))
    current.event_seq += 1
    return event


def set_pending(request: dict[str, Any] | None, *, removing: str | None = None) -> None:
    """Publish the workflow's pending human requests as a DBOS event."""
    current = state()
    if request is not None:
        current.pending[request["id"]] = request
    if removing is not None:
        current.pending.pop(removing, None)
    load_dbos().DBOS.set_event(PENDING_EVENT, list(current.pending.values()))
