"""Argument and command definitions for explicit CLI tools."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

_MISSING = object()
_AUTO_POSITIONAL = -1


@dataclass(frozen=True, eq=False, kw_only=True)
class Arg:
    """One argument of an explicitly defined :class:`Command`.

    Set exactly one of ``flag`` or ``positional``. A missing ``default`` makes
    the argument required, except for boolean and repeatable flags, which
    default to off and empty respectively.
    """

    flag: str | None = None
    positional: int | None = None
    type: Any = str
    default: Any = _MISSING
    help: str = ""
    choices: tuple[str, ...] = ()
    repeatable: bool = False


@dataclass(frozen=True, eq=False, kw_only=True)
class Command:
    """One explicitly defined CLI command exposed as a ``dspy.Tool``.

    ``command`` is the command path after the executable, either a tuple such
    as ``("pr", "list")`` or a space-separated string such as ``"pr list"``.
    Colons stay inside one token (``"db:migrate"`` is one command).
    ``name`` is the tool name before the CLI's ``name_prefix``. ``args`` maps
    tool argument names to argument definitions; use :func:`Argument` and
    :func:`Option`. ``fixed`` arguments are always
    appended before user flags.
    """

    command: str | Sequence[str]
    name: str
    description: str = ""
    args: dict[str, Arg] = field(default_factory=dict)
    fixed: tuple[str, ...] = ()
    ok_codes: frozenset[int] = frozenset()


def Argument(
    default: Any = _MISSING,
    *,
    help: str = "",
    type: Any = str,
    choices: Sequence[str] = (),
    repeatable: bool = False,
) -> Arg:
    """Define a positional argument for an explicit :class:`Command`.

    Positional indices are assigned automatically in ``Command.args`` order.
    ``Argument(...)`` (Ellipsis) marks a required argument, matching the
    usecli/Typer convention.
    """
    return Arg(
        positional=_AUTO_POSITIONAL,
        type=type,
        default=_MISSING if default is Ellipsis else default,
        help=help,
        choices=tuple(choices),
        repeatable=repeatable,
    )


def Option(
    *flags: Any,
    default: Any = _MISSING,
    help: str = "",
    type: Any = str,
    choices: Sequence[str] = (),
    repeatable: bool = False,
    is_flag: bool = False,
) -> Arg:
    """Define an option for an explicit :class:`Command`.

    The last long flag is used in argv; aliases are accepted for readability.
    ``is_flag=True`` or ``type=bool`` makes a boolean switch that defaults to
    off. The usecli form ``Option(default, "--flag")`` is accepted as well:
    ``Option(..., "--flag")`` marks a required option.
    """
    if flags and (not isinstance(flags[0], str) or not flags[0].startswith("-")):
        if default is not _MISSING:
            raise ValueError("Option default was given twice")
        default = flags[0]
        flags = flags[1:]
    if not flags:
        raise ValueError("Option requires at least one flag")
    for flag in flags:
        if not isinstance(flag, str) or not flag.startswith("-"):
            raise ValueError(f"Option flag must start with '-': {flag!r}")
    primary = next(
        (flag for flag in reversed(flags) if flag.startswith("--")), flags[-1]
    )
    kind = bool if is_flag else type
    if is_flag and default is _MISSING:
        default = False
    return Arg(
        flag=primary,
        type=kind,
        default=_MISSING if default is Ellipsis else default,
        help=help,
        choices=tuple(choices),
        repeatable=repeatable,
    )
