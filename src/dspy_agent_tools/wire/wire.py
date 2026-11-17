"""One call that composes memory, offloading, tracing, sessions, and policy.

The same capabilities reach both DSPy agent shapes, with the mechanism the
shape allows:

- ``dspy.RLM`` gets lifecycle hooks from ``dspy-rlm-hooks``: recall before the
  action, policy before execution, offloading after it, and a log plus memory
  extraction after the iteration.
- ``dspy.ReAct`` / ``dspy.ReActV2`` get their tools rewrapped: policy, spans,
  offloading, and session logging around every call, and memory tools added to
  the agent with recalled memories injected as guidance.

Neither path patches DSPy itself. ``Wire`` returns the agent it was given, so
it chains with :func:`dspy_agent_tools.Patch`, and calling it again replaces
the middleware instead of stacking another layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .hooks import install
from .offload import Offloader
from .policy import PolicyLike, as_policy
from .session import Session
from .tools import wrap_tools

if TYPE_CHECKING:
    from ..memory.tools import MemoryTools


def _memory_tools(memory: Any, user_id: str | None) -> MemoryTools | None:
    """Accept memory tools or a bare store, and return tools."""
    from ..memory import Memory, MemoryTools

    if memory is None:
        return None
    if isinstance(memory, MemoryTools):
        return memory
    if callable(getattr(memory, "search_memories", None)):
        if not user_id:
            raise TypeError(
                "Wire(memory=<store>) needs user_id=...; otherwise pass "
                "Memory(store, user_id=...)."
            )
        return Memory(memory, user_id=user_id)
    raise TypeError(
        "Wire(memory=...) takes Memory(store, user_id=...) tools, a store "
        "with search_memories(), or None; got "
        f"{type(memory).__name__}"
    )


def _flag(value: Any, factory: Any) -> Any:
    """``True`` builds the default object; ``False`` and ``None`` mean none."""
    if value is True:
        return factory()
    if value is False:
        return None
    return value


def Wire(
    agent: Any,
    *,
    memory: MemoryTools | Any = None,
    user_id: str | None = None,
    session: Session | bool | None = None,
    offload: Offloader | bool | None = None,
    policy: PolicyLike | Sequence[PolicyLike] | None = None,
    trace: bool = True,
    query: str | None = None,
    write_back: bool = True,
) -> Any:
    """Attach memory, offloading, tracing, sessions, and policy to ``agent``.

    Every option is independent; the shortest useful call is one line:

        >>> Wire(agent, policy=safe_defaults("."))

    Args:
        agent: A ``dspy.ReAct``, ``dspy.ReActV2``, or ``dspy.RLM`` instance.
        memory: Tools from :func:`dspy_agent_tools.Memory`, or a bare store
            with ``search_memories()`` (then ``user_id`` is required). On an
            RLM the first iteration recalls against ``query`` or the call's
            inputs and iteration 1 extracts new memories; memory tools are
            added to the REPL as ordinary tools. On ReAct the tools are added
            and recalled memories are injected as guidance when ``query`` is
            given.
        user_id: Owner for a bare store passed as ``memory``.
        session: A :class:`~dspy_agent_tools.Session`, or ``True`` for an
            in-memory one. Records every iteration or tool call; spilled files
            are recorded as artifacts when ``offload`` is present too.
        offload: An :class:`~dspy_agent_tools.Offloader`, or ``True`` for the
            default one. Replaces oversized results with a file handle.
        policy: One policy or a sequence of them, combined with conjunction.
            Denials surface to the model, not as a crash. The factories in
            :mod:`dspy_agent_tools.wire.policies` cover the shipped tools.
        trace: Emit OTel spans. Without ``opentelemetry-api`` this is inert.
        query: Text to recall memories against. On an RLM it overrides the
            call's inputs; on ReAct it is required to recall at all, because
            the agent's input is only known when it is called.
        write_back: On an RLM, extract memories from the call's inputs on
            iteration 1. Best-effort: a failure is logged, not raised.

    Returns:
        The same ``agent``, configured in place.

    Raises:
        TypeError: ``agent`` is not a supported DSPy module, or ``memory`` is
            neither memory tools nor a store.
        ImportError: ``agent`` is an RLM and ``dspy-rlm-hooks`` is not
            installed (``uv add 'dspy-agent-tools[hooks]'``).

    Example:
        >>> from dspy_agent_tools import Wire, safe_defaults
        >>> Wire(agent, policy=safe_defaults("."))

        >>> from dspy_agent_tools import Memory, Session
        >>> Wire(
        ...     rlm,
        ...     memory=store,          # a dspy-lancedb-memory store
        ...     user_id="user_123",
        ...     offload=True,
        ...     session=Session("runs.db"),
        ... )
    """
    from ..patch.patch import Patch, _kind

    memory = _memory_tools(memory, user_id)
    session = _flag(session, Session)
    offload = _flag(offload, Offloader)
    policy = as_policy(policy)

    if offload is not None and session is not None and offload.session is None:
        # One call site should not have to remember both links: spilled files
        # become session artifacts whenever both are supplied.
        offload.session = session

    def with_middleware(sequence: Any) -> list[Any]:
        return wrap_tools(
            sequence,
            policy=policy,
            offload=offload,
            session=session,
            trace=trace,
        )

    kind = _kind(agent)

    def add_memory(guide: str | None = None) -> None:
        assert memory is not None
        existing = agent._user_tools if kind == "RLM" else agent.tools
        previous = getattr(agent, "_dspy_agent_tools_memory_names", set())
        incoming_names = {tool.name for tool in memory}
        collisions = {
            tool.name
            for tool in memory
            if tool.name in existing
            and tool.name not in previous
            and getattr(
                existing[tool.name].func,
                "_dspy_agent_tools_original",
                existing[tool.name].func,
            )
            is not getattr(tool.func, "_dspy_agent_tools_original", tool.func)
        }
        if collisions:
            raise ValueError(
                f"Agent already has a different tool named {min(collisions)!r}; "
                "pass name_prefix=... to the memory factory."
            )
        Patch(
            agent,
            tools=memory.with_tools(with_middleware(memory)),
            guide=guide,
            replace=True,
        )
        agent._dspy_agent_tools_memory_names = previous | incoming_names

    if kind == "RLM":
        Patch(agent, tools=with_middleware(agent._user_tools), replace=True)
        if memory is not None:
            # RLM tools are callable from generated code, so adding the
            # memory family gives the REPL memory_recall/memory_remember
            # without injecting Python objects the interpreter cannot
            # serialise. Patch rebuilds the module, so it runs before hooks.
            add_memory()
        install(
            agent,
            memory=memory,
            session=session,
            offload=offload,
            policy=policy,
            trace=trace,
            write_back=write_back,
            recall_query=query,
        )
        return agent

    Patch(agent, tools=with_middleware(agent.tools), replace=True)

    if memory is not None:
        guide = None
        if query:
            guide = memory.format(memory.recall(query)) or None
        add_memory(guide)
    return agent


__all__ = ["Wire"]
