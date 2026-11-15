"""Adapters between MCP SDK tool objects and DSPy tools."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from ..utils.dspy import sync_tool_class

if TYPE_CHECKING:
    from dspy import Tool


def _accepted_kwargs(func: Any, **kwargs: Any) -> dict[str, Any]:
    """Drop keyword arguments the installed MCP SDK version does not accept."""
    parameters = inspect.signature(func).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return kwargs
    return {name: value for name, value in kwargs.items() if name in parameters}


def _zero_arg_factory(factory: Any) -> Any:
    """Adapt a zero-argument client factory to the SDK's keyword convention."""
    return lambda **_kwargs: factory()


def _mcp_signature(
    tool: Any,
    args: dict[str, Any],
    arg_types: dict[str, Any],
) -> inspect.Signature:
    """Build a real Python signature for an MCP tool's arguments.

    ``Tool.from_mcp_tool`` wraps every tool in ``async def func(*args,
    **kwargs)``, so anything introspecting the callable (``dspy.RLM``'s
    interpreter registers sandbox tools from ``inspect.signature``) sees a
    generic ``(args, kwargs)`` wrapper instead of the tool's parameters.
    """
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        schema = {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())

    # Required parameters come first so the signature stays valid Python when
    # optional parameters carry defaults.
    ordered = [name for name in args if name in required]
    ordered += [name for name in args if name not in required]

    parameters = []
    for name in ordered:
        parameter = inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=arg_types.get(name, Any),
        )
        if name not in required:
            prop = properties.get(name)
            parameter = parameter.replace(
                default=prop.get("default") if isinstance(prop, dict) else None
            )
        parameters.append(parameter)
    return inspect.Signature(parameters)


def _as_sync_capable(
    tool: Tool,
    signature: inspect.Signature | None = None,
    *,
    func: Callable[..., Any] | None = None,
) -> Tool:
    """Rebuild an MCP tool so its synchronous path also works."""
    cls = sync_tool_class(drop_nulls=True)
    call = tool.func if func is None else func
    if signature is not None:
        cast(Any, call).__signature__ = signature
    return cls(
        func=call,
        name=tool.name,
        desc=tool.desc,
        args=tool.args,
        arg_types=tool.arg_types,
        arg_desc=tool.arg_desc,
    )


def _error_payload(value: Any) -> bool:
    """Whether a tool result carries the usual MCP error shape.

    Servers normally report a failed call with ``isError``, which DSPy turns
    into an exception. Some servers instead answer with an ordinary payload —
    typically ``{"error": ...}`` or ``{"ok": false}`` — while a temporary
    condition (startup work, an index rebuild) clears. Structured results
    arrive as mappings, text results as JSON strings.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return False
        try:
            value = json.loads(text)
        except ValueError:
            return False
    if isinstance(value, dict):
        return bool(value.get("error")) or value.get("ok") is False
    if isinstance(value, list | tuple):
        return any(_error_payload(item) for item in value)
    return False


def _read_only(tool: Any) -> bool:
    """Whether the server annotates ``tool`` as free of side effects."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return False
    if isinstance(annotations, dict):
        value = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    else:
        value = getattr(annotations, "read_only_hint", None)
        if value is None:
            value = getattr(annotations, "readOnlyHint", None)
    return bool(value)
