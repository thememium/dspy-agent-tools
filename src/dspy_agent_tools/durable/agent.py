"""Durable wrappers for DSPy agents and arbitrary Python callables."""

from __future__ import annotations

import copy
import functools
import importlib
import inspect
import logging
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypeVar, overload

from ._dbos import load_dbos
from .context import (
    check_pause,
    check_pause_async,
    current_facade_agent,
    current_operation_id,
    emit,
    facade_agent_scope,
    in_workflow,
    in_workflow_body,
    next_operation_id,
    operation_scope,
    set_workflow_event,
    workflow_state,
)
from .events import EventType
from .exceptions import (
    AgentExecutionError,
    DuplicateAgentName,
    DurableError,
    NotInWorkflow,
)
from .runtime import runtime
from .schedule import Schedule
from .tool import (
    call_tool,
    checkpoint_value,
    is_durable_tool,
    is_workflow_tool,
    jsonable,
    summarize,
    tool_name,
    wrap_tool,
)

if TYPE_CHECKING:
    from dspy import Module, Tool

    from .function import DurableFunction
    from .run import Run

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])

_AGENTS: dict[str, DurableAgent] = {}
_AUTO_COUNTS: dict[str, int] = {}
_INTERNAL_TOOLS = frozenset({"finish", "submit"})
_LLM_CHECKPOINT = "__durable_llm_checkpoint__"


def _is_dspy_module(value: Any) -> bool:
    try:
        import dspy
    except ImportError:  # pragma: no cover - depends on the environment
        return False
    return isinstance(value, dspy.Module)


def _auto_name(agent: Any) -> str:
    base = f"{type(agent).__module__}.{type(agent).__qualname__}"
    count = _AUTO_COUNTS.get(base, 0) + 1
    _AUTO_COUNTS[base] = count
    return base if count == 1 else f"{base}#{count}"


def _workflow_name(agent_name: str, version: str | None = None) -> str:
    base = f"dspy-agent:{agent_name}"
    return f"{base}@{version}" if version else base


def _step_options(
    name: str,
    retries: int,
    retry_on: Callable[[BaseException], bool] | None = None,
) -> Any:
    from dbos import StepOptions

    return StepOptions(
        name=name,
        retries_allowed=retries > 0,
        max_attempts=retries + 1,
        should_retry=retry_on,
    )


def _callbacks(value: Any) -> list[Callable[..., Any]]:
    if value is None:
        return []
    if callable(value):
        return [value]
    return list(value)


def _add_unique(callbacks: list[Callable[..., Any]], callback: Any) -> None:
    if callable(callback) and all(existing is not callback for existing in callbacks):
        callbacks.append(callback)


def _agent_for_name(name: str) -> DurableAgent | None:
    direct = _AGENTS.get(name)
    if direct is not None:
        return direct
    prefix = f"{name}@"
    return next(
        (agent for key, agent in _AGENTS.items() if key.startswith(prefix)),
        None,
    )


class DurableAgent:
    """A DSPy module whose predictions and tool calls are checkpointed.

    Example:
        >>> import dspy
        >>> from dspy_agent_tools import Durable
        >>> agent = Durable(
        ...     dspy.ReAct("question -> answer", tools=[]),
        ...     name="researcher",
        ... )
        >>> result = agent(question="What is new in DBOS?")
        >>> result.answer
    """

    def __init__(
        self,
        module: Module | None = None,
        *,
        factory: Callable[[], Module] | None = None,
        name: str | None = None,
        version: str | None = None,
        tenant: str | None = None,
        queue: str | None = None,
        retries: int = 0,
        retry_on: Callable[[BaseException], bool] | None = None,
        approvals: Iterable[Any] | None = None,
        lm: Any = None,
        recover: Any = None,
        verbose: bool | None = None,
    ) -> None:
        if module is None and factory is None:
            raise TypeError("Durable() requires a dspy.Module or a factory")
        if module is not None and factory is not None:
            raise TypeError("pass either a dspy.Module or factory=..., not both")
        if factory is not None:
            module = factory()
        if not _is_dspy_module(module):
            raise TypeError(
                f"Durable() expected a dspy.Module or a callable, got "
                f"{type(module).__name__}"
            )
        assert module is not None
        load_dbos()
        runtime().set_verbose(
            verbose if verbose is not None else runtime().config.verbose
        )
        self.module = module
        self._factory = factory
        self.name = name or _auto_name(module)
        self.version = version
        self.tenant = tenant
        self.queue = queue
        self.retries = max(0, int(retries))
        self.retry_on = retry_on
        self._lm = lm
        self._approval_names = {tool_name(item) for item in (approvals or ())}
        self._approval_targets = tuple(approvals or ())
        self._base_recover_callbacks = _callbacks(recover)

        owner = getattr(module, "__durable_agent__", None)
        if owner is not None and owner != self.name:
            raise DurableError(
                f"this module is already wrapped as {owner!r}; wrap each agent once"
            )
        vars(module)["__durable_agent__"] = self.name

        key = f"{self.name}@{self.version}" if self.version else self.name
        existing = _AGENTS.get(key)
        if existing is not None and existing is not self:
            raise DuplicateAgentName(
                f"a durable agent named {self.name!r} already exists; choose a "
                "stable, unique name=... for each agent"
            )
        _AGENTS[key] = self

        self._instrument(module)
        self._register_workflows()
        if queue:
            runtime().register_queue(queue)

    # ------------------------------------------------------------------
    # Registration and instrumentation
    # ------------------------------------------------------------------
    def _register_workflows(self) -> None:
        from dbos import DBOS

        @DBOS.workflow(name=_workflow_name(self.name, self.version))
        def run(**inputs: Any) -> Any:
            return self._execute(**inputs)

        @DBOS.workflow(name=f"{_workflow_name(self.name, self.version)}:scheduled")
        def scheduled(_scheduled_at: datetime, context: dict[str, Any]) -> Any:
            return self._execute(**(context or {}))

        self._run_workflow = run
        self._scheduled_workflow = scheduled

    def _instrument(self, module: Module | None = None) -> None:
        module = module or self.module
        assert module is not None
        callbacks = list(self._base_recover_callbacks)
        self._patch_predictors(module)
        self._patch_tools(module, callbacks)
        self._patch_rlm_helpers(module)
        self._collect_recovery(module, callbacks)
        vars(module)["__durable_recovery_callbacks__"] = callbacks

    def _patch_predictors(self, module: Module | None = None) -> None:
        module = module or self.module
        assert module is not None
        try:
            predictors = list(module.named_predictors())
        except AttributeError:  # pragma: no cover - non-standard Module
            return
        for path, predictor in predictors:
            if getattr(predictor, "__durable_predictor__", False):
                continue
            self._patch_predictor(predictor, path)

    def _patch_predictor(self, predictor: Any, path: str) -> None:
        # ``inspect.getattr_static`` bypasses DSPy's ``__getattribute__`` hook
        # that warns when ``forward`` is read directly. We bind the function
        # ourselves so instrumenting a module does not emit that warning.
        original = inspect.getattr_static(predictor, "forward")
        bound_forward = (
            original.__get__(predictor, type(predictor))
            if hasattr(original, "__get__")
            else original
        )
        step_name = f"{self.name}:llm:{path}"

        @functools.wraps(bound_forward)
        def forward(*args: Any, **kwargs: Any) -> Any:
            return self._llm_call(bound_forward, path, step_name, args, kwargs)

        predictor.forward = forward

        original_aforward = inspect.getattr_static(predictor, "aforward", None)
        if original_aforward is not None:
            bound_aforward = (
                original_aforward.__get__(predictor, type(predictor))
                if hasattr(original_aforward, "__get__")
                else original_aforward
            )

            @functools.wraps(bound_aforward)
            async def aforward(*args: Any, **kwargs: Any) -> Any:
                return await self._llm_call_async(
                    bound_aforward, path, step_name, args, kwargs
                )

            predictor.aforward = aforward

        vars(predictor)["__durable_predictor__"] = True

    def _patch_tools(
        self, module: Module | None = None, callbacks: list[Any] | None = None
    ) -> None:
        if callbacks is None:
            callbacks = []
        module = module or self.module
        assert module is not None
        # RLM exposes ``tools`` as a read-only property that returns a copy;
        # its mutable mapping is ``_user_tools``. Prefer that backing store so
        # instrumentation does not try to assign through the property.
        for attribute in ("_user_tools", "tools"):
            tools = getattr(module, attribute, None)
            if isinstance(tools, dict):
                break
        else:
            return
        wrapped: dict[str, Any] = {}
        for name, tool in tools.items():
            catalog = getattr(
                getattr(tool, "func", tool), "__durable_toolbox_catalog__", None
            )
            if isinstance(catalog, dict):
                wrapped[name] = self._wrap_toolbox_tool(tool, catalog, callbacks)
                continue
            if (
                name in _INTERNAL_TOOLS
                or is_workflow_tool(tool)
                or is_durable_tool(tool)
                or is_durable_tool(getattr(tool, "func", None))
                or getattr(getattr(tool, "func", tool), "__human__", None) is not None
            ):
                wrapped[name] = tool
                continue
            approval = self._approval_for(name, tool)
            wrapped[name] = wrap_tool(
                tool,
                name=name,
                retries=self.retries,
                retry_on=self.retry_on,
                approval=True if approval else None,
            )
        setattr(module, attribute, wrapped)

    def _wrap_toolbox_tool(
        self, tool: Any, catalog: dict[str, Any], callbacks: list[Any]
    ) -> Any:
        original = tool.func
        for entry in catalog.values():
            _add_unique(
                callbacks,
                getattr(entry.tool.func, "__durable_recover__", None),
            )

        @functools.wraps(original)
        def dispatch(name: str, arguments: dict[str, Any] | None = None) -> Any:
            entry = catalog.get(name)
            if entry is None:
                folded = str(name).casefold()
                entry = next(
                    (item for key, item in catalog.items() if key.casefold() == folded),
                    None,
                )
            if entry is None:
                raise ValueError(f"Unknown tool {name!r}")
            target = entry.tool.func
            args = arguments or {}
            if (
                getattr(target, "__human__", None) is not None
                or is_durable_tool(target)
                or is_workflow_tool(target)
            ):
                return target(**args)
            return call_tool(
                target,
                (),
                args,
                name=entry.name,
                retries=self.retries,
                retry_on=self.retry_on,
            )

        vars(dispatch)["__durable_tool__"] = True
        result = copy.copy(tool)
        result.func = dispatch
        vars(result)["__durable_tool__"] = True
        return result

    def _approval_for(self, name: str, tool: Any) -> bool:
        if name in self._approval_names:
            return True
        function = getattr(tool, "func", tool)
        return any(
            function is target or getattr(target, "func", None) is function
            for target in self._approval_targets
        )

    def _collect_recovery(self, module: Module, callbacks: list[Any]) -> None:
        for attribute in ("_user_tools", "tools"):
            tools = getattr(module, attribute, None)
            if isinstance(tools, dict):
                for tool in tools.values():
                    function = getattr(tool, "func", tool)
                    _add_unique(
                        callbacks,
                        getattr(function, "__durable_recover__", None),
                    )
                return

    def _module_for_run(self) -> Module:
        if self._factory is None:
            assert self.module is not None
            return self.module
        module = self._factory()
        if not _is_dspy_module(module):
            raise TypeError(
                f"agent factory returned {type(module).__name__}, expected dspy.Module"
            )
        vars(module)["__durable_agent__"] = self.name
        self._instrument(module)
        return module

    def _patch_rlm_helpers(self, module: Module | None = None) -> None:
        module = module or self.module
        assert module is not None
        prepare = getattr(module, "_prepare_execution_tools", None)
        if not callable(prepare) or getattr(module, "__durable_rlm__", False):
            return

        @functools.wraps(prepare)
        def prepare_durable(*args: Any, **kwargs: Any) -> dict[str, Any]:
            tools = prepare(*args, **kwargs)
            for helper in ("llm_query", "llm_query_batched"):
                function = tools.get(helper)
                if function is None or is_durable_tool(function):
                    continue
                if helper == "llm_query_batched":
                    tools[helper] = self._wrap_batched_llm_helper(
                        function, f"{self.name}:llm:{helper}"
                    )
                else:
                    tools[helper] = self._wrap_llm_helper(
                        function, f"{self.name}:llm:{helper}"
                    )
            return tools

        vars(module)["_prepare_execution_tools"] = prepare_durable
        vars(module)["__durable_rlm__"] = True
        self._patch_facade_call()
        self._disable_rlm_speculation(module)

    def _wrap_llm_helper(
        self, function: Callable[..., Any], step_name: str
    ) -> Callable[..., Any]:
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self._llm_call(function, step_name, step_name, args, kwargs)

        vars(wrapper)["__durable_tool__"] = True
        return wrapper

    def _wrap_batched_llm_helper(
        self, function: Callable[..., Any], step_name: str
    ) -> Callable[..., Any]:
        @functools.wraps(function)
        def wrapper(prompts: list[str]) -> list[str]:
            if not in_workflow_body():
                return function(prompts)
            results: list[str] = []
            for index, prompt in enumerate(prompts):
                result = self._llm_call(
                    function,
                    f"{step_name}:{index}",
                    f"{step_name}:{index}",
                    ([prompt],),
                    {},
                )
                results.append(result[0])
            return results

        vars(wrapper)["__durable_tool__"] = True
        return wrapper

    def _patch_facade_call(self) -> None:
        """Instrument sandbox-built sub-agents instead of nesting their steps.

        DSPy's RLM facade invokes a host predictor for ``dspy.Predict`` and
        friends. Instrumenting that predictor keeps each prediction and tool
        call as its own DBOS step, so nested approvals and checkpoints work;
        wrapping the whole sub-agent call in one step would make them nested.
        """
        try:
            from dspy.primitives import facade
        except ImportError:  # pragma: no cover - depends on DSPy internals
            return
        # DSPy 3.5 dropped ``FacadeInvocation``: ``SandboxSession`` now sends a
        # sandbox predictor call to the host as a signature plus inputs
        # (``_serve_lm``), so no host predictor object exists to instrument.
        invocation_type = getattr(facade, "FacadeInvocation", None)
        if invocation_type is None:
            logger.debug(
                "dspy.primitives.facade has no FacadeInvocation (DSPy >= 3.5); "
                "sandbox sub-agent predictions stay outside DBOS steps"
            )
            return
        original = getattr(invocation_type, "call", None)
        if original is None or getattr(original, "__durable_patched__", False):
            return

        @functools.wraps(original)
        def call(invocation: Any, handle: str, inputs: Any = None) -> Any:
            name = current_facade_agent()
            if name is None or not in_workflow_body():
                return original(invocation, handle, inputs)
            agent = _agent_for_name(name)
            predictor = getattr(invocation, "_predictors", {}).get(handle)
            if agent is not None and predictor is not None:
                agent._instrument(predictor)
            return original(invocation, handle, inputs)

        vars(call)["__durable_patched__"] = True
        invocation_type.call = call

    def _disable_rlm_speculation(self, module: Module) -> None:
        try:
            hooks = importlib.import_module("dspy_rlm_hooks")
        except ImportError:
            return
        disable = getattr(hooks, "disable_rlm_speculation", None)
        if disable is None:
            return
        try:
            disable(module)
        except Exception:
            logger.debug("could not disable RLM speculation", exc_info=True)

    # ------------------------------------------------------------------
    # Step bodies
    # ------------------------------------------------------------------
    def _llm_call(
        self,
        function: Callable[..., Any],
        path: str,
        step_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if not in_workflow_body():
            return function(*args, **kwargs)
        check_pause()
        operation_id = next_operation_id()
        input_data = {"args": args, "kwargs": kwargs}
        emit(
            EventType.LLM_STARTED,
            predictor=path,
            input=summarize(input_data),
        )
        try:
            from dbos import DBOS

            def body() -> Any:
                import dspy

                from .lm import capture_lm

                capture = capture_lm(dspy.settings.lm)
                with operation_scope(operation_id):
                    if capture is None:
                        output = function(*args, **kwargs)
                    else:
                        with dspy.context(lm=capture):
                            output = function(*args, **kwargs)
                if isinstance(output, dspy.Prediction):
                    # A Prediction is a custom class; DBOS's viewer cannot
                    # decode it as plain data. Checkpoint its fields instead
                    # and rebuild the Prediction on read.
                    rendered = jsonable(output.toDict())
                    output_type = "prediction"
                    usage = jsonable(output.get_lm_usage())
                else:
                    rendered = checkpoint_value(output)
                    output_type = "value"
                    usage = None
                return {
                    _LLM_CHECKPOINT: True,
                    "predictor": path,
                    "input": {
                        "args": jsonable(args),
                        "kwargs": jsonable(kwargs),
                    },
                    "output": rendered,
                    "replay_value": runtime().serializer.serialize(
                        checkpoint_value(output)
                    ),
                    "output_type": output_type,
                    "usage": usage,
                    "llm": jsonable(capture.responses if capture is not None else []),
                }

            result = DBOS.run_step(
                _step_options(step_name, self.retries, getattr(self, "retry_on", None)),
                body,
            )
        except BaseException as exc:
            emit(
                EventType.LLM_FAILED,
                predictor=path,
                input=summarize(input_data),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        if isinstance(result, dict) and result.get(_LLM_CHECKPOINT) is True:
            value = result["output"]
            if "replay_value" in result:
                output = runtime().serializer.deserialize(result["replay_value"])
            elif result.get("output_type") == "prediction":
                import dspy

                output = dspy.Prediction(**value)
                usage = result.get("usage")
                if usage is not None:
                    output.set_lm_usage(usage)
            else:
                # Older envelopes stored the object itself; unknown values
                # are returned as-is.
                output = value
            raw = result.get("llm", [])
        else:
            # Existing workflows can replay pre-envelope step results.
            output = result
            raw = []
        emit(
            EventType.LLM_COMPLETED,
            predictor=path,
            output=summarize(output),
            llm=summarize(raw),
        )
        return output

    async def _llm_call_async(
        self,
        function: Callable[..., Any],
        path: str,
        step_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if not in_workflow_body():
            return await function(*args, **kwargs)
        await check_pause_async()
        operation_id = next_operation_id()
        input_data = {"args": args, "kwargs": kwargs}
        emit(
            EventType.LLM_STARTED,
            predictor=path,
            input=summarize(input_data),
        )
        try:
            from dbos import DBOS

            async def body() -> Any:
                import dspy

                from .lm import capture_lm

                capture = capture_lm(dspy.settings.lm)
                with operation_scope(operation_id):
                    if capture is None:
                        output = await function(*args, **kwargs)
                    else:
                        with dspy.context(lm=capture):
                            output = await function(*args, **kwargs)
                if isinstance(output, dspy.Prediction):
                    rendered = jsonable(output.toDict())
                    output_type = "prediction"
                    usage = jsonable(output.get_lm_usage())
                else:
                    rendered = checkpoint_value(output)
                    output_type = "value"
                    usage = None
                return {
                    _LLM_CHECKPOINT: True,
                    "predictor": path,
                    "input": {
                        "args": jsonable(args),
                        "kwargs": jsonable(kwargs),
                    },
                    "output": rendered,
                    "replay_value": runtime().serializer.serialize(
                        checkpoint_value(output)
                    ),
                    "output_type": output_type,
                    "usage": usage,
                    "llm": jsonable(capture.responses if capture is not None else []),
                }

            result = await DBOS.run_step_async(
                _step_options(step_name, self.retries, getattr(self, "retry_on", None)),
                body,
            )
        except BaseException as exc:
            emit(
                EventType.LLM_FAILED,
                predictor=path,
                input=summarize(input_data),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        if isinstance(result, dict) and result.get(_LLM_CHECKPOINT) is True:
            value = result["output"]
            if "replay_value" in result:
                output = runtime().serializer.deserialize(result["replay_value"])
            elif result.get("output_type") == "prediction":
                import dspy

                output = dspy.Prediction(**value)
                usage = result.get("usage")
                if usage is not None:
                    output.set_lm_usage(usage)
            else:
                output = value
            raw = result.get("llm", [])
        else:
            output = result
            raw = []
        emit(
            EventType.LLM_COMPLETED,
            predictor=path,
            output=summarize(output),
            llm=summarize(raw),
        )
        return output

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _execute(self, **inputs: Any) -> Any:
        if not in_workflow_body():
            return self._call_module(self._module_for_run(), inputs)
        with workflow_state(self.name, self.tenant):
            check_pause()
            input_data = jsonable(inputs)
            emit(EventType.RUN_STARTED, agent=self.name, input=input_data)
            set_workflow_event("agent.input", input_data)
            try:
                module = self._module_for_run()
                for recovery in vars(module)["__durable_recovery_callbacks__"]:
                    recovery()
                with facade_agent_scope(self.name):
                    result = self._call_module(module, inputs)
            except Exception as exc:
                if type(exc).__name__ == "DBOSWorkflowCancelledError":
                    raise  # pragma: no cover - DBOS cancellation
                logger.exception("durable agent %s failed", self.name)
                error = f"{type(exc).__name__}: {exc}"
                emit(
                    EventType.RUN_FAILED,
                    agent=self.name,
                    input=input_data,
                    error=error,
                )
                set_workflow_event("agent.error", {"error": error})
                raise AgentExecutionError(
                    self.name, type(exc).__name__, str(exc)
                ) from None
            output_data = jsonable(result)
            emit(
                EventType.RUN_COMPLETED,
                agent=self.name,
                input=input_data,
                output=output_data,
            )
            set_workflow_event("agent.output", output_data)
            return result

    def _call_module(self, module: Module, inputs: dict[str, Any]) -> Any:
        import dspy

        with dspy.context(allow_tool_async_sync_conversion=True):
            if self._lm is None:
                return module(**inputs)
            with dspy.context(lm=self._lm):
                return module(**inputs)

    # ------------------------------------------------------------------
    # Public execution surface
    # ------------------------------------------------------------------
    def __call__(self, **inputs: Any) -> Any:
        """Run the agent in a durable workflow and wait for the result."""
        return self.start(**inputs).result()

    async def acall(self, **inputs: Any) -> Any:
        """Async variant of :meth:`__call__`."""
        run = await self.astart(**inputs)
        return await run.aresult()

    async def astart(
        self,
        *,
        workflow_id: str | None = None,
        timeout: float | None = None,
        **inputs: Any,
    ) -> Run:
        """Async alias for :meth:`start`; starting is non-blocking."""
        return self.start(workflow_id=workflow_id, timeout=timeout, **inputs)

    def start(
        self,
        *,
        workflow_id: str | None = None,
        timeout: float | None = None,
        **inputs: Any,
    ) -> Run:
        """Start the agent in the background and return its :class:`Run`."""
        runtime().ensure_launched()
        from dbos import SetWorkflowID, SetWorkflowTimeout

        with ExitStack() as stack:
            if workflow_id is not None:
                stack.enter_context(SetWorkflowID(workflow_id))
            if timeout is not None:
                stack.enter_context(SetWorkflowTimeout(timeout))
            handle = load_dbos().DBOS.start_workflow(self._run_workflow, **inputs)
        from .run import Run

        return Run(handle.get_workflow_id(), agent=self.name, tenant=self.tenant)

    def enqueue(
        self,
        *,
        queue: str | None = None,
        delay: float | None = None,
        priority: int | None = None,
        deduplication_id: str | None = None,
        duplication_policy: Literal["reject", "return-existing"] | None = None,
        workflow_id: str | None = None,
        queue_partition_key: str | None = None,
        timeout: float | None = None,
        **inputs: Any,
    ) -> Run:
        """Enqueue the agent on a persistent DBOS queue."""
        queue_name = queue or self.queue
        if not queue_name:
            raise DurableError(
                "enqueue() needs a queue: pass queue=... or create the agent with queue=..."
            )
        runtime().ensure_launched()
        runtime().register_queue(queue_name)
        from dbos import SetEnqueueOptions, SetWorkflowID, SetWorkflowTimeout

        with ExitStack() as stack:
            if any(
                value is not None
                for value in (
                    delay,
                    priority,
                    deduplication_id,
                    duplication_policy,
                    queue_partition_key,
                )
            ):
                stack.enter_context(
                    SetEnqueueOptions(
                        delay_seconds=delay,
                        priority=priority,
                        deduplication_id=deduplication_id,
                        duplication_policy=duplication_policy,
                        queue_partition_key=queue_partition_key,
                    )
                )
            if workflow_id is not None:
                stack.enter_context(SetWorkflowID(workflow_id))
            if timeout is not None:
                stack.enter_context(SetWorkflowTimeout(timeout))
            handle = load_dbos().DBOS.enqueue_workflow(
                queue_name, self._run_workflow, **inputs
            )
        from .run import Run

        return Run(handle.get_workflow_id(), agent=self.name, tenant=self.tenant)

    def schedule(
        self,
        *,
        cron: str,
        inputs: dict[str, Any] | None = None,
        name: str | None = None,
        timezone: str | None = None,
        queue: str | None = None,
        **schedule_inputs: Any,
    ) -> Schedule:
        """Register a recurring durable execution."""
        runtime().ensure_launched()
        schedule_name = name or f"{self.name}:schedule"
        context = {**(inputs or {}), **schedule_inputs}
        load_dbos().DBOS.create_schedule(
            schedule_name=schedule_name,
            workflow_fn=self._scheduled_workflow,
            schedule=cron,
            context=context,
            cron_timezone=timezone,
            queue_name=queue or self.queue,
        )
        return Schedule(schedule_name)

    def __repr__(self) -> str:
        return f"DurableAgent(name={self.name!r}, module={type(self.module).__name__})"


class Durable:
    """Make an existing DSPy agent, function, or tool durable.

    ``Durable(agent)`` detects a ``dspy.Module`` and returns
    :class:`DurableAgent`; a plain callable becomes
    :class:`DurableFunction`. The class also holds the process-wide runtime
    configuration and the small wrapping helpers.

    Example:
        >>> import dspy
        >>> from dspy_agent_tools import Durable, Human, bash_exec, web_search
        >>> Durable.configure(name="my-agents")
        >>> agent = Durable(
        ...     dspy.ReAct(
        ...         "question -> answer",
        ...         tools=[web_search, Human(bash_exec)],
        ...     ),
        ...     name="researcher",
        ... )
        >>> result = agent(question="What is new in DBOS?")
        >>> run = agent.start(question="What is new in DBOS?")
        >>> run.status()
        'SUCCESS'
    """

    if TYPE_CHECKING:  # pragma: no cover - type-only overloads

        @overload
        def __new__(
            cls,
            target: Module,
            *,
            name: str | None = ...,
            version: str | None = ...,
            tenant: str | None = ...,
            queue: str | None = ...,
            retries: int = ...,
            retry_on: Callable[[BaseException], bool] | None = ...,
            approvals: Iterable[Any] | None = ...,
            lm: Any = ...,
            recover: Any = ...,
            verbose: bool | None = ...,
        ) -> DurableAgent: ...

        @overload
        def __new__(
            cls,
            target: Callable[..., Any],
            *,
            name: str | None = ...,
            version: str | None = ...,
            tenant: str | None = ...,
            queue: str | None = ...,
            retries: int = ...,
            concurrency: int | None = ...,
            worker: bool = ...,
            verbose: bool | None = ...,
        ) -> DurableFunction: ...

        @overload
        def __new__(cls, target: None = None, **kwargs: Any) -> NoReturn: ...

        @classmethod
        @overload
        def tool(cls, fn: None = None, **options: Any) -> Callable[[_F], _F]: ...

        @classmethod
        @overload
        def tool(
            cls, fn: _F, *, approval: None = None, when: None = None, **options: Any
        ) -> _F: ...

        @classmethod
        @overload
        def tool(cls, fn: Tool, **options: Any) -> Tool: ...

        @classmethod
        @overload
        def tool(cls, fn: _F, **options: Any) -> Callable[..., Any]: ...

        @classmethod
        @overload
        def worker(
            cls, fn: None = None, **options: Any
        ) -> Callable[[_F], DurableFunction]: ...

        @classmethod
        @overload
        def worker(cls, fn: _F, **options: Any) -> DurableFunction: ...

        @classmethod
        @overload
        def workflow(
            cls, fn: None = None, **options: Any
        ) -> Callable[[_F], DurableFunction]: ...

        @classmethod
        @overload
        def workflow(cls, fn: _F, **options: Any) -> DurableFunction: ...

        @classmethod
        @overload
        def step(cls, fn: None = None, **options: Any) -> Callable[[_F], _F]: ...

        @classmethod
        @overload
        def step(cls, fn: _F, **options: Any) -> _F: ...

    def __new__(
        cls,
        target: Any = None,
        **kwargs: Any,
    ) -> Any:
        if target is None:
            raise TypeError("Durable() requires a dspy.Module or a callable")
        if _is_dspy_module(target):
            return DurableAgent(target, **kwargs)
        if callable(target):
            from .function import DurableFunction

            return DurableFunction(target, **kwargs)
        raise TypeError(
            f"Durable() expected a dspy.Module or a callable, got "
            f"{type(target).__name__}"
        )

    @classmethod
    def agent(cls, agent: Module | None = None, **kwargs: Any) -> Any:
        """Wrap an agent, or register a factory with ``@Durable.agent``.

        With a factory, each run constructs a fresh module, which is the right
        shape for agents that own browser or MCP sessions.

        Example:
            >>> @Durable.agent(name="coder", version="v1")
            ... def make_coder():
            ...     return dspy.RLM("task -> summary", tools=[])
        """
        if agent is None:
            return lambda factory: DurableAgent(factory=factory, **kwargs)
        return DurableAgent(agent, **kwargs)

    @classmethod
    def configure(
        cls,
        *,
        name: str | None = None,
        database_url: str | None = None,
        manage_runtime: bool | None = None,
        application_version: str | None = None,
        executor_id: str | None = None,
        log_level: str | None = None,
        verbose: bool | None = None,
        authorizer: (
            Callable[[str, str, str | None, dict[str, Any] | None], bool] | None
        ) = None,
    ) -> None:
        """Configure the process-wide DBOS runtime exactly once at startup.

        ``verbose=False`` (the default) keeps DBOS INFO logs and DSPy's
        direct-``forward`` warning quiet. Set ``verbose=True`` to see them.

        Example:
            >>> import os
            >>> from dspy_agent_tools import Durable
            >>> Durable.configure(
            ...     name="my-agents",
            ...     database_url=os.environ["DATABASE_URL"],
            ...     verbose=True,
            ... )
        """
        load_dbos()
        runtime().configure(
            name=name,
            database_url=database_url,
            manage_runtime=manage_runtime,
            application_version=application_version,
            executor_id=executor_id,
            log_level=log_level,
            verbose=verbose,
            authorizer=authorizer,
        )

    @classmethod
    def launch(cls) -> None:
        """Launch DBOS explicitly instead of on first execution."""
        runtime().launch()

    @classmethod
    def shutdown(cls, *, timeout: float = 10.0) -> None:
        """Stop DBOS and release background threads."""
        runtime().shutdown(timeout=timeout)

    @classmethod
    def tool(cls, fn: Any = None, **options: Any) -> Any:
        """Checkpoint a single tool, usable directly or as a decorator.

        Example:
            >>> from dspy_agent_tools import Durable, bash_exec
            >>> shell = Durable.tool(bash_exec, retries=2)
            >>> @Durable.tool
            ... def lookup(customer_id: str) -> dict:
            ...     return {"id": customer_id}
        """
        if fn is None:
            return lambda target: wrap_tool(target, **options)
        return wrap_tool(fn, **options)

    @classmethod
    def step(cls, fn: Any = None, **options: Any) -> Any:
        """Checkpoint a plain function as a DBOS step.

        The decorator spelling of :meth:`tool` for code that is not a DSPy
        tool. Inside a durable workflow each call records its inputs and
        output; outside one the function runs normally.

        Example:
            >>> from dspy_agent_tools import Durable
            >>> @Durable.step
            ... def lookup(customer_id: str) -> dict:
            ...     return {"id": customer_id}
        """
        return cls.tool(fn, **options)

    @classmethod
    def worker(cls, fn: Any = None, **options: Any) -> Any:
        """Turn a Python function into a queued durable worker.

        Example:
            >>> from dspy_agent_tools import Durable
            >>> @Durable.worker(queue="indexing", concurrency=5)
            ... def index(repo: str) -> dict:
            ...     return {"repo": repo}
            >>> run = index.enqueue(repo="thememium/dspy-agent-tools")
        """
        from .function import DurableFunction

        if fn is None:
            return lambda target: DurableFunction(target, worker=True, **options)
        return DurableFunction(fn, worker=True, **options)

    @classmethod
    def workflow(cls, fn: Any = None, **options: Any) -> Any:
        """Turn a Python function into a durable workflow.

        The decorator spelling of ``Durable(fn)``. Returns a
        :class:`DurableFunction`, so ``start``, ``enqueue``, ``schedule``, and
        ``Run`` work exactly as they do for ``Durable(fn)``.

        Workflow arguments are keyword arguments: ``main(url=...)``.

        Example:
            >>> from dspy_agent_tools import Durable
            >>> @Durable.workflow(name="pipeline")
            ... def main(*, url: str) -> str:
            ...     return url
            >>> main(url="https://example.com")
        """
        from .function import DurableFunction

        if fn is None:
            return lambda target: DurableFunction(target, **options)
        return DurableFunction(fn, **options)

    @classmethod
    def tools(cls, tools: Iterable[Any], **options: Any) -> list[Any]:
        """Checkpoint a collection of tools."""
        return [wrap_tool(tool, **options) for tool in tools]

    @classmethod
    def schedule(
        cls, target: DurableAgent | DurableFunction, **options: Any
    ) -> Schedule:
        """Create a schedule from any durable agent or worker."""
        return target.schedule(**options)

    @classmethod
    def pause_point(cls) -> None:
        """Yield to a pending pause at a safe workflow boundary."""
        check_pause()

    @classmethod
    async def pause_point_async(cls) -> None:
        """Async variant of :meth:`pause_point`."""
        await check_pause_async()

    @classmethod
    def sleep(cls, seconds: float) -> None:
        """Sleep durably at workflow level; plain sleep elsewhere.

        DBOS only records a durable sleep in a workflow body. Sleeping inside a
        checkpointed step is rejected because it would create a nested
        operation and break replay boundaries.
        """
        if in_workflow_body():
            load_dbos().DBOS.sleep(seconds)
            return
        if in_workflow():
            raise DurableError(
                "Durable.sleep() must run at workflow level, not inside a "
                "checkpointed step; move the sleep out of the tool body"
            )
        import time

        time.sleep(seconds)

    @classmethod
    def queue(
        cls, name: str, *, concurrency: int | None = None, **options: Any
    ) -> None:
        """Declare a persistent queue before the runtime launches."""
        runtime().register_queue(name, concurrency=concurrency, **options)

    @classmethod
    def get_run(cls, run_id: str) -> Run:
        """Reconnect to a run by workflow ID."""
        from .run import Run

        return Run.get(run_id)

    @classmethod
    def list_runs(cls, **filters: Any) -> list[Run]:
        """List runs, accepting :meth:`Run.list_runs` filters."""
        from .run import Run

        return Run.list_runs(**filters)

    @classmethod
    def get_schedule(cls, name: str) -> Schedule:
        """Look up an existing schedule."""
        return Schedule.get(name)

    @classmethod
    def list_schedules(cls, **filters: Any) -> list[dict[str, Any]]:
        """List schedule rows from the DBOS system database."""
        return [dict(row) for row in runtime().client().list_schedules(**filters)]

    @classmethod
    def operation_id(cls) -> str:
        """Return the replay-stable ID of the current durable operation."""
        operation_id = current_operation_id()
        if operation_id is None:
            raise NotInWorkflow("operation_id() must be called inside a durable step")
        return operation_id

    @classmethod
    def worker_tools(cls, allowed: Iterable[Any], **options: Any) -> list[Tool]:
        """Expose registered workers to an LLM through orchestration tools."""
        from .worker import worker_tools

        return worker_tools(allowed, **options)
