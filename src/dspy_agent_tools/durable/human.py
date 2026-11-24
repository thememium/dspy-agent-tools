"""Persisted human approvals and questions."""

from __future__ import annotations

import functools
import inspect
import logging
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .context import (
    PENDING_EVENT,
    current_run_id,
    emit,
    in_workflow_body,
    next_human_id,
    set_pending,
)
from .events import ApprovalRequest, EventType, QuestionRequest
from .exceptions import HumanInputRequired, HumanTimeout, NotInWorkflow
from .runtime import runtime
from .tool import call_tool, summarize, tool_name

logger = logging.getLogger(__name__)
_INTERACTIVE: ContextVar[bool | None] = ContextVar("durable_interactive", default=None)
_DEFAULT_TIMEOUT = 86_400.0


@contextmanager
def _interactive(enabled: bool | None):
    """Override terminal prompting for the duration of a call."""
    token = _INTERACTIVE.set(enabled)
    try:
        yield
    finally:
        _INTERACTIVE.reset(token)


def _can_prompt() -> bool:
    override = _INTERACTIVE.get()
    if override is not None:
        return override
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - closed stdin
        return False


def _request(
    kind: str,
    event: EventType,
    message: str,
    *,
    tool: str | None = None,
    arguments: dict[str, Any] | None = None,
    choices: list[str] | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": request_id or uuid.uuid4().hex,
        "run_id": run_id or current_run_id() or "",
        "kind": kind,
        "event": str(event),
        "message": message,
        "tool": tool,
        "arguments": summarize(arguments) if arguments else None,
        "choices": list(choices) if choices else None,
        "reason": reason,
        "created_at": time.time(),
    }


def _request_object(
    request: dict[str, Any], run_id: str | None = None
) -> ApprovalRequest | QuestionRequest:
    request = {**request, "run_id": request.get("run_id") or run_id or ""}
    if request.get("kind") == "question":
        return QuestionRequest(
            request, request["run_id"], request.get("created_at", 0.0)
        )
    return ApprovalRequest(request, request["run_id"], request.get("created_at", 0.0))


def _wait_for_human(
    kind: str,
    event: EventType,
    message: str,
    *,
    tool: str | None = None,
    arguments: dict[str, Any] | None = None,
    choices: list[str] | None = None,
    reason: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[dict[str, Any], Any]:
    """Publish a human request and wait for the matching DBOS message."""
    if not in_workflow_body():
        raise NotInWorkflow("human requests require a durable workflow")
    request = _request(
        kind,
        event,
        message,
        tool=tool,
        arguments=arguments,
        choices=choices,
        reason=reason,
        request_id=next_human_id(),
    )
    emit(event, request=request)
    set_pending(request)
    from ._dbos import load_dbos

    response = load_dbos().DBOS.recv(
        topic=f"human:{request['id']}", timeout_seconds=timeout
    )
    set_pending(None, removing=request["id"])
    emit(
        EventType.APPROVAL_RESOLVED
        if kind == "approval"
        else EventType.QUESTION_RESOLVED,
        request_id=request["id"],
        response=response,
    )
    return request, response


def _prompt(message: str, request: dict[str, Any]) -> str:
    if not _can_prompt():
        raise HumanInputRequired(request)
    choices = request.get("choices")
    if choices:
        prompt = f"{message}\n" + "\n".join(
            f"  {index + 1}. {choice}" for index, choice in enumerate(choices)
        )
        while True:
            answer = input(prompt + "\n> ").strip()
            if answer in choices:
                return answer
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1]
            print(f"Choose one of: {', '.join(choices)}")
    return input(f"{message}\n> ")


def _confirm(message: str, request: dict[str, Any]) -> bool:
    if not _can_prompt():
        raise HumanInputRequired(request)
    answer = input(f"{message} [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def ask_human(
    question: str,
    choices: list[str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """Ask the user a question and wait for their answer.

    Inside a durable workflow the wait is persisted and survives a restart.
    Outside one it prompts the terminal (when interactive) or raises
    :class:`HumanInputRequired`.
    """
    if in_workflow_body():
        request, response = _wait_for_human(
            "question",
            EventType.QUESTION_REQUESTED,
            question,
            choices=choices,
            timeout=timeout,
        )
        if response is None:
            raise HumanTimeout(request, timeout)
        return str(response.get("answer", ""))
    request = _request(
        "question", EventType.QUESTION_REQUESTED, question, choices=choices
    )
    return _prompt(question, request)


def ask_human_approval(message: str, *, timeout: float = _DEFAULT_TIMEOUT) -> bool:
    """Ask the user to approve an action and wait for the decision."""
    if in_workflow_body():
        request, response = _wait_for_human(
            "approval", EventType.APPROVAL_REQUESTED, message, timeout=timeout
        )
        return bool(response is not None and response.get("approved"))
    request = _request("approval", EventType.APPROVAL_REQUESTED, message)
    return _confirm(message, request)


vars(ask_human)["__human__"] = {"name": "ask_human", "when": None, "message": None}
vars(ask_human)["__durable_tool__"] = True
vars(ask_human)["__durable_name__"] = "ask_human"
vars(ask_human_approval)["__human__"] = {
    "name": "ask_human_approval",
    "when": None,
    "message": None,
}
vars(ask_human_approval)["__durable_tool__"] = True
vars(ask_human_approval)["__durable_name__"] = "ask_human_approval"


def _pending(run_id: str | None = None) -> list[ApprovalRequest | QuestionRequest]:
    """Return outstanding human requests, optionally scoped to one run."""
    client = runtime().client()
    if run_id is not None:
        rows = [run_id]
    else:
        workflows = client.list_workflows(status="PENDING", limit=500)
        rows = [workflow.workflow_id for workflow in workflows]
    requests: list[ApprovalRequest | QuestionRequest] = []
    for workflow_id in rows:
        try:
            pending = client.get_event(workflow_id, PENDING_EVENT, timeout_seconds=0)
        except Exception:  # noqa: BLE001 - a missing event means nothing pending
            logger.debug(
                "no pending human event for %s", workflow_id
            )  # pragma: no cover
            continue  # pragma: no cover
        for request in pending or []:
            requests.append(_request_object(request, workflow_id))
    return requests


def _approval_rejection(
    request: dict[str, Any] | None, reason: str | None
) -> dict[str, Any]:
    return {
        "status": "rejected",
        "reason": (request or {}).get("reason") or reason or "rejected by human",
    }


class Human:
    """Wrap a tool so each call waits for a human decision.

    ``Human(tool)`` preserves the tool's name, signature, and docstring. Use it
    anywhere a tool is accepted::

        tools = [read_file, Human(bash_exec), Human.ask]
    """

    def __new__(
        cls,
        tool: Any,
        *,
        when: Callable[..., bool] | None = None,
        message: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        _already_durable: bool = False,
    ) -> Any:
        if tool is None:
            raise TypeError("Human() requires a tool")
        return _wrap_human(
            tool,
            when=when,
            message=message,
            timeout=timeout,
            already_durable=_already_durable,
        )

    ask = staticmethod(ask_human)
    approve = staticmethod(ask_human_approval)
    pending = staticmethod(_pending)

    @staticmethod
    def tools(tools: Iterable[Any], **kwargs: Any) -> list[Any]:
        """Wrap several tools with the same approval policy."""
        return [Human(tool, **kwargs) for tool in tools]


def _wrap_human(
    tool: Any,
    *,
    when: Callable[..., bool] | None,
    message: str | None,
    timeout: float,
    already_durable: bool,
) -> Any:
    tool_cls = _dspy_tool_type()
    if tool_cls is not None and isinstance(tool, tool_cls):
        import copy

        wrapped = _approval_wrapper(
            tool.func,
            tool_name(tool),
            when=when,
            message=message,
            timeout=timeout,
            already_durable=already_durable,
        )
        result = copy.copy(tool)
        result.func = wrapped
        return result

    if not callable(tool):
        raise TypeError(f"expected a callable or dspy.Tool, got {type(tool).__name__}")
    return _approval_wrapper(
        tool,
        tool_name(tool),
        when=when,
        message=message,
        timeout=timeout,
        already_durable=already_durable,
    )


def _dspy_tool_type() -> type[Any] | None:
    try:
        from dspy import Tool
    except ImportError:  # pragma: no cover - depends on the environment
        return None
    return Tool


def _approval_wrapper(
    fn: Callable[..., Any],
    name: str,
    *,
    when: Callable[..., bool] | None,
    message: str | None,
    timeout: float,
    already_durable: bool,
) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if when is not None and not _matches(when, fn, args, kwargs):
            return _execute(fn, args, kwargs, name, already_durable)
        prompt = message or f"The agent wants to run {name}."
        if in_workflow_body():
            request, response = _wait_for_human(
                "approval",
                EventType.APPROVAL_REQUESTED,
                prompt,
                tool=name,
                arguments=dict(kwargs),
                timeout=timeout,
            )
            approved = bool(response is not None and response.get("approved"))
            if not approved:
                return _approval_rejection(response, None)
            return _execute(fn, args, kwargs, name, already_durable)
        request = _request(
            "approval",
            EventType.APPROVAL_REQUESTED,
            prompt,
            tool=name,
            arguments=dict(kwargs),
        )
        if not _confirm(prompt, request):
            return _approval_rejection(request, None)
        return _execute(fn, args, kwargs, name, already_durable)

    vars(wrapper)["__human__"] = {
        "name": name,
        "when": when,
        "message": message,
        "timeout": timeout,
    }
    vars(wrapper)["__durable_tool__"] = True
    vars(wrapper)["__durable_name__"] = name
    return wrapper


def _execute(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    name: str,
    already_durable: bool,
) -> Any:
    if already_durable:
        return fn(*args, **kwargs)
    return call_tool(fn, *args, name=name, **kwargs)


def _matches(
    predicate: Callable[..., bool],
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> bool:
    """Call an approval predicate the same way the tool itself is called."""
    try:
        signature = inspect.signature(fn)
        bound = signature.bind_partial(*args, **kwargs)
        return bool(predicate(*bound.args, **bound.kwargs))
    except TypeError:
        return bool(predicate(*args, **kwargs))
