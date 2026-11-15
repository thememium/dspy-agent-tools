"""Add tools and tool guidance to an agent that was already built.

``patch`` takes any number of tool sources — tool lists such as ``MCP(...)``,
``CLI(...)``, ``Browser()``, ``Skills(...)``, ``Toolbox(...)``, or bare
``dspy.Tool`` objects and functions — appends them to an existing agent, and
recomputes a short policy section per tool family in the agent's
instructions.

```python
import dspy
from dspy_agent_tools import CLI, Skills, bash_exec, patch, read_file

agent = dspy.ReAct("question -> answer", tools=[bash_exec])
patch(agent, read_file, Skills("./skills"), CLI("gh"))
```

The guidance goes into the signature's instructions, which DSPy sends as the
system message — never into new input fields, which would change what every
caller has to pass (``dspy.RLM`` refuses a call whose inputs were not
supplied, and ``dspy.ReAct`` copies them into the predictor it builds).

Sections come from the tool families present on the agent, so the block names
what the agent can actually do. A source that knows more than its family
template — ``Skills``, which carries the ``<available_skills>`` catalog — can
define ``family`` and a ``section(tools)`` method; that method is called with
the agent's full tool list and its output replaces the template.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .guide import present_keys as _present_keys
from .guide import sections as _sections

if TYPE_CHECKING:
    from dspy import Tool

logger = logging.getLogger(__name__)

#: Module bookkeeping that belongs to the instance, not to the rebuilt predictor.
_MODULE_STATE = frozenset({"callbacks", "history", "_compiled"})

#: Where the agent keeps the guide from its last ``patch`` call.
_GUIDE_ATTR = "_dspy_agent_tools_guide"

#: Delimit the managed region so a later call replaces it instead of appending.
_BEGIN = "<!-- dspy-agent-tools:begin -->"
_END = "<!-- dspy-agent-tools:end -->"

_REPL_NOTE = (
    "Your Python code runs in a REPL that cannot spawn processes or open "
    "network connections. Call a tool from inside your code to reach the "
    "machine — installing, running a command, or opening a URL from plain "
    "Python will fail."
)

_KINDS = ("ReAct", "ReActV2", "RLM")


def _flatten(source: Any) -> list[Tool]:
    """Tools from one source: a Tool, a callable, or a list of either."""
    import dspy

    if isinstance(source, dspy.Tool):
        return [source]
    if isinstance(source, (str, bytes)):
        raise TypeError(
            "patch() sources are tools, callables, or lists of them; "
            f"got {type(source).__name__}"
        )
    if callable(source):
        return [dspy.Tool(source)]
    try:
        items = list(source)
    except TypeError as exc:
        raise TypeError(
            "patch() sources are tools, callables, or lists of them; "
            f"got {type(source).__name__}"
        ) from exc
    tools: list[Tool] = []
    for item in items:
        tools.extend(_flatten(item))
    return tools


def _kind(agent: Any) -> str:
    """The supported module behind ``agent``, by its DSPy class name."""
    import dspy

    for name in _KINDS:
        cls = getattr(dspy, name, None)
        if cls is not None and isinstance(agent, cls):
            return name
    raise TypeError(
        "patch() supports dspy.ReAct, dspy.ReActV2, and dspy.RLM; "
        f"got {type(agent).__name__}"
    )


def _agent_tools(agent: Any, kind: str) -> dict[str, Any]:
    """The agent's tool mapping, keyed by name."""
    if kind == "RLM":
        return dict(getattr(agent, "_user_tools", {}))
    tools = getattr(agent, "tools", None)
    if not isinstance(tools, dict):
        raise TypeError(f"Unsupported {kind} tool container: {type(tools).__name__}")
    return dict(tools)


def _same(a: Any, b: Any) -> bool:
    """Whether two tools are one tool: the same object or the same function.

    A bare function is wrapped in a fresh ``dspy.Tool`` on every call, so
    patching ``read_file`` twice must compare the functions, not the wrappers.
    """
    if a is b:
        return True
    func = getattr(a, "func", None)
    return func is not None and func is getattr(b, "func", None)


def _without_region(instructions: str) -> str:
    """``instructions`` with the region this module manages removed."""
    start = instructions.find(_BEGIN)
    if start < 0:
        return instructions
    end = instructions.find(_END, start)
    tail = instructions[end + len(_END) :] if end >= 0 else ""
    return (instructions[:start] + tail).strip("\n")


def _with_region(instructions: str, blocks: Sequence[str]) -> str:
    """``instructions`` with one managed region holding ``blocks``."""
    base = _without_region(instructions)
    if not blocks:
        return base
    region = "\n\n".join([_BEGIN, *blocks, _END])
    return f"{base}\n\n{region}" if base else region


def _blocks(
    sources: Sequence[Any],
    tools: Sequence[Any],
    kind: str,
    guide: str | None,
    skip: frozenset[str],
) -> list[str]:
    """Guidance sections for the sources and the tools the agent holds."""
    blocks: list[str] = [guide] if guide else []
    covered: set[str] = set()
    for source in sources:
        family = getattr(source, "family", None)
        section = getattr(source, "section", None)
        if family and callable(section):
            blocks.append(section(tools))
            covered.add(family)
    if kind == "RLM":
        blocks.append(_REPL_NOTE)
    blocks.extend(_sections(tools, skip | frozenset(covered)))
    return blocks


def _demos(agent: Any) -> int:
    """Few-shot demos on the predictors the rebuild is about to replace."""
    return sum(
        len(getattr(value, "demos", None) or []) for value in vars(agent).values()
    )


def _copy_module_state(agent: Any, rebuilt: Any) -> None:
    """Copy a rebuilt module's own state onto ``agent`` in place."""
    for name, value in vars(rebuilt).items():
        if name not in _MODULE_STATE:
            setattr(agent, name, value)


def _rebuild(agent: Any, kind: str, signature: Any, tools: Sequence[Any]) -> Any:
    """A fresh module of the same class and settings, with ``tools``."""
    import dspy

    if kind == "RLM":
        return dspy.RLM(
            signature,
            max_iters=agent.max_iters,
            max_llm_calls=agent.max_llm_calls,
            max_output_chars=agent.max_output_chars,
            verbose=agent.verbose,
            tools=list(tools),
            sub_lm=agent.sub_lm,
            interpreter_factory=agent._interpreter_factory,
        )
    internal = "finish" if kind == "ReAct" else "submit"
    user_tools = [tool for tool in tools if tool.name != internal]
    cls = dspy.ReAct if kind == "ReAct" else dspy.ReActV2
    return cls(signature, tools=user_tools, max_iters=agent.max_iters)


def patch(
    agent: Any,
    *sources: Any,
    guide: str | None = None,
    describe: bool = True,
) -> Any:
    """Append ``sources`` to ``agent`` and inject tool guidance into its instructions.

    The agent is rebuilt in place from its own signature and settings, so the
    same instance keeps working: its instructions gain one section per tool
    family it holds (see :mod:`dspy_agent_tools.guide`), and its tool list
    gains the tools from ``sources``. Works for ``dspy.ReAct``,
    ``dspy.ReActV2``, and ``dspy.RLM``.

    Args:
        agent: The agent to extend.
        sources: Tools to add: ``dspy.Tool`` objects, plain functions, or lists
            of them, such as ``MCP(...)``, ``CLI(...)``, ``Browser()``,
            ``Skills(...)``, or ``Toolbox(...)``.
        guide: Your own text for the block, placed before the family sections.
            Use it for policy the tool families cannot know, such as a
            project's conventions or a forbidden action. The agent keeps the
            last guide it was given, so a later call that omits ``guide``
            keeps it; pass ``guide=""`` to clear it.
        describe: Add the family sections. ``False`` appends only the tools and
            ``guide``.

    Returns:
        The same ``agent``, for chaining: ``agent = patch(agent, ...)``.

    Raises:
        TypeError: ``agent`` is not one of the supported modules, or a source
            is neither a tool nor a list of tools.
        ValueError: The agent already has a *different* tool with one of the
            incoming names; pass ``name_prefix=...`` to the factory that built
            it.

    The injected block lives in one delimited region of the instructions and
    is recomputed on every call from the tools the agent holds, the sources
    passed now, and the guide, so patching repeatedly converges instead of
    growing: edited guidance replaces the old copy, and a family whose heading
    you already wrote yourself (``## Shell``) is left to you. Calling ``patch``
    again with the same arguments changes nothing.

    Rebuilding replaces the agent's inner predictor, so few-shot demos an
    optimizer left on it are dropped and a warning says how many; patch before
    compiling, or recompile afterwards.

    Examples:
        >>> import dspy
        >>> from dspy_agent_tools import CLI, Skills, bash_exec, patch, read_file
        >>> agent = dspy.ReAct("question -> answer", tools=[bash_exec])
        >>> patch(agent, read_file, Skills("./skills"), CLI("gh"))

        >>> agent = dspy.RLM("task -> report")
        >>> patch(agent, guide="Never edit files under vendor/.")
    """
    if not hasattr(agent, "signature"):
        raise TypeError(
            f"patch() needs a dspy module with a signature, got {type(agent).__name__}"
        )
    kind = _kind(agent)
    existing = _agent_tools(agent, kind)
    incoming = [tool for source in sources for tool in _flatten(source)]

    for tool in incoming:
        found = existing.get(tool.name)
        if found is not None and not _same(found, tool):
            raise ValueError(
                f"Agent already has a different tool named {tool.name!r}; "
                "pass name_prefix=... to the factory that built it."
            )
    additions = [tool for tool in incoming if tool.name not in existing]
    tools = [*existing.values(), *additions]

    if guide is None:
        note = getattr(agent, _GUIDE_ATTR, None)
    else:
        note = guide.strip() or None
    base = _without_region(agent.signature.instructions)
    if describe:
        blocks = _blocks(sources, tools, kind, note, _present_keys(base))
    else:
        blocks = [note] if note else []
    instructions = _with_region(base, blocks)
    setattr(agent, _GUIDE_ATTR, note)
    if not additions and instructions == agent.signature.instructions:
        return agent

    dropped = _demos(agent)
    if dropped:
        logger.warning(
            "patch() rebuilds %s and drops %d few-shot demo(s) on its inner "
            "predictor; patch before compiling, or recompile afterwards.",
            kind,
            dropped,
        )

    rebuilt = _rebuild(
        agent,
        kind,
        agent.signature.with_instructions(instructions),
        tools,
    )
    _copy_module_state(agent, rebuilt)
    return agent


__all__ = ["patch"]
