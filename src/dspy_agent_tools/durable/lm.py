"""Capture raw LM responses while a durable predictor step runs.

DBOS checkpoints the predictor's return value, but the parsed
:class:`dspy.Prediction` is not always enough to see what the model actually
returned. This proxy stays inside the predictor step (it is not itself a DBOS
step, so replay semantics are unchanged) and records each provider response
for inclusion in the step checkpoint.
"""

from __future__ import annotations

from typing import Any

from dspy.clients.base_lm import BaseLM


class CapturingLM(BaseLM):
    """Delegating LM proxy that records every response it receives."""

    def __init__(self, lm: BaseLM) -> None:
        model = str(getattr(lm, "model", "") or "lm")
        super().__init__(model=model)
        self._lm = lm
        self.responses: list[Any] = []
        # Mirror the real LM's state so DSPy sees the original configuration
        # and history/usage tracking still reads real calls.
        self.model = model
        self.model_type = getattr(lm, "model_type", "chat")
        self.cache = getattr(lm, "cache", False)
        self.callbacks = list(getattr(lm, "callbacks", None) or [])
        self.num_retries = getattr(lm, "num_retries", 3)
        self.kwargs = getattr(lm, "kwargs", None) or {}
        self.history = getattr(lm, "history", None) or []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        response = self._lm(*args, **kwargs)
        self.responses.append(response)
        return response

    async def acall(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._lm.acall(*args, **kwargs)
        self.responses.append(response)
        return response

    # Adapters choose native tool calling, native reasoning and structured
    # output from these hints. BaseLM's own properties answer for this proxy,
    # not the wrapped LM, and would report none: the adapter then inlines the
    # tools and reasoning fields into the prompt, which providers reject or
    # misparse (gpt-oss emits Harmony tool calls the request never allowed;
    # a text `plan` field goes missing). Answer for the wrapped model, as
    # DSPy's own SandboxLM proxy does.
    @property
    def supports_function_calling(self) -> bool:
        return bool(getattr(self._lm, "supports_function_calling", False))

    @property
    def supports_reasoning(self) -> bool:
        return bool(getattr(self._lm, "supports_reasoning", False))

    @property
    def supports_response_schema(self) -> bool:
        return bool(getattr(self._lm, "supports_response_schema", False))

    @property
    def supported_params(self) -> set[str]:
        return set(getattr(self._lm, "supported_params", ()) or ())

    @property
    def supported_tool_result_media(self) -> set[str]:
        return set(getattr(self._lm, "supported_tool_result_media", ()) or ())

    def __getattr__(self, name: str) -> Any:
        if name == "_lm":
            raise AttributeError(name)
        return getattr(self._lm, name)

    def __repr__(self) -> str:
        return f"CapturingLM({self.model})"


def capture_lm(lm: Any) -> CapturingLM | None:
    """Wrap *lm* for response capture, or return ``None`` if not applicable."""
    if lm is None or isinstance(lm, CapturingLM) or not isinstance(lm, BaseLM):
        return None
    return CapturingLM(lm)
