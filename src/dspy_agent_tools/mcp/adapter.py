"""Adapters between MCP SDK tool objects and DSPy tools."""

from __future__ import annotations

import functools
import inspect
import json
import keyword
import re
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


def _python_parameter(name: str, taken: set[str]) -> str:
    """A legal Python parameter name for ``name``, unique within ``taken``."""
    safe = re.sub(r"\W", "_", name)
    if not safe or safe[0].isdigit():
        safe = f"_{safe}"
    if keyword.iskeyword(safe):
        safe = f"{safe}_"
    while safe in taken or keyword.iskeyword(safe):
        safe = f"{safe}_"
    taken.add(safe)
    return safe


def _mcp_signature(
    tool: Any,
    args: dict[str, Any],
    arg_types: dict[str, Any],
) -> tuple[inspect.Signature, dict[str, str]]:
    """Build a real Python signature for an MCP tool's arguments.

    ``Tool.from_mcp_tool`` wraps every tool in ``async def func(*args,
    **kwargs)``, so anything introspecting the callable (``dspy.RLM``'s
    interpreter registers sandbox tools from ``inspect.signature``) sees a
    generic ``(args, kwargs)`` wrapper instead of the tool's parameters.

    Names that Python cannot accept as parameters, such as ``from`` or
    ``repo-name``, are renamed rather than hidden behind ``**kwargs``: dspy's
    sandbox renders a variadic catch-all as a positional parameter and fails to
    compile the tool. The returned mapping renames each exposed name back to
    the server's argument name, so the callable can still forward it.
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
    renames: dict[str, str] = {}
    taken: set[str] = set()
    for name in ordered:
        exposed = _python_parameter(name, taken)
        if exposed != name:
            renames[exposed] = name
        parameter = inspect.Parameter(
            exposed,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=arg_types.get(name, Any),
        )
        if name not in required:
            prop = properties.get(name)
            parameter = parameter.replace(
                default=prop.get("default") if isinstance(prop, dict) else None
            )
        parameters.append(parameter)
    return inspect.Signature(parameters), renames


def _renamed(func: Callable[..., Any], renames: dict[str, str]) -> Callable[..., Any]:
    """``func`` taking the signature's names and forwarding the server's names."""
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_call(*args: Any, **kwargs: Any) -> Any:
            return await func(
                *args,
                **{renames.get(name, name): value for name, value in kwargs.items()},
            )

        return async_call

    @functools.wraps(func)
    def call(*args: Any, **kwargs: Any) -> Any:
        return func(
            *args, **{renames.get(name, name): value for name, value in kwargs.items()}
        )

    return call


def _as_sync_capable(
    tool: Tool,
    signature: inspect.Signature | None = None,
    *,
    func: Callable[..., Any] | None = None,
    renames: dict[str, str] | None = None,
) -> Tool:
    """Rebuild an MCP tool so its synchronous path also works."""
    cls = sync_tool_class(drop_nulls=True)
    call = tool.func if func is None else func
    renames = renames or {}
    if renames:
        call = _renamed(call, renames)
    if signature is not None:
        cast(Any, call).__signature__ = signature

    reverse = {original: exposed for exposed, original in renames.items()}
    args = {
        reverse.get(name, name): dict(spec) for name, spec in (tool.args or {}).items()
    }
    if signature is not None:
        for name in args.keys() & signature.parameters.keys():
            parameter = signature.parameters[name]
            if parameter.default is inspect.Parameter.empty:
                args[name].pop("default", None)
            else:
                args[name].setdefault("default", parameter.default)
    arg_types = (
        {
            reverse.get(name, name): value
            for name, value in (tool.arg_types or {}).items()
        }
        if renames
        else tool.arg_types
    )
    arg_desc = (
        {reverse.get(name, name): desc for name, desc in tool.arg_desc.items()}
        if renames and tool.arg_desc
        else tool.arg_desc
    )
    return cls(
        func=call,
        name=tool.name,
        desc=tool.desc,
        args=args,
        arg_types=arg_types,
        arg_desc=arg_desc,
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
