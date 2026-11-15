"""Cross-tool policy sections injected into an agent's instructions.

DSPy already tells the model what each tool is: every adapter renders a
``dspy.Tool`` as its name, its description, and its argument schema, and
``dspy.ReAct`` lists that same text in the instructions it builds. What no
single tool can say is how its family relates to the others — when to reach
for the shell instead of the browser, that the REPL cannot spawn a process,
that a file write made against a stale snapshot is refused. That is what the
sections here carry, so the injected block stays short and never restates a
schema.

Each :class:`Family` owns one section. A tool belongs to a family when it
comes from that module (:mod:`dspy_agent_tools.bash` and friends) or when its
name is one of the spellings a foreign tool is likely to use, such as
``shell`` or ``run_command``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Names listed in a heading before the rest are summarised as "+N more".
_MAX_LISTED = 8


@dataclass(frozen=True, slots=True)
class Family:
    """One tool family: how to recognise its tools and what to tell the model."""

    key: str
    heading: str
    modules: frozenset[str]
    names: frozenset[str]
    text: str
    #: Append the family's tool names to the heading.
    list_names: bool = True
    suffixes: tuple[str, ...] = ()


_FAMILIES: tuple[Family, ...] = (
    Family(
        key="shell",
        heading="Shell",
        modules=frozenset({"dspy_agent_tools.bash"}),
        names=frozenset(
            {"bash", "exec", "execute", "run_shell", "shell", "sh", "terminal"}
        ),
        suffixes=("_bash_exec", "_run_command"),
        text=(
            "These run host commands with the permissions of the agent process: "
            "one command for installs, git, and tests, and the search tools to "
            "locate files or text without building a pipeline. Read the output "
            "before reporting it; never state a result you did not see and never "
            "invent command output."
        ),
    ),
    Family(
        key="files",
        heading="Files",
        modules=frozenset({"dspy_agent_tools.files"}),
        names=frozenset({"read_file", "write_file", "edit_file", "delete_file"}),
        text=(
            "Read a file before changing it: `read_file` returns its content and "
            "a snapshot token, and passing that token makes a write made against "
            "a stale file fail instead of overwriting a newer change. Never claim "
            "a file change you did not make."
        ),
    ),
    Family(
        key="web",
        heading="Web",
        modules=frozenset({"dspy_agent_tools.search"}),
        names=frozenset({"web_search", "web_news", "search_web"}),
        text=(
            "Use these for facts that may have changed since training — versions, "
            "releases, prices, news — and for the URL a page cites. They return "
            "titles, URLs, and snippets, not page contents: fetch or open a "
            "promising result instead of guessing what is behind it, and cite the "
            "URLs you used."
        ),
    ),
    Family(
        key="browser",
        heading="Browser",
        modules=frozenset({"dspy_agent_tools.browser"}),
        names=frozenset({"browser", "browser_open"}),
        text=(
            "Open a URL, then take a snapshot: it returns the page text and the "
            "element refs the other calls take. Reach for the browser only when a "
            "page needs JavaScript or interaction — a search or fetch tool is "
            "faster for static content — and close it when the task is done."
        ),
    ),
    Family(
        key="commands",
        heading="Commands",
        modules=frozenset({"dspy_agent_tools.cli"}),
        names=frozenset(),
        text=(
            "These are typed wrappers around a program: each tool is one "
            "subcommand, arguments are validated and passed as argv, and there is "
            "no shell — no pipes, redirects, globs, or `$VAR` expansion. Read a "
            "subcommand's help tool before using it for the first time, and report "
            "a failure by its output rather than a guess."
        ),
    ),
    Family(
        key="mcp",
        heading="MCP",
        modules=frozenset({"dspy_agent_tools.mcp"}),
        names=frozenset(),
        list_names=False,
        text=(
            "These tools come from MCP servers, and each description and argument "
            "schema is the server's own, so trust it over assumptions from the "
            "tool name. A server can be slow or offline: retry once, then report "
            "the error instead of inventing a result."
        ),
    ),
    Family(
        key="skills",
        heading="Agent Skills",
        modules=frozenset({"dspy_agent_tools.skills"}),
        names=frozenset({"list_skills", "activate_skill"}),
        text=(
            "A skill is a folder of instructions and reference files. List them, "
            "then activate one before following it; follow a skill's steps with "
            "the tools you actually have, and name a command the user must run "
            "when you have no tool that runs it."
        ),
    ),
    Family(
        key="toolbox",
        heading="On-demand tools",
        modules=frozenset({"dspy_agent_tools.toolbox"}),
        names=frozenset({"search_tools", "get_tool_details", "call_tool"}),
        text=(
            "This agent loads its tools on demand: `search_tools` finds a tool by "
            "intent, `get_tool_details` returns its input schema, and `call_tool` "
            "runs it. Search before concluding that a capability is missing, and "
            "read the schema before calling."
        ),
    ),
)

_BY_KEY = {family.key: family for family in _FAMILIES}


def _name(tool: Any) -> str:
    func = getattr(tool, "func", None)
    return str(getattr(tool, "name", None) or getattr(func, "__name__", "") or "")


def family_of(tool: Any) -> Family | None:
    """The family ``tool`` belongs to, or ``None`` for a tool this package
    does not own."""
    func = getattr(tool, "func", None)
    module = getattr(func, "__module__", "") or ""
    name = _name(tool)
    for family in _FAMILIES:
        if module in family.modules:
            return family
        if name and (name in family.names or name.endswith(family.suffixes)):
            return family
    return None


def names_in(tools: Iterable[Any], keys: set[str] | frozenset[str]) -> list[str]:
    """Sorted names of the tools whose family key is in ``keys``."""
    found = set()
    for tool in tools:
        family = family_of(tool)
        if family is not None and family.key in keys:
            found.add(_name(tool))
    return sorted(name for name in found if name)


def _heading(family: Family, names: Sequence[str]) -> str:
    if not family.list_names or not names:
        return f"## {family.heading}"
    listed = ", ".join(f"`{name}`" for name in names[:_MAX_LISTED])
    if len(names) > _MAX_LISTED:
        listed += f", +{len(names) - _MAX_LISTED} more"
    return f"## {family.heading} — {listed}"


def sections(tools: Sequence[Any], skip: frozenset[str] = frozenset()) -> list[str]:
    """One section per family present in ``tools``, in a fixed order.

    ``skip`` names family keys already described by a caller-supplied section,
    such as :meth:`dspy_agent_tools.SkillTools.section`.
    """
    buckets: dict[str, set[str]] = {}
    for tool in tools:
        family = family_of(tool)
        if family is None or family.key in skip:
            continue
        buckets.setdefault(family.key, set()).add(_name(tool))
    blocks = []
    for family in _FAMILIES:
        names = sorted(name for name in buckets.get(family.key, ()) if name)
        if names:
            blocks.append(f"{_heading(family, names)}\n\n{family.text}")
    return blocks


__all__ = ["Family", "family_of", "names_in", "sections"]
