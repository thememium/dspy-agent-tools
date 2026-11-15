"""Build DSPy tools for explicit commands or lazy help discovery."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from plumbum import CommandNotFound, local

from ..utils.dspy import load_tool
from ..utils.process import DEFAULT_TIMEOUT, resolve_cwd
from .argv import (
    _annotation,
    _arg_schema,
    _command_argv,
    _command_signature,
    _normalize_args,
    _normalize_command,
    _validate_command,
)
from .discovery import DEFAULT_MAX_HELP_CHARS, _Discovery
from .render import _details_record, _details_text, _search_records, _search_text
from .runner import _run_argv
from .types import Command

if TYPE_CHECKING:
    from dspy import Tool

DEFAULT_HELP_TIMEOUT = 10.0
DEFAULT_MAX_EXPANDS = 8
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_OUTPUT_CHARS = 0

_MODES = ("search", "code")

_SEARCH_DESC = (
    "Search this CLI's command tree by name or description. Use this only "
    "when you do not know the exact command path; if you know it, call "
    "get_command_help directly. Returns matching paths and descriptions."
)
_DETAILS_DESC = (
    "Return help for one command path: usage, subcommands, parsed flags, and "
    "JSON help when available. Raw help is included only as a fallback. "
    "command='' returns the root command list. Fetch this before the first "
    "run of a subcommand when its flags or positional arity are unclear; "
    "skip it for obvious commands, and never fetch it for every command."
)
_RUN_DESC = (
    "Run one command path of this CLI with argv strings. command is the path "
    "only (e.g. 'search'); do not repeat it in args. args is the argv tail: "
    "one item per argument, so a multi-word query is one item, e.g. "
    "command='search', args=['coverage python', '--limit', '3']. command='' "
    "runs the executable itself; use it only when the CLI has no "
    "subcommands. If help lists an output-limit flag, pass a small value; "
    "narrow the query instead of fetching many broad results. No shell is "
    "used. Returns the output and wall time, plus the exit code on failure."
)

_LOAD_TOOL_MESSAGE = (
    "dspy_agent_tools.CLI requires DSPy. "
    "Install it with: uv add 'dspy-agent-tools[mcp]'"
)


def _build_command_tools(
    commands: Sequence[Command],
    *,
    executable: str,
    base_args: Sequence[str],
    prefix: str,
    cwd: Path | None,
    env: dict[str, str] | None,
    timeout: float,
    json_flag: str | None,
    max_output_chars: int,
) -> list[Tool]:
    """Build one ``dspy.Tool`` per explicitly defined command."""
    tool_cls = load_tool(_LOAD_TOOL_MESSAGE)
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
                json_flag=json_flag,
                max_output_chars=max_output_chars,
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
    include_search: bool,
) -> list[Tool]:
    """Build the three discovery tools over one executable."""
    Tool = load_tool(_LOAD_TOOL_MESSAGE)

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

    tools = [
        Tool(
            get_command_help,
            name=f"{prefix}get_command_help",
            desc=_DETAILS_DESC,
        ),
        Tool(run_command, name=f"{prefix}run_command", desc=_RUN_DESC),
    ]
    if include_search:
        tools.insert(
            0,
            Tool(
                search_commands,
                name=f"{prefix}search_commands",
                desc=_SEARCH_DESC,
            ),
        )
    return tools


class CLITools(list):
    """List of ``dspy.Tool`` backed by one executable.

    Behaves like a normal list, so it can be passed directly as ``tools=`` or
    concatenated with other tools. Discovery tools share one cache, and
    :meth:`refresh` drops its help pages. Explicit-command mode has
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
    help_flag: str | Sequence[str] | None = "--help",
    json_flag: str | None = None,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    include_search: bool = False,
) -> CLITools:
    """Expose one executable as explicit DSPy tools or a discovery facade.

    With ``commands=...``, each :class:`Command` becomes its own typed
    ``dspy.Tool`` and discovery is skipped entirely. Without it, the CLI's
    own help is the schema: ``get_command_help`` reads the root command list
    and expands exactly the requested path; ``run_command`` resolves that
    path and executes it. ``search_commands`` is added only with
    ``include_search=True``.

    Args:
        executable: Program to run, resolved through ``PATH`` (or a path).
            Only this program is reachable; arguments are never passed to a
            shell.
        commands: Explicit command definitions. When provided, the
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
            only, with ``include_search=True``).
        max_depth: Maximum command-tree levels a single search may walk
            (discovery only, with ``include_search=True``).
        max_help_chars: Cap for the JSON/raw help text in
            ``get_command_help``; ``0`` omits it (discovery only). Parsed
            flags are unaffected.
        help_flag: Help invocation tokens, default ``"--help"``. Pass ``"-h"``,
            ``"help"``, or a sequence such as ``("help", "commands")`` when the
            CLI uses something else. ``None`` skips help and runs the command
            itself. If the help invocation is rejected or returns nothing, the
            command is run so the agent can inspect its output.
        json_flag: Optional machine-readable output flag to probe. It is
            disabled by default; pass ``"--json"`` to try it on every command
            run and help call and fall back when the CLI rejects it. An
            already-declared ``--json`` argument is always passed through
            unchanged.
        max_output_chars: Cap one command result before it reaches the model.
            ``0`` (default) keeps the full output. Truncation is head-only and
            appends a marker; JSON output can become invalid, so keep this
            off when the agent parses it. Prefer the CLI's own output-limit
            flags when available.
        include_search: Add ``search_commands`` to the discovery tools.
            It is off by default because ``get_command_help(command="")``
            already lists root commands and avoids an extra lookup turn.

    Returns:
        A :class:`CLITools` list. With ``commands=...`` it contains one tool
        per command. Otherwise it contains ``get_command_help`` and
        ``run_command``; ``search_commands`` is added with
        ``include_search=True``. Call :meth:`refresh` to clear cached help
        pages.

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
    if max_output_chars < 0:
        raise ValueError("max_output_chars must be >= 0")
    if json_flag is not None and not json_flag.startswith("-"):
        raise ValueError("json_flag must start with '-' or be None")
    if help_flag is None:
        help_tokens: tuple[str, ...] = ()
    elif isinstance(help_flag, str):
        help_tokens = (help_flag,)
    else:
        help_tokens = tuple(help_flag)
    if any(not isinstance(token, str) or not token for token in help_tokens):
        raise ValueError("help_flag must be a non-empty string or sequence of strings")

    command_list = None if commands is None else list(commands)
    if command_list is not None and not command_list:
        raise ValueError("commands must contain at least one Command")

    try:
        local[executable]
    except CommandNotFound as exc:
        raise FileNotFoundError(f"Executable not found: {executable}") from exc

    resolved_cwd = resolve_cwd(str(cwd)) if cwd is not None else None
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
            json_flag=json_flag,
            max_output_chars=max_output_chars,
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
            help_tokens=help_tokens,
            json_flag=json_flag,
            max_help_chars=max_help_chars,
            max_output_chars=max_output_chars,
        )
        tools = _build_tools(
            discovery,
            prefix=prefix,
            mode=mode,
            max_help_chars=max_help_chars,
            include_search=include_search,
        )
    return CLITools(tools, discovery)
