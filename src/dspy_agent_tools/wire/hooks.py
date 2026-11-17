"""RLM lifecycle hooks that compose memory, offloading, tracing, and policy.

``dspy_rlm_hooks`` exposes four points in DSPy's RLM loop. Each one maps to a
capability this package already has:

============================  =====================================
Hook                          Composition
============================  =====================================
``pre_iteration``             recall memories into prompt context
``pre_execution``             deny generated code by policy
``post_execution``            spill an oversized result to a file
``post_iteration``            log the iteration, extract new memories
============================  =====================================
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from .observe import start_span
from .offload import Offloader
from .policy import PolicyLike, check
from .session import Session

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ..memory.tools import MemoryTools

logger = logging.getLogger("dspy_agent_tools.wire")


def _hooks_module() -> Any:
    try:
        import dspy_rlm_hooks
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "Wire() needs dspy-rlm-hooks to instrument dspy.RLM. "
            "Install it with: uv add 'dspy-agent-tools[hooks]'"
        ) from exc
    return dspy_rlm_hooks


def _query(input_args: dict[str, Any]) -> str:
    """The text to recall memories against: the call's own inputs."""
    parts = [str(value) for value in input_args.values() if value]
    return "\n".join(parts)[:4000]


def _messages(input_args: dict[str, Any]) -> list[dict[str, str]]:
    """Turn an RLM call's inputs into one user turn for memory extraction."""
    text = "\n".join(f"{key}: {value}" for key, value in input_args.items())
    return [{"role": "user", "content": text[:8000]}]


def _refusal(reason: str) -> str:
    """Code that fails in the REPL with the policy's reason."""
    return f"raise PermissionError({reason!r})"


def build_hooks(
    *,
    memory: MemoryTools | None = None,
    session: Session | None = None,
    offload: Offloader | None = None,
    policy: PolicyLike | Sequence[PolicyLike] | None = None,
    trace: bool = True,
    write_back: bool = True,
    recall_query: str | None = None,
) -> dict[str, Callable[..., Any]]:
    """Build the four hook callables for :func:`dspy_rlm_hooks.enable_rlm_hooks`.

    Args:
        memory: Memory tools; recalled once per run and extracted once at the
            first iteration.
        session: Records every iteration and generated code.
        offload: Replaces an oversized iteration result with a file handle.
        policy: Denies generated Python by returning a reason string.
        trace: Emit an OTel span per iteration and per execution.
        write_back: Extract memories from the call's inputs on iteration 1.
        recall_query: Fixed text to recall against instead of the call's
            inputs. Use it when the run is about one known question.

    Returns:
        ``{"pre_iteration_hook": ..., ...}``, ready to unpack into
        :func:`dspy_rlm_hooks.enable_rlm_hooks`.
    """
    hooks = _hooks_module()
    # A wired agent can be called repeatedly or from separate execution contexts.
    # Start a fresh state at iteration zero instead of caching across runs.
    state: ContextVar[dict[str, Any] | None] = ContextVar("wire_run", default=None)

    def run_state(*, new: bool = False) -> dict[str, Any]:
        current = state.get()
        if new or current is None:
            current = {"iteration_spans": {}, "execution_spans": {}, "inputs": {}}
            state.set(current)
        return current

    def pre_iteration(
        iteration: int,
        variables: list[Any],
        history: list[Any],
        input_args: dict[str, Any],
    ) -> Any:
        output = hooks.PreIterationOutput()
        current_state = run_state(new=iteration == 0)
        if trace:
            current_state["iteration_spans"][iteration] = start_span(
                "rlm.iteration",
                {
                    "gen_ai.operation.name": "rlm.iteration",
                    "dspy.rlm.iteration": iteration,
                },
            )
        if memory is None:
            return output
        current_state["inputs"] = dict(input_args)
        if "recalled" not in current_state:
            try:
                current_state["recalled"] = memory.recall(
                    recall_query or _query(input_args)
                )
            except Exception:  # noqa: BLE001 - recall is best-effort
                logger.warning("Memory recall failed; continuing without it.")
                current_state["recalled"] = []
        found = current_state["recalled"]
        if found:
            output.prompt_context = memory.format(found)
        return output

    def pre_execution(
        iteration: int,
        code: str,
        variables: list[Any],
        history: list[Any],
        input_args: dict[str, Any],
    ) -> Any:
        if trace:
            run_state()["execution_spans"][iteration] = start_span(
                "rlm.execute",
                {
                    "gen_ai.operation.name": "rlm.execute",
                    "dspy.rlm.iteration": iteration,
                    "dspy.rlm.code.chars": len(code),
                },
            )
        if policy is None:
            return hooks.PreExecutionOutput(code=code)
        try:
            check(policy, "code", {"code": code, "iteration": iteration})
        except PermissionError as exc:
            return hooks.PreExecutionOutput(code=_refusal(str(exc)))
        return hooks.PreExecutionOutput(code=code)

    def post_execution(
        iteration: int,
        code: str,
        result: Any,
        variables: list[Any],
        history: list[Any],
        input_args: dict[str, Any],
    ) -> Any:
        if offload is not None:
            result = offload.spill(result, label=f"rlm-{iteration}")
        current = run_state()["execution_spans"].pop(iteration, None)
        if current is not None:
            current.set_attribute("dspy.rlm.result.chars", len(str(result)))
            current.end()
        return hooks.PostExecutionOutput(result=result)

    def post_iteration(
        iteration: int,
        pred: Any,
        code: str,
        result: Any,
        history: Any,
    ) -> Any:
        current_state = run_state()
        current = current_state["iteration_spans"].pop(iteration, None)
        if current is not None:
            current.set_attribute("dspy.rlm.code.chars", len(code or ""))
            current.set_attribute("dspy.rlm.result.chars", len(str(result)))
            current.end()
        if session is not None:
            session.record(
                kind="iteration",
                iteration=iteration,
                code=code,
                output=result,
            )
        inputs = current_state["inputs"]
        if memory is not None and write_back and iteration == 1 and inputs:
            try:
                memory.remember_turn(_messages(inputs))
            except Exception:  # noqa: BLE001 - write-back is best-effort
                logger.warning("Memory write-back failed; continuing.")
        return hooks.PostIterationOutput(history=history)

    return {
        "pre_iteration_hook": pre_iteration,
        "pre_execution_hook": pre_execution,
        "post_execution_hook": post_execution,
        "post_iteration_hook": post_iteration,
    }


def install(agent: Any, **options: Any) -> None:
    """Enable the composed hooks on ``agent`` (an RLM instance)."""
    hooks = _hooks_module()
    hooks.enable_rlm_hooks(agent, **build_hooks(**options))


__all__ = ["build_hooks", "install"]
