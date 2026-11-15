"""Turn any executable into a lazily discovered set of ``dspy.Tool`` objects.

Unlike MCP, a plain command-line program has no machine-readable protocol for
its commands or arguments. The only schema it exposes is its own help output.
``CLI`` therefore returns two discovery tools by default -- help and run --
instead of one tool per command:

- ``get_command_help`` returns the CLI's real help: usage, subcommands,
  parsed flags, and JSON help when available.
- ``run_command`` executes one command path with an argv list.
- ``search_commands`` is optional (``include_search=True``) for callers that
  need name/description lookup over a large command tree.

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

from .tools import CLI, CLITools
from .types import Argument, Command, Option

__all__ = ["CLI", "Argument", "CLITools", "Command", "Option"]
