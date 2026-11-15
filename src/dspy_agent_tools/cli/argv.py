"""Turn explicit command definitions into signatures and argv lists."""

from __future__ import annotations

import inspect
import shlex
import types
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from .types import _AUTO_POSITIONAL, _MISSING, Arg, Command

_SCHEMA_TYPES: dict[type[Any], str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _normalize_args(args: Any) -> list[str]:
    """Accept the list from DSPy, or a string when called directly."""
    if args is None:
        return []
    if isinstance(args, str):
        return shlex.split(args)
    return [str(item) for item in args]


def _effective_default(arg: Arg) -> Any:
    """Resolve the default used for schema, signature, and argv building."""
    if arg.default is not _MISSING:
        return arg.default
    if arg.flag is not None and arg.type is bool:
        return False
    if arg.flag is not None and arg.repeatable:
        return ()
    return _MISSING


def _validate_arg(
    name: str, arg: Arg, positionals: dict[int, str], flags: set[str]
) -> None:
    """Validate one explicit argument definition."""
    if not name.isidentifier():
        raise ValueError(f"Argument name {name!r} must be a valid identifier")
    if (arg.flag is None) == (arg.positional is None):
        raise ValueError(f"{name}: set exactly one of flag= or positional=")
    if arg.type not in _SCHEMA_TYPES:
        raise ValueError(f"{name}: unsupported type {arg.type!r}")
    if arg.choices and arg.type is not str:
        raise ValueError(f"{name}: choices require type=str")
    if (
        arg.repeatable
        and arg.default is not _MISSING
        and arg.default is not None
        and not isinstance(arg.default, (list, tuple, set, frozenset))
    ):
        raise ValueError(f"{name}: repeatable defaults must be a list or tuple")
    if arg.flag is not None:
        if not arg.flag.startswith("-"):
            raise ValueError(f"{name}: flag must start with '-'")
        if arg.flag in flags:
            raise ValueError(f"{name}: duplicate flag {arg.flag!r}")
        flags.add(arg.flag)
        return
    if arg.positional is None or arg.positional < 0:
        raise ValueError(f"{name}: positional must be a non-negative index")
    if arg.positional in positionals:
        raise ValueError(f"{name}: duplicate positional index {arg.positional}")
    positionals[arg.positional] = name


def _command_parts(command: str | Sequence[str]) -> tuple[str, ...]:
    """Split a space-separated signature; colons stay inside one token."""
    if isinstance(command, str):
        return tuple(part for part in command.split() if part)
    return tuple(command)


def _assign_positionals(args: dict[str, Arg]) -> dict[str, Arg]:
    """Turn ``Argument()`` markers into concrete positional indices."""
    result: dict[str, Arg] = {}
    next_index = 0
    for name, arg in args.items():
        if arg.positional == _AUTO_POSITIONAL:
            result[name] = replace(arg, positional=next_index)
            next_index += 1
        else:
            result[name] = arg
            if arg.positional is not None and arg.positional >= next_index:
                next_index = arg.positional + 1
    return result


def _normalize_command(command: Command) -> Command:
    """Resolve path strings and automatic positional indices once."""
    fixed = command.fixed if isinstance(command.fixed, str) else tuple(command.fixed)
    return replace(
        command,
        command=_command_parts(command.command),
        args=_assign_positionals(command.args),
        fixed=fixed,
    )


def _validate_command(command: Command) -> None:
    """Validate one explicit command, including positional ordering."""
    if not command.name or not command.name.isidentifier():
        raise ValueError(f"Command name {command.name!r} must be a valid identifier")
    if isinstance(command.fixed, (str, bytes)):
        raise TypeError(f"{command.name}: fixed must be a sequence of strings")
    for part in command.command:
        if not isinstance(part, str) or not part:
            raise ValueError(
                f"{command.name}: command path parts must be non-empty strings"
            )
    for item in command.fixed:
        if not isinstance(item, str):
            raise TypeError(f"{command.name}: fixed arguments must be strings")

    positionals: dict[int, str] = {}
    flags: set[str] = set()
    for name, arg in command.args.items():
        _validate_arg(name, arg, positionals, flags)

    if not positionals:
        return
    ordered = sorted(positionals)
    if ordered != list(range(len(ordered))):
        raise ValueError(
            f"{command.name}: positional indices must be contiguous from 0"
        )
    seen_optional = False
    for index in ordered:
        arg = command.args[positionals[index]]
        optional = _effective_default(arg) is not _MISSING
        if seen_optional and not optional:
            raise ValueError(
                f"{command.name}: required positional {positionals[index]!r} "
                "cannot follow an optional positional"
            )
        seen_optional = seen_optional or optional
        if arg.repeatable and index != ordered[-1]:
            raise ValueError(
                f"{command.name}: repeatable positional {positionals[index]!r} "
                "must be last"
            )


def _annotation(arg: Arg) -> Any:
    """Return the Python annotation for one explicit argument."""
    return types.GenericAlias(list, arg.type) if arg.repeatable else arg.type


def _arg_schema(arg: Arg) -> dict[str, Any]:
    """Return the JSON schema for one explicit argument."""
    item: dict[str, Any] = {"type": _SCHEMA_TYPES[arg.type]}
    if arg.choices:
        item["enum"] = list(arg.choices)
    schema = {"type": "array", "items": item} if arg.repeatable else item
    default = _effective_default(arg)
    if default is not _MISSING:
        if arg.repeatable and default is not None:
            schema["default"] = list(default)
        else:
            schema["default"] = default
    return schema


def _command_signature(command: Command) -> inspect.Signature:
    """Build a real signature so ``dspy.RLM`` sees explicit parameters."""
    required = [
        (name, arg)
        for name, arg in command.args.items()
        if _effective_default(arg) is _MISSING
    ]
    optional = [
        (name, arg)
        for name, arg in command.args.items()
        if _effective_default(arg) is not _MISSING
    ]
    parameters = []
    for name, arg in [*required, *optional]:
        default = _effective_default(arg)
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=_annotation(arg),
                default=(inspect.Parameter.empty if default is _MISSING else default),
            )
        )
    return inspect.Signature(parameters)


def _stringify(name: str, arg: Arg, value: Any) -> str:
    text = str(value)
    if arg.choices and text not in arg.choices:
        raise ValueError(f"{name} must be one of {arg.choices}, got {text!r}")
    return text


def _command_argv(command: Command, values: dict[str, Any]) -> list[str]:
    """Turn tool keyword arguments into argv items."""
    positionals: dict[int, list[str]] = {}
    flags: list[str] = []
    for name, arg in command.args.items():
        value = values.get(name, _effective_default(arg))
        if value is _MISSING:
            raise ValueError(f"{name} is required")
        if value is None:
            continue
        if arg.positional is not None:
            items = (
                value
                if arg.repeatable and isinstance(value, (list, tuple, set, frozenset))
                else [value]
            )
            positionals[arg.positional] = [
                _stringify(name, arg, item) for item in items
            ]
            continue

        assert arg.flag is not None
        if arg.type is bool:
            if value:
                flags.append(arg.flag)
            continue
        items = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
        if arg.repeatable or isinstance(value, (list, tuple, set, frozenset)):
            for item in items:
                flags.extend([arg.flag, _stringify(name, arg, item)])
        else:
            flags.extend([arg.flag, _stringify(name, arg, value)])

    ordered = sorted(positionals)
    if ordered != list(range(len(ordered))):
        raise ValueError(
            f"{command.name}: positional arguments cannot be skipped; "
            "supply them in order"
        )
    positional_values = [item for index in ordered for item in positionals[index]]
    return [*positional_values, *command.fixed, *flags]
