"""Errors raised by the durable execution layer."""

from __future__ import annotations

from typing import Any


class DurableError(RuntimeError):
    """Base error for the durable layer."""


class DuplicateAgentName(DurableError):
    """Two live durable agents tried to use the same workflow name."""


class DurableExecutionError(DurableError):
    """A durable run failed and its recorded exception could not be restored."""

    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(f"run {run_id} failed: {detail}")
        self.run_id = run_id
        self.detail = detail

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.run_id, self.detail))


class AgentExecutionError(DurableError):
    """A durable agent failed; the original error is reduced to a message."""

    def __init__(self, agent: str, error_type: str, message: str) -> None:
        self.agent = agent
        self.error_type = error_type
        self.message = message
        super().__init__(f"agent {agent!r} failed with {error_type}: {message}")

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.agent, self.error_type, self.message))


class NotInWorkflow(DurableError):
    """An operation that needs a workflow was called outside one."""


class HumanInputRequired(DurableError):
    """A human decision is needed but no interactive terminal is available."""

    def __init__(self, request: dict[str, Any]) -> None:
        self.request = request
        super().__init__(
            f"human input required for {request.get('kind', 'request')} "
            f"{request.get('id', '')}: {request.get('message', '')}"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.request,))


class HumanTimeout(DurableError):
    """A human did not answer before the request's deadline."""

    def __init__(self, request: dict[str, Any], timeout: float) -> None:
        self.request = request
        self.timeout = timeout
        super().__init__(
            f"no response to {request.get('kind', 'request')} "
            f"{request.get('id', '')} within {timeout:g}s"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.request, self.timeout))
