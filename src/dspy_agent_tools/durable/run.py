"""Control plane for a durable agent run."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Generator, Iterator
from typing import Any

from .context import CONTROL_TOPIC, EVENTS_STREAM, PENDING_EVENT
from .events import ApprovalRequest, Event, QuestionRequest, event_from_payload
from .exceptions import DurableExecutionError
from .runtime import runtime

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = frozenset({"PENDING", "ENQUEUED", "DELAYED", "SUSPENDED"})


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

    def __init__(
        self,
        run_id: str,
        *,
        agent: str | None = None,
        tenant: str | None = None,
    ) -> None:
        self.id = run_id
        self.agent = agent
        self.tenant = tenant

    @classmethod
    def get(cls, run_id: str, *, tenant: str | None = None) -> Run:
        """Reconnect to a run from this or another process."""
        return cls(run_id, tenant=tenant)

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

    def result(self, *, timeout: float | None = None) -> Any:
        """Block until the run finishes and return its prediction or value."""
        if timeout is not None:
            deadline = time.monotonic() + timeout
            while self.is_active():
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"run {self.id} did not finish within {timeout:g}s"
                    )
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
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

    async def aresult(self, *, timeout: float | None = None) -> Any:
        """Async variant of :meth:`result`."""
        return await asyncio.to_thread(lambda: self.result(timeout=timeout))

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

    def pause(self) -> Run:
        """Pause at the next checkpoint boundary."""
        if not self._send_control("pause"):
            raise DurableExecutionError(self.id, "control messages are unavailable")
        return self

    def resume(self) -> Run:
        """Resume a paused, cancelled, or interrupted run."""
        self._send_control("resume")
        try:
            status = self.status()
        except DurableExecutionError:
            status = None
        if status in (None, "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"):
            self._client().resume_workflow(self.id)
        return self

    def _send_control(self, control: str) -> bool:
        send = getattr(self._client(), "send", None)
        if not callable(send):
            return False
        send(
            self.id,
            {"control": control},
            topic=CONTROL_TOPIC,
            idempotency_key=f"{self.id}:{control}:{time.time_ns()}",
        )
        return True

    def _authorize(self, action: str, request: dict[str, Any] | None) -> None:
        authorizer = getattr(runtime().config, "authorizer", None)
        if authorizer is None:
            return
        tenant = self.tenant or (request or {}).get("tenant")
        if not authorizer(action, self.id, tenant, request):
            raise PermissionError(f"{action} is not authorized for run {self.id!r}")

    def _request_payload(self, request_id: str) -> dict[str, Any] | None:
        for request in self.pending():
            if request.id == request_id:
                return request.request
        return None

    def retry(self) -> Run:
        """Retry an interrupted run from its recorded prefix.

        Terminal application errors are not necessarily retryable; use
        :meth:`fork` when the recorded failure must be re-executed.
        """
        return self.resume()

    def fork(
        self,
        start_step: int = 1,
        *,
        application_version: str | None = None,
        queue_name: str | None = None,
    ) -> Run:
        """Fork this run at *start_step* and return the new run."""
        handle = self._client().fork_workflow(
            self.id,
            start_step,
            application_version=application_version,
            queue_name=queue_name,
        )
        return Run(handle.get_workflow_id(), agent=self.agent)

    @classmethod
    def list_runs(
        cls,
        *,
        status: str | list[str] | None = None,
        name: str | list[str] | None = None,
        queue_name: str | list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_desc: bool = True,
    ) -> list[Run]:
        """List runs in the system database."""
        rows = (
            runtime()
            .client()
            .list_workflows(
                status=status,
                name=name,
                queue_name=queue_name,
                limit=limit,
                offset=offset,
                sort_desc=sort_desc,
            )
        )
        return [Run(row.workflow_id, agent=row.name) for row in rows]

    def steps(self, *, load_output: bool = True) -> list[dict[str, Any]]:
        """Return the recorded workflow steps.

        Each step's ``output`` is its checkpoint. Tool steps contain
        ``input`` (positional args and keyword args) and ``output``; predictor
        steps contain the predictor ``input``, the parsed ``output``, and the
        raw ``llm`` responses.
        """
        return list(
            self._client().list_workflow_steps(self.id, load_output=load_output)
        )

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
        self._authorize("approve", self._request_payload(request_id))
        self._send(request_id, {"approved": True}, "approve")

    def reject(self, request_id: str, reason: str | None = None) -> None:
        """Reject a pending action with optional feedback for the agent."""
        self._authorize("reject", self._request_payload(request_id))
        self._send(
            request_id,
            {"approved": False, "reason": reason},
            "reject",
        )

    def respond(self, request_id: str, answer: Any) -> None:
        """Answer a pending question."""
        self._authorize("respond", self._request_payload(request_id))
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
