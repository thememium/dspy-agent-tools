"""Checkpoint arbitrary tools as DBOS steps."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import inspect
import threading
from collections.abc import Callable
from typing import Any

from .context import (
    emit,
    in_workflow_body,
    next_operation_id,
    operation_scope,
)
from .events import EventType


def tool_name(target: Any) -> str:
    """Return the name a tool is exposed under."""
    name = getattr(target, "name", None)
    if isinstance(name, str) and name:
        return name
    name = getattr(target, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return type(target).__name__


def summarize(value: Any, *, limit: int = 500) -> Any:
    """Reduce a value to JSON-ish data for event streams."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "..."
    if isinstance(value, (list, tuple)):
        return [summarize(item, limit=limit) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key): summarize(item, limit=limit)
            for key, item in list(value.items())[:20]
        }
    to_dict = getattr(value, "toDict", None)
    if callable(to_dict):
        try:
            return summarize(to_dict(), limit=limit)
        except Exception:  # noqa: BLE001 - fall back to the type name
            return f"<{type(value).__name__}>"
    return f"<{type(value).__name__}>"


def run_coroutine(coro: Any) -> Any:
    """Run a coroutine from synchronous code, even inside a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[Any] = []
    error: list[BaseException] = []

    def target() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            error.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def _step_options(name: str, retries: int) -> Any:
    from dbos import StepOptions

    return StepOptions(
        name=name,
        retries_allowed=retries > 0,
        max_attempts=retries + 1,
    )


_TOOL_CHECKPOINT = "__durable_tool_checkpoint__"


def checkpoint_value(value: Any) -> Any:
    """Return *value* when DBOS can serialize it, otherwise a safe summary."""
    try:
        from .serializer import _dumps

        _dumps(value)
    except Exception:  # noqa: BLE001 - capture the readable shape instead
        return summarize(value)
    return value


def jsonable(value: Any) -> Any:
    """Render *value* as plain data the DBOS viewer can decode.

    Inputs and raw model responses are diagnostic payloads: replay never reads
    them, so converting DSPy objects (History, Tool, Prediction) to plain
    fields is safe and keeps the step output inspectable instead of pickled.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    for attribute in ("toDict", "model_dump", "dict"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                converted = method()
            except Exception:  # noqa: BLE001 - try the next representation
                converted = None
            if converted is not None and converted is not value:
                return jsonable(converted)
    if dataclasses.is_dataclass(value):
        return jsonable(dataclasses.asdict(value))
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return f"<{type(value).__name__}:{name}>"
    return f"<{type(value).__name__}>"


def _tool_checkpoint(tool: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    return {
        _TOOL_CHECKPOINT: True,
        "tool": tool,
        "input": {
            "args": jsonable(args),
            "kwargs": jsonable(kwargs),
        },
    }


def call_tool(
    fn: Callable[..., Any],
    *args: Any,
    name: str | None = None,
    retries: int = 0,
    **kwargs: Any,
) -> Any:
    """Execute *fn*, checkpointing it when a durable workflow is active."""
    if not in_workflow_body():
        result = fn(*args, **kwargs)
        return run_coroutine(result) if inspect.iscoroutine(result) else result

    step_name = name or tool_name(fn)
    operation_id = next_operation_id()
    input_data = {"args": args, "kwargs": kwargs}
    emit(
        EventType.TOOL_STARTED,
        tool=step_name,
        input=summarize(input_data),
        arguments=summarize(kwargs),
    )
    try:
        if inspect.iscoroutinefunction(fn):

            async def body() -> Any:
                with operation_scope(operation_id):
                    output = await fn(*args, **kwargs)
                return {**_tool_checkpoint(step_name, args, kwargs), "output": output}

            result = _run_async_step(step_name, body, retries)
        else:

            def body() -> Any:
                with operation_scope(operation_id):
                    output = fn(*args, **kwargs)
                return {**_tool_checkpoint(step_name, args, kwargs), "output": output}

            result = _run_sync_step(step_name, body, retries)
    except BaseException as exc:
        emit(
            EventType.TOOL_FAILED,
            tool=step_name,
            input=summarize(input_data),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    if isinstance(result, dict) and result.get(_TOOL_CHECKPOINT) is True:
        result = result["output"]
    emit(EventType.TOOL_COMPLETED, tool=step_name, output=summarize(result))
    return result


def _run_sync_step(name: str, body: Callable[[], Any], retries: int) -> Any:
    from dbos import DBOS

    return DBOS.run_step(_step_options(name, retries), body)


def _run_async_step(name: str, body: Callable[[], Any], retries: int) -> Any:
    from dbos import DBOS

    return DBOS.run_step(_step_options(name, retries), body)


def _wrap_callable(
    fn: Callable[..., Any],
    *,
    name: str | None = None,
    retries: int = 0,
) -> Callable[..., Any]:
    exposed = name or tool_name(fn)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return call_tool(fn, *args, name=exposed, retries=retries, **kwargs)

    vars(wrapper)["__durable_tool__"] = True
    vars(wrapper)["__durable_name__"] = exposed
    return wrapper


def _dspy_tool_class() -> type[Any] | None:
    try:
        from dspy import Tool
    except ImportError:  # pragma: no cover - depends on the environment
        return None
    return Tool


def wrap_tool(
    target: Any,
    *,
    name: str | None = None,
    retries: int = 0,
    approval: bool | Callable[..., bool] | None = None,
    when: Callable[..., bool] | None = None,
    message: str | None = None,
    timeout: float = 86_400,
    idempotent: bool = True,
) -> Any:
    """Return a durable replacement for a function or ``dspy.Tool``.

    ``approval`` (or ``when``) installs a :class:`~dspy_agent_tools.Human`
    gate before the checkpointed call. Metadata, signatures, and docstrings are
    preserved by ``functools.wraps`` or by copying the ``dspy.Tool``.
    """
    if getattr(target, "__human__", None) is not None:
        return target
    if is_durable_tool(getattr(target, "func", None)):
        return target

    from .human import Human

    policy = when
    if approval is True:

        def policy(*_args: Any, **_kwargs: Any) -> bool:
            return True
    elif callable(approval):
        policy = approval

    tool_cls = _dspy_tool_class()
    is_tool = tool_cls is not None and isinstance(target, tool_cls)
    exposed = name or tool_name(target)

    if is_tool:
        wrapped = functools.wraps(target.func)(
            lambda *args, **kwargs: call_tool(
                target.func,
                *args,
                name=exposed,
                retries=0 if not idempotent else retries,
                **kwargs,
            )
        )
        vars(wrapped)["__durable_tool__"] = True
        vars(wrapped)["__durable_name__"] = exposed
        if policy is not None:
            wrapped = Human(
                type(target)(
                    wrapped,
                    name=target.name,
                    desc=target.desc,
                    args=target.args,
                    arg_types=target.arg_types,
                ),
                when=policy,
                message=message,
                timeout=timeout,
            ).func
        import copy

        result = copy.copy(target)
        result.func = wrapped
        return result

    if not callable(target):
        raise TypeError(
            f"expected a callable or dspy.Tool, got {type(target).__name__}"
        )

    wrapped_fn = _wrap_callable(
        target, name=exposed, retries=0 if not idempotent else retries
    )
    if policy is not None:
        wrapped_fn = Human(
            wrapped_fn,
            when=policy,
            message=message,
            timeout=timeout,
            _already_durable=True,
        )
    return wrapped_fn


def is_durable_tool(value: Any) -> bool:
    """Return whether *value* is already checkpointed."""
    return bool(getattr(value, "__durable_tool__", False))
