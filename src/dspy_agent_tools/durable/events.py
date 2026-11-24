"""Events written to a run's persisted stream."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    """Semantic events persisted for every durable run."""

    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"

    LLM_STARTED = "llm.started"
    LLM_COMPLETED = "llm.completed"
    LLM_FAILED = "llm.failed"

    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    QUESTION_REQUESTED = "question.requested"
    QUESTION_RESOLVED = "question.resolved"


class Event:
    """One entry from a run's persisted event stream."""

    __slots__ = ("at", "data", "run_id", "type")

    def __init__(
        self,
        type: str,
        run_id: str,
        data: dict[str, Any] | None = None,
        at: float | None = None,
    ) -> None:
        self.type = type
        self.run_id = run_id
        self.data = data or {}
        self.at = time.time() if at is None else at

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "run_id": self.run_id,
            "at": self.at,
            "data": self.data,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(type={self.type!r}, run_id={self.run_id!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Event):
            return NotImplemented
        return self.to_dict() == other.to_dict()


class HumanRequest(Event):
    """A pending human decision carried by an event."""

    def __init__(self, request: dict[str, Any], run_id: str, at: float) -> None:
        super().__init__(request["event"], run_id, {"request": request}, at)
        self.request = request

    @property
    def id(self) -> str:
        return self.request["id"]

    @property
    def message(self) -> str:
        return self.request["message"]

    @property
    def tool(self) -> str | None:
        return self.request.get("tool")

    @property
    def arguments(self) -> dict[str, Any] | None:
        return self.request.get("arguments")

    @property
    def choices(self) -> list[str] | None:
        return self.request.get("choices")

    def defer(self) -> HumanRequest:
        """Leave the request pending and let the caller keep streaming."""
        return self

    def _run(self) -> Any:
        from .run import Run

        return Run.get(self.run_id)


class ApprovalRequest(HumanRequest):
    """An agent is waiting for permission to run a tool."""

    def approve(self) -> None:
        """Approve the pending action."""
        self._run().approve(self.id)

    def reject(self, reason: str | None = None) -> None:
        """Reject the pending action, optionally with feedback for the agent."""
        self._run().reject(self.id, reason=reason)


class QuestionRequest(HumanRequest):
    """An agent is waiting for an answer to a question."""

    @property
    def question(self) -> str:
        return self.message

    def respond(self, answer: Any) -> None:
        """Send an answer to the waiting agent."""
        self._run().respond(self.id, answer)


def event_from_payload(payload: dict[str, Any]) -> Event:
    """Rebuild an :class:`Event` (or a human request subclass) from a stream row."""
    event_type = payload.get("type", "")
    run_id = payload.get("run_id", "")
    at = payload.get("at", 0.0)
    data = payload.get("data") or {}
    request = data.get("request")
    if request is not None:
        if event_type == EventType.APPROVAL_REQUESTED:
            return ApprovalRequest(request, run_id, at)
        if event_type == EventType.QUESTION_REQUESTED:
            return QuestionRequest(request, run_id, at)
    return Event(event_type, run_id, data, at)


def event_payload(event: Event) -> dict[str, Any]:
    """Convert an event to the dict stored in the DBOS stream."""
    return event.to_dict()
