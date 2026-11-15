"""Apply tools and guide sections to a built DSPy agent."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .guide import present_keys as _present_keys
from .guide import sections as _sections
from .region import _with_region, _without_region

if TYPE_CHECKING:
    from dspy import Tool

logger = logging.getLogger("dspy_agent_tools.patch")

#: Module bookkeeping that belongs to the instance, not to the rebuilt predictor.
_MODULE_STATE = frozenset({"callbacks", "history", "_compiled"})

#: Where the agent keeps the guide from its last ``Patch`` call.
_GUIDE_ATTR = "_dspy_agent_tools_guide"

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
            "Patch() takes tools, callables, or lists of them; "
            f"got {type(source).__name__}"
        )
    if callable(source):
        return [dspy.Tool(source)]
    try:
        items = list(source)
    except TypeError as exc:
        raise TypeError(
            "Patch() takes tools, callables, or lists of them; "
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
        "Patch() supports dspy.ReAct, dspy.ReActV2, and dspy.RLM; "
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


def _sources(tools: Any) -> list[Any]:
    """Top-level items of ``tools``, keeping a list that describes itself.

    ``tools=Skills(...)`` is one source that owns its section; ``tools=[skills,
    read_file]`` is two. A bare tool or function is one source.
    """
    if callable(getattr(tools, "section", None)):
        return [tools]
    if isinstance(tools, (str, bytes)):
        return [tools]
    try:
        return list(tools)
    except TypeError:
        return [tools]


def _drop_names(drop: Any) -> set[str]:
    """Tool names to remove, from strings, tools, or functions."""
    names: set[str] = set()
    for item in _sources(drop):
        if isinstance(item, str):
            names.add(item)
            continue
        func = getattr(item, "func", None)
        name = getattr(item, "name", None) or getattr(item, "__name__", "")
        names.add(str(name or getattr(func, "__name__", "") or ""))
    return {name for name in names if name}


def Patch(
    agent: Any,
    tools: Any = (),
    *,
    drop: Any = (),
    guide: str | None = None,
    describe: bool = True,
) -> Any:
    """Add ``tools`` to ``agent`` and inject tool guidance into its instructions.

    The agent is rebuilt in place from its own signature and settings, so the
    same instance keeps working: its instructions gain one section per tool
    family it holds (see :mod:`dspy_agent_tools.patch.guide`), and its tool list
    gains ``tools`` and loses ``drop``. Works for ``dspy.ReAct``,
    ``dspy.ReActV2``, and ``dspy.RLM``.

    Adding accumulates and dropping is explicit: two call sites that patch the
    same agent both keep their tools, and ``drop`` revokes a capability without
    having to reconstruct the agent.

    Args:
        agent: The agent to extend.
        tools: Tools to add: ``dspy.Tool`` objects, plain functions, or lists
            of them, such as ``MCP(...)``, ``CLI(...)``, ``Browser()``,
            ``Skills(...)``, or ``Toolbox(...)``.
        drop: Tools to remove — names, ``dspy.Tool`` objects, or functions,
            or a list of them. Names that are not on the agent are reported in
            a warning and ignored, so a conditional drop stays idempotent.
        guide: Your own text for the block, placed before the family sections.
            Use it for policy the tool families cannot know, such as a
            project's conventions or a forbidden action. The agent keeps the
            last guide it was given, so a later call that omits ``guide``
            keeps it; pass ``guide=""`` to clear it.
        describe: Add the family sections. ``False`` appends only the tools and
            ``guide``.

    Returns:
        The same ``agent``, for chaining: ``agent = Patch(agent, tools=...)``.

    Raises:
        TypeError: ``agent`` is not one of the supported modules, or ``tools``
            holds something that is neither a tool nor a list of tools.
        ValueError: The agent already has a *different* tool with one of the
            incoming names (pass ``name_prefix=...`` to the factory that built
            it), a name is in both ``tools`` and ``drop``, or ``drop`` names
            the agent's own internal tool (ReAct's ``finish`` or ReActV2's
            ``submit``).

    The injected block lives in one delimited region of the instructions and
    is recomputed on every call from the tools the agent holds, the sources
    passed now, and the guide, so patching repeatedly converges instead of
    growing: edited guidance replaces the old copy, and a family whose heading
    you already wrote yourself (``## Shell``) is left to you. Calling ``Patch``
    again with the same arguments changes nothing.

    Rebuilding replaces the agent's inner predictor, so few-shot demos an
    optimizer left on it are dropped and a warning says how many; patch before
    compiling, or recompile afterwards.

    Examples:
        >>> import dspy
        >>> from dspy_agent_tools import CLI, Skills, bash_exec, Patch, read_file
        >>> agent = dspy.ReAct("question -> answer", tools=[bash_exec])
        >>> Patch(agent, tools=[read_file, Skills("./skills"), CLI("gh")])

        >>> Patch(agent, drop="bash_exec")  # revoke a capability

        >>> agent = dspy.RLM("task -> report")
        >>> Patch(agent, guide="Never edit files under vendor/.")
    """
    if not hasattr(agent, "signature"):
        raise TypeError(
            f"Patch() needs a dspy module with a signature, got {type(agent).__name__}"
        )
    kind = _kind(agent)
    existing = _agent_tools(agent, kind)
    sources = _sources(tools)
    incoming = [tool for source in sources for tool in _flatten(source)]

    for tool in incoming:
        found = existing.get(tool.name)
        if found is not None and not _same(found, tool):
            raise ValueError(
                f"Agent already has a different tool named {tool.name!r}; "
                "pass name_prefix=... to the factory that built it."
            )
    additions = [tool for tool in incoming if tool.name not in existing]

    removed = _drop_names(drop)
    internal = (
        {"finish"} if kind == "ReAct" else {"submit"} if kind == "ReActV2" else set()
    )
    reserved = removed & internal
    if reserved:
        raise ValueError(
            f"{min(reserved)!r} is {kind}'s own tool and cannot be dropped"
        )
    clash = removed & {tool.name for tool in incoming}
    if clash:
        raise ValueError(f"{min(clash)!r} is in both tools= and drop=")
    unknown = removed - set(existing)
    if unknown:
        logger.warning(
            "Patch() has no tool named %s to drop", ", ".join(sorted(unknown))
        )

    kept = [
        tool for tool in [*existing.values(), *additions] if tool.name not in removed
    ]

    if guide is None:
        note = getattr(agent, _GUIDE_ATTR, None)
    else:
        note = guide.strip() or None
    base = _without_region(agent.signature.instructions)
    if describe:
        blocks = _blocks(sources, kept, kind, note, _present_keys(base))
    else:
        blocks = [note] if note else []
    instructions = _with_region(base, blocks)
    setattr(agent, _GUIDE_ATTR, note)
    if [tool.name for tool in kept] == list(existing) and (
        instructions == agent.signature.instructions
    ):
        return agent

    demos = _demos(agent)
    if demos:
        logger.warning(
            "Patch() rebuilds %s and drops %d few-shot demo(s) on its inner "
            "predictor; patch before compiling, or recompile afterwards.",
            kind,
            demos,
        )

    rebuilt = _rebuild(
        agent,
        kind,
        agent.signature.with_instructions(instructions),
        kept,
    )
    _copy_module_state(agent, rebuilt)
    return agent


__all__ = ["Patch"]
