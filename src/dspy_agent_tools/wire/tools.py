"""Wrap existing tools with policy, tracing, offloading, and logging.

A ``dspy.Tool`` is a name, a description, an argument schema, and a function.
Rewrapping one with the same four pieces and a different body is therefore
enough to give the tool-calling path (``dspy.ReAct`` / ``dspy.ReActV2``) the
same middleware the RLM hook path gets, without patching DSPy internals.

The wrapper keeps the original function's ``__module__`` so
:func:`dspy_agent_tools.patch.guide.family_of` still recognises which family
the tool belongs to and :func:`dspy_agent_tools.Patch` still injects the right
guidance section.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from ..utils.dspy import load_tool, sync_tool_class
from .observe import NOOP, span
from .offload import Offloader
from .policy import Policy, check
from .session import Session

if TYPE_CHECKING:
    from dspy import Tool

_DIGEST_LIMIT = 500


def _digest(value: Any) -> str:
    """A short, lossy rendering of a value for a span or a log row."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=repr)
    except (TypeError, ValueError):
        text = repr(value)
    return text[:_DIGEST_LIMIT]


def _is_async(func: Any) -> bool:
    """Whether ``func`` is a coroutine function, including an async __call__."""
    if inspect.iscoroutinefunction(func):
        return True
    return inspect.iscoroutinefunction(type(func).__call__)


def _finish(
    result: Any,
    *,
    name: str,
    offload: Offloader | None,
    session: Session | None,
    current: Any,
) -> Any:
    """Post-process one successful result."""
    if offload is not None:
        result = offload.spill(result, label=name)
    if isinstance(result, Mapping) and result.get("offloaded") is True:
        current.set_attribute("gen_ai.tool.result.chars", int(result["chars"]))
        current.set_attribute("gen_ai.tool.result.offloaded", True)
        payload = _digest(
            {
                "path": result["path"],
                "chars": result["chars"],
                "head": result["head"],
                "tail": result["tail"],
            }
        )
    else:
        text = _digest(result)
        current.set_attribute("gen_ai.tool.result.chars", len(text))
        payload = text
    if session is not None:
        session.record(kind="tool", output=payload, data={"tool": name})
    return result


def _wrap_one(
    tool: Tool,
    *,
    policy: Policy | None,
    offload: Offloader | None,
    session: Session | None,
    trace: bool,
) -> Tool:
    """Return a tool with the same schema whose call runs the middleware."""
    tool_cls = sync_tool_class()
    original = tool.func
    name = str(tool.name or getattr(original, "__name__", "tool"))

    if _is_async(original):

        async def wrapper(**kwargs: Any) -> Any:
            attributes = {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
            }
            manager = span("execute_tool", attributes) if trace else nullcontext(NOOP)
            with manager as current:
                check(policy, name, kwargs)
                try:
                    result = await original(**kwargs)
                except Exception as exc:
                    if session is not None:
                        session.record(
                            kind="tool",
                            error=f"{type(exc).__name__}: {exc}",
                            data={"tool": name},
                        )
                    raise
                return _finish(
                    result,
                    name=name,
                    offload=offload,
                    session=session,
                    current=current,
                )

    else:

        def wrapper(**kwargs: Any) -> Any:
            attributes = {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
            }
            manager = span("execute_tool", attributes) if trace else nullcontext(NOOP)
            with manager as current:
                check(policy, name, kwargs)
                try:
                    result = original(**kwargs)
                except Exception as exc:
                    if session is not None:
                        session.record(
                            kind="tool",
                            error=f"{type(exc).__name__}: {exc}",
                            data={"tool": name},
                        )
                    raise
                return _finish(
                    result,
                    name=name,
                    offload=offload,
                    session=session,
                    current=current,
                )

    wrapper.__module__ = getattr(original, "__module__", "")
    wrapper.__name__ = getattr(original, "__name__", name)
    wrapper.__doc__ = getattr(original, "__doc__", None)
    return tool_cls(
        func=wrapper,
        name=name,
        desc=tool.desc,
        args=tool.args,
        arg_types=tool.arg_types,
        arg_desc=tool.arg_desc,
    )


def wrap_tools(
    tools: Iterable[Any] | Mapping[str, Any],
    *,
    policy: Policy | None = None,
    offload: Offloader | None = None,
    session: Session | None = None,
    trace: bool = True,
) -> list[Tool]:
    """Rewrap every tool with the given middleware.

    Accepts an agent's tool mapping or a sequence of tools and functions.
    A value that is already a ``dspy.Tool`` keeps its name, description, and
    schema; a plain function is converted first.

    Args:
        tools: An agent's ``tools`` mapping or any iterable of tools.
        policy: Deny a call by returning a reason string.
        offload: Spill oversized results to a file and return a handle.
        session: Record each call's result or error.
        trace: Emit an OTel span per call.

    Returns:
        A new list of tools. The originals are untouched.

    Example:
        >>> from dspy_agent_tools import Offloader, wrap_tools
        >>> tools = wrap_tools([bash_exec], offload=Offloader(limit=5000))
    """
    ToolClass = load_tool(
        "wrap_tools() requires DSPy. Install it with: uv add 'dspy-agent-tools[mcp]'"
    )
    items = list(tools.values()) if isinstance(tools, Mapping) else list(tools)
    wrapped: list[Tool] = []
    for item in items:
        tool = item if isinstance(item, ToolClass) else ToolClass(item)
        wrapped.append(
            _wrap_one(
                tool,
                policy=policy,
                offload=offload,
                session=session,
                trace=trace,
            )
        )
    return wrapped


__all__ = ["wrap_tools"]
