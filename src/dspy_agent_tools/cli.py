"""Turn any executable into a lazily discovered set of ``dspy.Tool`` objects.

Unlike MCP, a plain command-line program has no machine-readable protocol for
its commands or arguments. The only schema it exposes is its own help output.
``CLI`` therefore returns a three-tool discovery facade instead of one tool per
command:

- ``search_commands`` finds command paths by name and description.
- ``get_command_help`` returns the CLI's real help: usage, subcommands,
  parsed flags, and the raw help text.
- ``run_command`` executes one command path with an argv list.

Discovery happens on demand: the root help is fetched on the first tool call,
each branch is expanded only when a search or lookup reaches it, and every
result is cached. No shell is involved; every invocation is
``[executable, *base_args, *command_path, *args]`` built and run through
plumbum, so the configured executable is the only reachable program.

The facade mirrors :class:`dspy_agent_tools.Toolbox`'s progressive-discovery
modes. ``mode="search"`` returns text for ``dspy.ReAct``; ``mode="code"``
returns structured dictionaries for ``dspy.RLM``. The parsed flag table is
best-effort; the raw help text remains authoritative.

Example:
    >>> from dspy_agent_tools import CLI
    >>> gh = CLI("gh")
    >>> agent = dspy.ReAct("question -> answer", tools=gh)
"""

from __future__ import annotations

import inspect
import re
import shlex
import threading
import types
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from plumbum import CommandNotFound, local

from .bash import DEFAULT_TIMEOUT, _execute, _render, _resolve_cwd

if TYPE_CHECKING:
    from dspy import Tool

DEFAULT_HELP_TIMEOUT = 10.0
DEFAULT_MAX_EXPANDS = 8
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_HELP_CHARS = 20_000

_MODES = ("search", "code")
_MISSING = object()
_AUTO_POSITIONAL = -1
_SCHEMA_TYPES: dict[type[Any], str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


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
    tool argument names to :class:`Arg` definitions; use :func:`Argument` and
    :func:`Option` for the common cases. ``fixed`` arguments are always
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


# Discovery must never open a pager or wait for a terminal.
_HELP_ENV = {
    "PAGER": "cat",
    "MANPAGER": "cat",
    "GIT_PAGER": "cat",
    "GH_PAGER": "cat",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "100",
}

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_OVERSTRIKE = re.compile(r".\x08")
_MAN_HEADER = re.compile(r"^[A-Z][A-Z0-9-]+\(\d+\)\s*$", re.MULTILINE)
_HEADER = re.compile(r"^\s*([A-Z][A-Za-z0-9 _/()-]{1,60}):\s*$")
_ALLCAPS = re.compile(r"^\s*([A-Z][A-Z0-9 _/()-]{1,60})\s*$")
_COMMAND = re.compile(
    r"^\s{2,}(?P<name>[A-Za-z0-9][\w.:-]*)(?:,\s*[A-Za-z0-9][\w-]*)?"
    r"\s{2,}(?P<desc>\S.*)$"
)
_FLAG = re.compile(r"^\s+(?P<head>-.*?)(?:(?:\s{2,}|\t+)(?P<desc>\S.*))?$")
_OPTIONAL_VALUE = re.compile(r"\[=(<[^>]+>|[^\]]*)\]$")
_DEFAULT = re.compile(r"\((?:default|Default):?\s*([^)]+)\)")
_POSSIBLE = re.compile(r"\[possible values:\s*([^\]]+)\]", re.IGNORECASE)
_BRACED_CHOICES = re.compile(r"\{([^{}]+)\}")
_ONE_OF = re.compile(r"One of:\s*\(([^)]+)\)", re.IGNORECASE)
_PIPE_CHOICES = re.compile(r"\((\w+(?:\|\w+)+)\)")
_USAGE = re.compile(r"^\s*usage:\s*(.*)$", re.IGNORECASE)
_USAGE_HEADER = re.compile(r"^\s*USAGE\s*$")
_OR_LINE = re.compile(r"^\s*or:", re.IGNORECASE)

_META_COMMANDS = {
    "__complete",
    "__completeNoDesc",
    "completion",
    "completions",
    "help",
    "help-all",
    "version",
}

_INT_TYPES = {
    "int",
    "int32",
    "int64",
    "integer",
    "number",
    "float",
    "port",
    "uint",
    "uint16",
    "uint32",
    "uint64",
}
_ARRAY_TYPES = {"array", "list", "slice", "strings"}
_OBJECT_TYPES = {"map", "object"}

_SEARCH_DESC = (
    "Search this CLI's command tree. Returns matching command paths and "
    "one-line descriptions. Discovery is lazy: deeper commands are read from "
    "their help on demand and cached."
)
_DETAILS_DESC = (
    "Return this CLI's own help for one command path: usage, subcommands, "
    "parsed flags, and the raw help text. Call before run_command when the "
    "flags are unclear."
)
_RUN_DESC = (
    "Run one command path of this CLI with argv strings. command='' runs the "
    "executable itself, for CLIs without subcommands. args is a list such as "
    "['--limit', '5']; no shell is used. Returns output with the exit code "
    "and wall time."
)


def _load_tool() -> type[Tool]:
    """Import ``dspy.Tool`` on first use."""
    try:
        from dspy import Tool
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "dspy_agent_tools.CLI requires DSPy. "
            "Install it with: uv add 'dspy-agent-tools[mcp]'"
        ) from exc
    return Tool


def _clean(text: str) -> str:
    """Remove terminal escapes, carriage returns, and overstrike."""
    text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return _OVERSTRIKE.sub("", text)


def _is_man_page(text: str) -> bool:
    """Recognize a pager/man rendering that is not usable as help."""
    return "\x08" in text or "\n.TH " in text or bool(_MAN_HEADER.search(text))


def _sections(text: str) -> list[tuple[str | None, list[str]]]:
    """Split help into ``(header, body)`` blocks, preserving order."""
    sections: list[tuple[str | None, list[str]]] = []
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        match = _ALLCAPS.match(line) or _HEADER.match(line)
        if match:
            if body:
                sections.append((current, body))
            current = match.group(1).strip()
            body = []
        else:
            body.append(line)
    if body:
        sections.append((current, body))
    return sections


def _classify(header: str | None) -> str | None:
    if not header or len(header.split()) > 4:
        return None
    upper = header.upper()
    if "COMMAND" in upper:
        return "commands"
    if "OPTION" in upper or "FLAG" in upper:
        return "flags"
    return None


def _usage(text: str) -> str:
    """Extract the usage block from help text."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _USAGE.match(line)
        if match:
            block = [match.group(1).strip()] if match.group(1).strip() else []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if not stripped:
                    break
                if _HEADER.match(stripped) or _ALLCAPS.match(stripped):
                    break
                if following.startswith((" ", "\t")) or _OR_LINE.match(following):
                    block.append(stripped)
                else:
                    break
            return "\n".join(block)
        if _USAGE_HEADER.match(line):
            block = []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if not stripped:
                    break
                if _HEADER.match(stripped) or _ALLCAPS.match(stripped):
                    break
                block.append(stripped)
            return "\n".join(block)
    return ""


def _description(text: str) -> str:
    """Return the first prose line of a help page.

    Usage continuations, flag rows, and section headers are skipped. Unindented
    prose is preferred; indented prose is used only when nothing else exists.
    """
    candidates: list[str] = []
    in_usage = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            in_usage = False
            continue
        if _USAGE.match(stripped):
            in_usage = True
            continue
        if _USAGE_HEADER.match(line):
            in_usage = True
            continue
        if in_usage:
            if line.startswith((" ", "\t")):
                continue
            in_usage = False
        if _ALLCAPS.match(stripped) or stripped.startswith("-"):
            continue
        header = _HEADER.match(stripped)
        if (
            header
            and len(header.group(1).split()) <= 4
            and any(
                word in header.group(1).upper()
                for word in (
                    "ALIAS",
                    "ARGUMENT",
                    "COMMAND",
                    "ENVIRONMENT",
                    "EXAMPLE",
                    "FLAG",
                    "OPTION",
                    "USAGE",
                )
            )
        ):
            continue
        if line != line.lstrip():
            candidates.append(stripped)
            continue
        return stripped
    return candidates[0] if candidates else ""


def _choices(description: str) -> list[str] | None:
    match = _POSSIBLE.search(description)
    if match is None:
        match = _BRACED_CHOICES.search(description)
    if match is None:
        match = _ONE_OF.search(description)
    if match is None:
        match = _PIPE_CHOICES.search(description)
    if match is None:
        return None
    values = [
        value.strip().strip("'\"")
        for value in re.split(r"[,|]", match.group(1))
        if value.strip()
    ]
    return values or None


@dataclass(slots=True)
class _Flag:
    names: list[str]
    kind: str
    default: str | None
    choices: list[str] | None
    repeatable: bool
    description: str

    def record(self) -> dict[str, Any]:
        return {
            "names": list(self.names),
            "type": self.kind,
            "default": self.default,
            "choices": list(self.choices) if self.choices else None,
            "repeatable": self.repeatable,
            "description": self.description,
        }

    def line(self) -> str:
        meta = [self.kind]
        if self.choices:
            meta.append("choices: " + "|".join(self.choices))
        if self.default is not None:
            meta.append(f"default: {self.default}")
        if self.repeatable:
            meta.append("repeatable")
        description = f" — {self.description}" if self.description else ""
        return f"  {', '.join(self.names)} ({'; '.join(meta)}){description}"


def _parse_flag_head(head: str, description: str) -> _Flag | None:
    """Parse ``-x, --long TYPE`` plus a default and description."""
    head = head.strip().rstrip(":")
    if not head.startswith("-"):
        return None

    names: list[str] = []
    default: str | None = None
    value_spec: str | None = None
    repeatable = False
    for part in head.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split(None, 1)
        name = tokens[0]
        if len(tokens) > 1 and value_spec is None:
            value_spec = tokens[1].strip()

        optional = _OPTIONAL_VALUE.search(name)
        if optional:
            if value_spec is None and "<" in optional.group(1):
                value_spec = optional.group(1)
            name = name[: optional.start()]

        if name.startswith("--") and "=" in name:
            name, default = name.split("=", 1)
        if name.endswith("..."):
            name = name[:-3]
            repeatable = True
        name = name.replace("[no-]", "")
        if name.startswith("-"):
            names.append(name)

    if not names:  # pragma: no cover - defensive; the head must start with "-"
        return None

    if value_spec is not None:
        value_spec = value_spec.strip()
        if value_spec.endswith("..."):
            value_spec = value_spec[:-3]
            repeatable = True
        value_spec = value_spec.strip("<>[]")

    if default is None:
        match = _DEFAULT.search(description)
        if match:
            default = match.group(1).strip()

    # A bare ``--flag=VALUE`` line with no description is usually a usage
    # template, not an actual default (for example ``--engine=ENGINE``).
    if (
        default is not None
        and value_spec is None
        and not description.strip()
        and re.fullmatch(r"[A-Z][A-Z0-9+?_<>./-]*", default)
    ):
        value_spec = default
        default = None

    lowered = (value_spec or "").lower()
    kind = "boolean"
    if lowered:
        if lowered in _INT_TYPES:
            kind = "integer"
        elif lowered in _OBJECT_TYPES:
            kind = "object"
        elif lowered in _ARRAY_TYPES or repeatable:
            kind = "array"
        else:
            kind = "string"

    if default is not None:
        bare = default.strip().strip("'\"")
        if bare in {"true", "false"}:
            kind = "boolean"
        elif bare in {"[]", "[ ]"}:
            kind = "array"
        elif bare in {"{}", "map[]"}:
            kind = "object"
        elif bare == "" and kind == "boolean":
            kind = "string"
        elif re.fullmatch(r"-?\d+", bare) and kind == "boolean":
            kind = "integer"
        elif kind == "boolean":
            kind = "string"

    if lowered in _ARRAY_TYPES:
        repeatable = True

    return _Flag(
        names=names,
        kind=kind,
        default=default,
        choices=_choices(description),
        repeatable=repeatable,
        description=description.strip(),
    )


def _parse_flags(lines: Sequence[str], flags: dict[str, _Flag]) -> None:
    """Add flag rows from one section, joining wrapped descriptions."""
    pending: _Flag | None = None
    for line in lines:
        if not line.strip():
            pending = None
            continue
        match = _FLAG.match(line)
        if match:
            flag = _parse_flag_head(match.group("head"), match.group("desc") or "")
            if flag is not None:
                key = flag.names[-1]
                if key not in flags:
                    flags[key] = flag
                    pending = flag
                else:
                    pending = None
                continue
        if pending is not None and line.startswith((" ", "\t")):
            pending.description = f"{pending.description} {line.strip()}".strip()
        else:
            pending = None


def _parse_commands(lines: Sequence[str], commands: dict[str, str]) -> None:
    """Add two-column command rows from one section."""
    for line in lines:
        if not line.strip() or line.lstrip().startswith("-"):
            continue
        match = _COMMAND.match(line)
        if match is None:
            continue
        name = match.group("name").rstrip(":")
        if name.casefold() in _META_COMMANDS or name.startswith("_"):
            continue
        commands.setdefault(name, match.group("desc").strip())


def _parse_help(
    text: str, *, is_root: bool
) -> tuple[str, str, dict[str, str], dict[str, _Flag]]:
    """Extract usage, description, commands, and flags from one help page."""
    usage = _usage(text)
    description = _description(text)
    commands: dict[str, str] = {}
    flags: dict[str, _Flag] = {}
    for header, lines in _sections(text):
        kind = _classify(header)
        if kind == "commands":
            _parse_commands(lines, commands)
        elif kind == "flags":
            _parse_flags(lines, flags)
    if not flags:
        _parse_flags(text.splitlines(), flags)
    if (
        not commands
        and is_root
        and re.search(r"<command>|\[command\]|\bsubcommand", usage, re.IGNORECASE)
    ):
        _parse_commands(text.splitlines(), commands)
    return usage, description, commands, flags


@dataclass(slots=True)
class _Node:
    name: str
    path: tuple[str, ...]
    description: str = ""
    usage: str = ""
    help: str = ""
    flags: dict[str, _Flag] = field(default_factory=dict)
    children: dict[str, _Node] = field(default_factory=dict)
    expanded: bool = False

    def label(self) -> str:
        return " ".join(self.path) or "(root)"


def _run_argv(
    executable: str,
    base_args: Sequence[str],
    path: Sequence[str],
    args: Sequence[str],
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    timeout: float,
    ok_codes: frozenset[int] = frozenset(),
) -> str:
    """Execute one argv tail and render it like the bash tools."""
    argv = [executable, *base_args, *path, *args]
    try:
        output, notices, elapsed, _returncode = _execute(
            argv, cwd=cwd, timeout=timeout, env=env, ok_codes=ok_codes
        )
    except CommandNotFound:
        return f"[command not found: {executable}]"
    return _render(output, notices, elapsed)


class _Discovery:
    """Lazily expand one executable's help tree on a background-free cache."""

    def __init__(
        self,
        executable: str,
        *,
        base_args: Sequence[str],
        cwd: Path | None,
        env: dict[str, str] | None,
        timeout: float,
        help_timeout: float,
        max_expands: int,
        max_depth: int,
    ) -> None:
        self.executable = executable
        self.base_args = tuple(base_args)
        self.cwd = cwd
        self.env = dict(env or {})
        self.help_env = {**self.env, **_HELP_ENV}
        self.timeout = timeout
        self.help_timeout = help_timeout
        self.max_expands = max_expands
        self.max_depth = max_depth
        self._lock = threading.RLock()
        self.root = _Node("", ())
        self._nodes: dict[tuple[str, ...], _Node] = {(): self.root}

    def refresh(self) -> None:
        """Drop every cached help page and start from the root again."""
        with self._lock:
            self.root = _Node("", ())
            self._nodes = {(): self.root}

    def _help_text(self, path: tuple[str, ...]) -> tuple[str, int | None]:
        """Fetch help for a path, retrying ``-h`` after a man page or failure."""
        last: tuple[str, int | None] = ("", 1)
        for flag in ("--help", "-h"):
            try:
                output, _notices, _elapsed, returncode = _execute(
                    [self.executable, *self.base_args, *path, flag],
                    cwd=self.cwd,
                    timeout=self.help_timeout,
                    env=self.help_env,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as help text
                last = (str(exc), 1)
                continue
            if output.strip() and not _is_man_page(output):
                return _clean(output), returncode
            last = (output, returncode)
        return _clean(last[0]), last[1]

    def expand(self, path: tuple[str, ...]) -> _Node:
        """Fetch and parse one node's help exactly once."""
        with self._lock:
            node = self._nodes.get(path)
            if node is None:
                raise KeyError(" ".join(path))
            if node.expanded:
                return node
            text, returncode = self._help_text(path)
            node.help = text
            usage, description, commands, flags = _parse_help(text, is_root=not path)
            node.usage = usage
            node.flags = flags
            if not node.description and returncode == 0:
                node.description = description
            for name, child_description in commands.items():
                child_path = (*path, name)
                child = self._nodes.get(child_path)
                if child is None:
                    child = _Node(name, child_path)
                    self._nodes[child_path] = child
                if not child.description:
                    child.description = child_description
                node.children[name] = child
            node.expanded = True
            return node

    def nodes(self) -> list[_Node]:
        return [node for path, node in self._nodes.items() if path]

    @staticmethod
    def _score(node: _Node, tokens: list[str], query: str) -> float:
        path = " ".join(node.path).casefold()
        name = node.name.casefold()
        description = node.description.casefold()
        score = 0.0
        if path == query:
            score += 100
        if name == query:
            score += 60
        if all(token in path.split() for token in tokens):
            score += 50
        if path.startswith(query):
            score += 30
        for token in tokens:
            if name == token:
                score += 40
            elif name.startswith(token):
                score += 20
            elif token in name:
                score += 10
            if token in description:
                score += 5
        if score:
            score += 5 * len(node.path)
        return score

    def search(self, query: str, limit: int) -> list[_Node]:
        """Rank known commands, expanding the best candidates within budget."""
        query = query.strip().casefold()
        tokens = query.split()
        limit = max(1, min(int(limit), 50))
        with self._lock:
            self.expand(())
            if not self.root.children:
                return [self.root]
            if not tokens:
                return list(self.root.children.values())[:limit]

            expands = 1
            for _ in range(self.max_depth):
                progressed = False
                candidates = sorted(
                    self.nodes(),
                    key=lambda node: (
                        -self._score(node, tokens, query),
                        node.label(),
                    ),
                )
                for node in candidates:
                    if expands >= self.max_expands:
                        break
                    if node.expanded or self._score(node, tokens, query) <= 0:
                        continue
                    self.expand(node.path)
                    expands += 1
                    progressed = True
                if not progressed:
                    break

            ranked = sorted(
                ((self._score(node, tokens, query), node) for node in self.nodes()),
                key=lambda pair: (-pair[0], pair[1].label()),
            )
            return [node for score, node in ranked if score > 0][:limit]

    def resolve(self, command: str) -> _Node:
        """Resolve a whitespace-separated command path to its expanded node."""
        tokens = shlex.split(command) if command.strip() else []
        with self._lock:
            self.expand(())
            node = self.root
            for token in tokens:
                child = node.children.get(token)
                if child is None:
                    child = next(
                        (
                            candidate
                            for name, candidate in node.children.items()
                            if name.casefold() == token.casefold()
                        ),
                        None,
                    )
                if child is None:
                    self.expand(node.path)
                    child = node.children.get(token) or next(
                        (
                            candidate
                            for name, candidate in node.children.items()
                            if name.casefold() == token.casefold()
                        ),
                        None,
                    )
                if child is None:
                    known = ", ".join(list(node.children)[:8]) or "none"
                    raise ValueError(
                        f"Unknown command {token!r} under "
                        f"{node.label()!r}; call search_commands "
                        f"first. Known commands: {known}."
                    )
                node = child
            if not node.expanded:
                self.expand(node.path)
            return node

    def run(self, command: str, args: Sequence[str]) -> str:
        """Resolve ``command`` and execute it with an argv tail."""
        node = self.resolve(command)
        return _run_argv(
            self.executable,
            self.base_args,
            node.path,
            [str(item) for item in args],
            cwd=self.cwd,
            env=self.env,
            timeout=self.timeout,
        )


def _search_text(nodes: Sequence[_Node]) -> str:
    if not nodes:
        return "[no matching commands]"
    lines = []
    for index, node in enumerate(nodes, 1):
        label = node.label()
        line = (
            f"{index}. {label}: {node.description}"
            if node.description
            else f"{index}. {label}"
        )
        lines.append(line)
    return "\n".join(lines)


def _search_records(nodes: Sequence[_Node]) -> list[dict[str, Any]]:
    return [
        {
            "command": node.label(),
            "description": node.description,
        }
        for node in nodes
    ]


def _details_text(node: _Node, max_help_chars: int) -> str:
    parts = []
    label = node.label()
    parts.append(f"{label}: {node.description}" if node.description else label)
    if node.usage:
        parts.append(
            "Usage:\n" + "\n".join("  " + line for line in node.usage.splitlines())
        )
    if node.children:
        parts.append(
            "Subcommands:\n"
            + "\n".join(
                f"  {name} — {child.description}" if child.description else f"  {name}"
                for name, child in node.children.items()
            )
        )
    if node.flags:
        parts.append(
            "Flags:\n" + "\n".join(flag.line() for flag in node.flags.values())
        )
    if node.help and max_help_chars:
        raw = node.help
        if len(raw) > max_help_chars:
            raw = raw[:max_help_chars].rstrip() + "\n[help truncated]"
        parts.append("Raw help:\n" + raw.rstrip())
    return "\n\n".join(parts)


def _details_record(node: _Node, max_help_chars: int) -> dict[str, Any]:
    raw = ""
    if node.help and max_help_chars:
        raw = node.help
        if len(raw) > max_help_chars:
            raw = raw[:max_help_chars].rstrip() + "\n[help truncated]"
    return {
        "command": node.label(),
        "description": node.description,
        "usage": node.usage,
        "subcommands": {
            name: child.description for name, child in node.children.items()
        },
        "flags": {name: flag.record() for name, flag in node.flags.items()},
        "help": raw,
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


def _build_command_tools(
    commands: Sequence[Command],
    *,
    executable: str,
    base_args: Sequence[str],
    prefix: str,
    cwd: Path | None,
    env: dict[str, str] | None,
    timeout: float,
) -> list[Tool]:
    """Build one ``dspy.Tool`` per explicitly defined command."""
    tool_cls = _load_tool()
    seen: set[str] = set()
    tools: list[Tool] = []

    def build(command: Command) -> Tool:
        command = _normalize_command(command)
        _validate_command(command)
        tool_name = f"{prefix}{command.name}"
        key = tool_name.casefold()
        if key in seen:
            raise ValueError(f"Duplicate tool name {tool_name!r}")
        seen.add(key)
        signature = _command_signature(command)

        def run(**kwargs: Any) -> str:
            return _run_argv(
                executable,
                base_args,
                command.command,
                _command_argv(command, kwargs),
                cwd=cwd,
                env=env,
                timeout=timeout,
                ok_codes=command.ok_codes,
            )

        run.__name__ = tool_name
        run.__doc__ = command.description
        run.__signature__ = signature  # type: ignore
        return tool_cls(
            run,
            name=tool_name,
            desc=command.description,
            args={name: _arg_schema(arg) for name, arg in command.args.items()},
            arg_types={name: _annotation(arg) for name, arg in command.args.items()},
            arg_desc={name: arg.help for name, arg in command.args.items() if arg.help},
        )

    for command in commands:
        tools.append(build(command))
    return tools


def _build_tools(
    discovery: _Discovery,
    *,
    prefix: str,
    mode: Literal["search", "code"],
    max_help_chars: int,
) -> list[Tool]:
    """Build the three discovery tools over one executable."""
    Tool = _load_tool()

    def search_commands(query: str, limit: int = 8) -> str | list[dict[str, Any]]:
        """Search this CLI's command tree by name and description."""
        nodes = discovery.search(query, limit)
        if mode == "code":
            return _search_records(nodes)
        return _search_text(nodes)

    def get_command_help(command: str = "") -> str | dict[str, Any]:
        """Return usage, subcommands, flags, and raw help for one command."""
        node = discovery.resolve(str(command or ""))
        if mode == "code":
            return _details_record(node, max_help_chars)
        return _details_text(node, max_help_chars)

    def run_command(command: str = "", args: list[str] | None = None) -> str:
        """Run a command path with argv strings; no shell is used."""
        return discovery.run(str(command or ""), _normalize_args(args))

    return [
        Tool(
            search_commands,
            name=f"{prefix}search_commands",
            desc=_SEARCH_DESC,
        ),
        Tool(
            get_command_help,
            name=f"{prefix}get_command_help",
            desc=_DETAILS_DESC,
        ),
        Tool(run_command, name=f"{prefix}run_command", desc=_RUN_DESC),
    ]


class CLITools(list):
    """List of ``dspy.Tool`` backed by one executable.

    Behaves like a normal list, so it can be passed directly as ``tools=`` or
    concatenated with other tools. In discovery mode the three tools share one
    cache, and :meth:`refresh` drops its help pages. Explicit-command mode has
    no discovery cache, so :meth:`refresh` is a no-op.
    """

    def __init__(self, tools: Any = (), discovery: _Discovery | None = None) -> None:
        super().__init__(tools)
        self._discovery = discovery

    def refresh(self) -> None:
        """Drop cached help pages; the next call rediscovers from the root."""
        if self._discovery is not None:
            self._discovery.refresh()


def _default_prefix(executable: str) -> str:
    stem = Path(executable).name
    slug = re.sub(r"[^a-z0-9]+", "_", stem.casefold()).strip("_")
    return f"{slug}_" if slug else ""


def CLI(
    executable: str,
    *,
    commands: Sequence[Command] | None = None,
    base_args: Sequence[str] = (),
    mode: Literal["search", "code"] = "search",
    name_prefix: str | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    help_timeout: float = DEFAULT_HELP_TIMEOUT,
    max_expands: int = DEFAULT_MAX_EXPANDS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_help_chars: int = DEFAULT_MAX_HELP_CHARS,
) -> CLITools:
    """Expose one executable as explicit DSPy tools or a discovery facade.

    With ``commands=...``, each :class:`Command` becomes its own typed
    ``dspy.Tool`` and discovery is skipped entirely. Without it, the CLI's
    own help is the schema: ``search_commands`` reads the root help, expands
    the best matching branches, and caches every page; ``get_command_help``
    expands exactly the requested path; ``run_command`` resolves that path
    and executes it.

    Args:
        executable: Program to run, resolved through ``PATH`` (or a path).
            Only this program is reachable; arguments are never passed to a
            shell.
        commands: Explicit command definitions. When provided, the three
            ``--help`` discovery tools are skipped; each command becomes one
            tool. The sequence must contain at least one command. Commands can
            declare required and optional arguments, flags, positionals,
            choices, and fixed args.
        base_args: Fixed arguments inserted before the discovered command,
            e.g. ``["-m", "mytool"]`` for a Python module CLI.
        mode: Used by the discovery facade: ``"search"`` (default) returns
            text for ``dspy.ReAct``; ``"code"`` returns dictionaries for
            ``dspy.RLM``. Ignored when ``commands=...`` is provided.
        name_prefix: Prepended to every tool name. Defaults to the executable
            name, such as ``"gh_"``.
        cwd: Working directory for help calls and command execution.
        env: Extra environment variables for help calls and execution. Help
            calls additionally force non-interactive pager settings.
        timeout: Deadline in seconds for ``run_command``; ``0`` disables it.
        help_timeout: Deadline in seconds for each help call (discovery only).
        max_expands: Maximum help pages a single search may fetch (discovery
            only).
        max_depth: Maximum command-tree levels a single search may walk
            (discovery only).
        max_help_chars: Cap for the raw help text in ``get_command_help``;
            ``0`` omits it (discovery only). Parsed flags are unaffected.

    Returns:
        A :class:`CLITools` list. With ``commands=...`` it contains one tool
        per command. Otherwise it contains ``search_commands``,
        ``get_command_help``, and ``run_command``; call :meth:`refresh` to
        clear cached help pages.

    Examples:
        >>> from dspy_agent_tools import CLI
        >>> gh = CLI("gh")
        >>> agent = dspy.ReAct("question -> answer", tools=gh)

        >>> from dspy_agent_tools import Argument, CLI, Command, Option
        >>> gh = CLI(
        ...     "gh",
        ...     commands=[
        ...         Command(
        ...             command="pr list",
        ...             name="pr_list",
        ...             description="List pull requests in a repository.",
        ...             args={
        ...                 "repo": Argument(help="owner/name"),
        ...                 "state": Option(
        ...                     "--state",
        ...                     default="open",
        ...                     choices=("open", "closed", "merged"),
        ...                 ),
        ...                 "limit": Option("--limit", default=30, type=int),
        ...             },
        ...             fixed=("--json",),
        ...         )
        ...     ],
        ... )
    """
    if mode not in _MODES:
        raise ValueError(f"Unknown mode {mode!r}: expected 'search' or 'code'")
    if timeout < 0:
        raise ValueError("timeout must be >= 0 (0 disables the deadline)")
    if help_timeout <= 0:
        raise ValueError("help_timeout must be > 0")
    if max_expands < 1:
        raise ValueError("max_expands must be >= 1")
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")
    if max_help_chars < 0:
        raise ValueError("max_help_chars must be >= 0")

    command_list = None if commands is None else list(commands)
    if command_list is not None and not command_list:
        raise ValueError("commands must contain at least one Command")

    try:
        local[executable]
    except CommandNotFound as exc:
        raise FileNotFoundError(f"Executable not found: {executable}") from exc

    resolved_cwd = _resolve_cwd(str(cwd)) if cwd is not None else None
    prefix = _default_prefix(executable) if name_prefix is None else name_prefix
    resolved_base_args = [str(item) for item in base_args]

    discovery: _Discovery | None = None
    if command_list is not None:
        tools = _build_command_tools(
            command_list,
            executable=executable,
            base_args=resolved_base_args,
            prefix=prefix,
            cwd=resolved_cwd,
            env=env,
            timeout=timeout,
        )
    else:
        discovery = _Discovery(
            executable,
            base_args=resolved_base_args,
            cwd=resolved_cwd,
            env=env,
            timeout=timeout,
            help_timeout=help_timeout,
            max_expands=max_expands,
            max_depth=max_depth,
        )
        tools = _build_tools(
            discovery, prefix=prefix, mode=mode, max_help_chars=max_help_chars
        )
    return CLITools(tools, discovery)


__all__ = ["CLI", "Arg", "Argument", "CLITools", "Command", "Option"]
