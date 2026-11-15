"""Adapters between MCP SDK tool objects and DSPy tools."""

from __future__ import annotations

import inspect
import json
import keyword
from collections.abc import Callable
from copy import copy
from typing import TYPE_CHECKING, Any, cast

from ..utils.dspy import sync_tool_class

if TYPE_CHECKING:
    from dspy import Tool


def _dspy_compatible(tool: Any) -> Any:
    """Express union types as compositions for DSPy's MCP schema converter.

    DSPy 3.4.1 indexes its Python type map with each property's ``type``;
    JSON Schema's valid array form (e.g. ``["string", "null"]``) is unhashable.
    An equivalent ``allOf`` leaves validation constraints intact and avoids
    that lookup. Keep the server's original schema for the catalog.
    """

    def normalize(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            key: item
            if key in ("default", "const", "enum", "examples")
            else normalize(item)
            for key, item in value.items()
        }
        types = result.get("type")
        if isinstance(types, list):
            result.pop("type")
            result.setdefault("allOf", []).append(
                {"anyOf": [{"type": item} for item in types]}
            )
        return result

    schema = getattr(tool, "input_schema", {})
    compatible = copy(tool)
    normalized = normalize(schema)
    required = set(normalized.get("required") or ())
    for name, spec in (normalized.get("properties") or {}).items():
        if name in required:
            spec.pop("default", None)
        else:
            spec.setdefault("default", None)
    compatible.input_schema = normalized
    return compatible


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
    extra_names = False
    for name in ordered:
        if not name.isidentifier() or keyword.iskeyword(name):
            extra_names = True
            continue
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
    if extra_names:
        rest = "kwargs"
        while rest in args:
            rest = "_" + rest
        parameters.append(inspect.Parameter(rest, inspect.Parameter.VAR_KEYWORD))
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
    args = {name: dict(spec) for name, spec in (tool.args or {}).items()}
    if signature is not None:
        for name, parameter in signature.parameters.items():
            if name not in args:
                continue
            if parameter.default is inspect.Parameter.empty:
                args[name].pop("default", None)
            else:
                args[name].setdefault("default", parameter.default)
    return cls(
        func=call,
        name=tool.name,
        desc=tool.desc,
        args=args,
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
