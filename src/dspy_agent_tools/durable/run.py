"""Control plane for a durable agent run."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Generator, Iterator
from typing import Any

from .context import EVENTS_STREAM, PENDING_EVENT
from .events import ApprovalRequest, Event, QuestionRequest, event_from_payload
from .exceptions import DurableExecutionError
from .runtime import runtime

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = frozenset(
    {"PENDING", "ENQUEUED", "DELAYED", "SUSPENDED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}
)


class Run:
    """A durable execution that can be inspected and controlled by ID.

    Example:
        >>> from dspy_agent_tools import ApprovalRequest, Run
        >>> run = Run.get(workflow_id)
        >>> run.status()
        'PENDING'
        >>> for event in run.events():
        ...     if isinstance(event, ApprovalRequest):
        ...         event.approve()
        >>> run.result().answer
    """

    def __init__(self, run_id: str, *, agent: str | None = None) -> None:
        self.id = run_id
        self.agent = agent

    @classmethod
    def get(cls, run_id: str) -> Run:
        """Reconnect to a run from this or another process."""
        return cls(run_id)

    def __repr__(self) -> str:
        return f"Run(id={self.id!r}, agent={self.agent!r})"

    def _client(self) -> Any:
        return runtime().client()

    def status(self) -> str:
        """Return the DBOS status string for this run."""
        try:
            return str(self._client().retrieve_workflow(self.id).get_status().status)
        except Exception as exc:
            raise DurableExecutionError(self.id, str(exc)) from exc

    def is_active(self) -> bool:
        """Whether the run may still make progress."""
        return self.status() in ACTIVE_STATUSES

    def result(self) -> Any:
        """Block until the run finishes and return its prediction or value."""
        try:
            return self._client().retrieve_workflow(self.id).get_result()
        except DurableExecutionError:
            raise
        except TypeError as exc:
            raise DurableExecutionError(
                self.id,
                f"the recorded failure could not be restored ({exc}); "
                "inspect run.steps() for the failing operation",
            ) from exc

    def cancel(self) -> None:
        """Cancel the run and wake any human wait it is blocked on.

        DBOS does not interrupt a workflow blocked in ``recv``; sending a
        rejection to each pending request lets the wait return, after which the
        next checkpoint observes the cancelled status.
        """
        pending = self.pending()
        self._client().cancel_workflow(self.id)
        for request in pending:
            try:
                self._send(
                    request.id,
                    {"approved": False, "reason": "run cancelled"},
                    "cancel",
                )
            except Exception:  # noqa: BLE001 - the run is already cancelled
                logger.debug("could not wake human request %s", request.id)

    def resume(self) -> Run:
        """Resume a cancelled or interrupted run."""
        self._client().resume_workflow(self.id)
        return self

    def steps(self) -> list[dict[str, Any]]:
        """Return the recorded workflow steps."""
        return list(self._client().list_workflow_steps(self.id, load_output=False))

    def pending(self) -> list[ApprovalRequest | QuestionRequest]:
        """Return this run's outstanding human requests."""
        try:
            payloads = self._client().get_event(
                self.id, PENDING_EVENT, timeout_seconds=0
            )
        except Exception:  # noqa: BLE001 - missing event means nothing pending
            return []
        requests: list[ApprovalRequest | QuestionRequest] = []
        for request in payloads or []:
            event = event_from_payload(
                {
                    "type": request["event"],
                    "run_id": self.id,
                    "at": request.get("created_at", 0.0),
                    "data": {"request": request},
                }
            )
            if isinstance(event, (ApprovalRequest, QuestionRequest)):
                requests.append(event)
        return requests

    def approve(self, request_id: str) -> None:
        """Approve a pending action."""
        self._send(request_id, {"approved": True}, "approve")

    def reject(self, request_id: str, reason: str | None = None) -> None:
        """Reject a pending action with optional feedback for the agent."""
        self._send(
            request_id,
            {"approved": False, "reason": reason},
            "reject",
        )

    def respond(self, request_id: str, answer: Any) -> None:
        """Answer a pending question."""
        self._send(request_id, {"answer": answer}, "respond")

    def _send(self, request_id: str, message: dict[str, Any], action: str) -> None:
        self._client().send(
            self.id,
            message,
            topic=f"human:{request_id}",
            idempotency_key=f"{request_id}:{action}",
        )

    def events(self, *, offset: int = 0) -> Generator[Event]:
        """Tail the persisted event stream until the run reaches a terminal state."""
        client = self._client()
        position = offset
        while True:
            for payload in client.read_stream(
                self.id,
                EVENTS_STREAM,
                offset=position,
                timeout_seconds=1.0,
            ):
                position += 1
                yield event_from_payload(payload)
            if not self.is_active():
                for payload in client.read_stream(
                    self.id,
                    EVENTS_STREAM,
                    offset=position,
                    timeout_seconds=0,
                ):  # pragma: no cover - race with the last write
                    position += 1
                    yield event_from_payload(payload)
                return

    def aevents(self, *, offset: int = 0) -> AsyncGenerator[Event]:
        """Async iterator over :meth:`events`."""

        def step(iterator: Iterator[Event]) -> tuple[bool, Event | None]:
            try:
                return True, next(iterator)
            except StopIteration:
                return False, None

        async def iterate() -> Any:
            import asyncio

            iterator = iter(self.events(offset=offset))
            while True:
                found, event = await asyncio.to_thread(step, iterator)
                if not found:
                    return
                assert event is not None
                yield event

        return iterate()

    def __iter__(self) -> Iterator[Event]:
        return self.events()
