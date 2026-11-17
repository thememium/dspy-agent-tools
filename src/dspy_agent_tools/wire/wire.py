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
it chains with :func:`dspy_agent_tools.Patch`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .hooks import install
from .offload import Offloader
from .policy import Policy
from .session import Session
from .tools import wrap_tools

if TYPE_CHECKING:
    from ..memory.tools import MemoryTools


def Wire(
    agent: Any,
    *,
    memory: MemoryTools | None = None,
    session: Session | None = None,
    offload: Offloader | None = None,
    policy: Policy | None = None,
    trace: bool = True,
    query: str | None = None,
    write_back: bool = True,
) -> Any:
    """Attach memory, offloading, tracing, sessions, and policy to ``agent``.

    Args:
        agent: A ``dspy.ReAct``, ``dspy.ReActV2``, or ``dspy.RLM`` instance.
        memory: Tools from :func:`dspy_agent_tools.Memory`. For an RLM the
            first iteration recalls against ``query`` or the call's inputs,
            the model also gets ``memory_recall``/``memory_remember`` inside
            its REPL, and iteration 1 extracts new memories. For ReAct the
            three memory tools are added and recalled memories are injected
            as guidance.
        session: Records every iteration or tool call under one key. When
            ``offload`` is also given, its spilled files are recorded as
            artifacts too.
        offload: Replaces oversized results with a file handle.
        policy: Denies a tool call or generated code by returning a reason.
            Denials surface to the model, not as a crash.
        trace: Emit OTel spans. Without ``opentelemetry-api`` this is inert.
        query: Text to recall memories against. For an RLM it overrides the
            call's inputs; for ReAct it is required to recall at all, because
            the agent's input is only known when it is called.
        write_back: For an RLM, extract memories from the call's inputs on
            iteration 1. Best-effort: a failure is logged, not raised.

    Returns:
        The same ``agent``, configured in place.

    Raises:
        TypeError: ``agent`` is not a supported DSPy module.
        ImportError: ``agent`` is an RLM and ``dspy-rlm-hooks`` is not
            installed (``uv add 'dspy-agent-tools[hooks]'``).

    Example:
        >>> from dspy_agent_tools import Memory, Offloader, Session, Wire
        >>> store = Memory(store, user_id="user_123")
        >>> Wire(
        ...     rlm,
        ...     memory=store,
        ...     offload=Offloader(limit=20_000),
        ...     session=Session("runs.db"),
        ... )
    """
    from ..patch.patch import Patch, _kind

    if offload is not None and session is not None and offload.session is None:
        # One call site should not have to remember both links: spilled files
        # become session artifacts whenever both are supplied.
        offload.session = session

    kind = _kind(agent)

    if kind == "RLM":
        if memory is not None:
            # RLM tools are callable from generated code, so adding the
            # memory family gives the REPL memory_recall/memory_remember
            # without injecting Python objects the interpreter cannot
            # serialise. Patch rebuilds the module, so it runs before hooks.
            Patch(agent, tools=memory)
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

    wrapped = wrap_tools(
        agent.tools,
        policy=policy,
        offload=offload,
        session=session,
        trace=trace,
    )
    Patch(agent, tools=wrapped, replace=True)

    if memory is not None:
        guide = None
        if query:
            guide = memory.format(memory.recall(query)) or None
        Patch(agent, tools=memory, guide=guide)
    return agent


__all__ = ["Wire"]
